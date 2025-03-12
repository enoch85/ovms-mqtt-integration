"""MQTT Client for OVMS Integration."""
import asyncio
import logging
import re
import time
import json
import uuid
import hashlib
from typing import Any, Dict, Optional, Tuple

import paho.mqtt.client as mqtt

from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send, async_dispatcher_connect
from homeassistant.helpers import device_registry as dr

from .const import (
    CONF_CLIENT_ID,
    CONF_MQTT_USERNAME,
    CONF_QOS,
    CONF_TOPIC_PREFIX,
    CONF_TOPIC_STRUCTURE,
    CONF_VEHICLE_ID,
    CONF_VERIFY_SSL,
    CONF_ORIGINAL_VEHICLE_ID,
    DEFAULT_TOPIC_STRUCTURE,
    DEFAULT_VERIFY_SSL,
    DEFAULT_COMMAND_RATE_LIMIT,
    DEFAULT_COMMAND_RATE_PERIOD,
    DOMAIN,
    LOGGER_NAME,
    SIGNAL_ADD_ENTITIES,
    SIGNAL_UPDATE_ENTITY,
    SIGNAL_PLATFORMS_LOADED,
    TOPIC_TEMPLATE,
    COMMAND_TOPIC_TEMPLATE,
    RESPONSE_TOPIC_TEMPLATE,
)
from .rate_limiter import CommandRateLimiter
from .metrics import (
    BINARY_METRICS,
    get_metric_by_path,
    get_metric_by_pattern,
    determine_category_from_topic,
    create_friendly_name,
)

_LOGGER = logging.getLogger(LOGGER_NAME)

# Maximum number of reconnection attempts before giving up
MAX_RECONNECTION_ATTEMPTS = 10


def ensure_serializable(obj):
    """Ensure objects are JSON serializable.

    Converts MQTT-specific types to standard Python types.
    """
    if isinstance(obj, dict):
        return {k: ensure_serializable(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [ensure_serializable(item) for item in obj]
    elif isinstance(obj, tuple):
        return [ensure_serializable(item) for item in obj]
    elif hasattr(obj, '__dict__'):  # Convert custom objects to dict
        return {k: ensure_serializable(v) for k, v in obj.__dict__.items()
                if not k.startswith('_')}
    elif obj.__class__.__name__ == 'ReasonCodes':  # Handle MQTT ReasonCodes specifically
        try:
            return [int(code) for code in obj]  # Convert to list of integers
        except (ValueError, TypeError):
            return str(obj)   # Fall back to string representation
    return obj


class OVMSMQTTClient:
    """MQTT Client for OVMS Integration."""

    def __init__(self, hass: HomeAssistant, config: Dict[str, Any]):
        """Initialize the MQTT Client."""
        self.hass = hass
        self.config = config
        self.client = None
        self.connected = False
        self.topic_cache = {}
        self.discovered_topics = set()
        self.entity_registry = {}
        self.entity_types = {}  # Track entity types for diagnostics
        self.structure_prefix = None  # Will be initialized in setup
        self._cleanup_task = None
        self._last_gps_update = 0  # Track last GPS update time
        self._shutting_down = False  # Flag to indicate shutdown in progress

        # Set up command response tracking
        self.pending_commands = {}

        # Set up rate limiter
        self.command_limiter = CommandRateLimiter(
            max_calls=DEFAULT_COMMAND_RATE_LIMIT,
            period=DEFAULT_COMMAND_RATE_PERIOD
        )

        # Debug counters
        self.message_count = 0
        self.reconnect_count = 0
        self.last_reconnect_time = 0

        # Entity queue for those discovered before platforms are ready
        self.entity_queue = asyncio.Queue()
        self.platforms_loaded = False

        # Status tracking
        self._status_topic = None
        self._connected_payload = "online"

        # Device tracker support
        self.has_device_tracker = False
        self.latitude_topic = None
        self.longitude_topic = None
        self.gps_topics = []
        
        # Store sensor IDs for updating individual lat/lon sensors
        self.lat_sensor_id = None
        self.lon_sensor_id = None

    async def async_setup(self) -> bool:
        """Set up the MQTT client."""
        _LOGGER.debug("Setting up MQTT client")

        # Initialize empty tracking sets/dictionaries
        self.discovered_topics = set()
        self.entity_registry = {}
        self.entity_types = {}
        self.topic_cache = {}
        self._shutting_down = False

        # Format the topic structure prefix
        self.structure_prefix = self._format_structure_prefix()
        _LOGGER.debug("Using structure prefix: %s", self.structure_prefix)

        # Create the MQTT client
        self.client = await self._create_mqtt_client()
        if not self.client:
            _LOGGER.error("Failed to create MQTT client")
            return False

        # Set up the callbacks
        self._setup_callbacks()

        # Connect to the broker
        result = await self._async_connect()
        if not result:
            _LOGGER.error("Failed to connect to MQTT broker")
            return False

        # Subscribe to topics
        self._subscribe_topics()

        # Set up listener for when all platforms are loaded
        async_dispatcher_connect(
            self.hass,
            SIGNAL_PLATFORMS_LOADED,
            self._async_platforms_loaded
        )

        # Start the cleanup task for pending commands
        self._cleanup_task = asyncio.create_task(self._async_cleanup_pending_commands())

        return True

    async def _create_mqtt_client(self) -> mqtt.Client:
        """Create and configure the MQTT client."""
        client_id = self.config.get(CONF_CLIENT_ID)
        protocol = mqtt.MQTTv5 if hasattr(mqtt, 'MQTTv5') else mqtt.MQTTv311

        _LOGGER.debug("Creating MQTT client with ID: %s", client_id)
        try:
            client = mqtt.Client(client_id=client_id, protocol=protocol)
        except Exception as ex:
            _LOGGER.error("Failed to create MQTT client: %s", ex)
            return None

        # Configure authentication if provided
        username = self.config.get(CONF_USERNAME)
        if username and client:
            password = self.config.get(CONF_PASSWORD)
            _LOGGER.debug("Setting username and password for MQTT client")
            client.username_pw_set(
                username=username, password=password,
            )

        # Configure TLS if needed
        if self.config.get(CONF_PORT) == 8883:
            _LOGGER.debug("Enabling SSL/TLS for port 8883")
            # pylint: disable=import-outside-toplevel
            import ssl
            verify_ssl = self.config.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)

            # Use executor to avoid blocking the event loop
            context = await self.hass.async_add_executor_job(ssl.create_default_context)

            # Allow self-signed certificates if verification is disabled
            if not verify_ssl:
                _LOGGER.debug("SSL certificate verification disabled")
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE

            client.tls_set_context(context)

        # Add Last Will and Testament message
        self._status_topic = f"{self.structure_prefix}/status"
        will_payload = "offline"
        will_qos = self.config.get(CONF_QOS, 1)
        will_retain = True

        client.will_set(self._status_topic, will_payload, will_qos, will_retain)

        # Add MQTT v5 properties when available
        if hasattr(mqtt, 'MQTTv5') and hasattr(mqtt, 'Properties') and hasattr(mqtt, 'PacketTypes'):
            try:
                properties = mqtt.Properties(mqtt.PacketTypes.CONNECT)
                properties.UserProperty = ("client_type", "home_assistant_ovms")
                properties.UserProperty = ("version", "1.0.0")
                client.connect_properties = properties
            except (TypeError, AttributeError) as ex:
                _LOGGER.debug("Failed to set MQTT v5 properties: %s", ex)
                # Continue without properties

        return client

    def _setup_callbacks(self) -> None:
        """Set up the MQTT callbacks."""
        # pylint: disable=unused-argument

        # Handle different MQTT protocol versions
        if hasattr(mqtt, 'MQTTv5'):
            def on_connect(client, userdata, flags, rc, properties=None):
                """Handle connection result."""
                self._on_connect_callback(client, userdata, flags, rc)
        else:
            def on_connect(client, userdata, flags, rc):
                """Handle connection result for MQTT v3."""
                self._on_connect_callback(client, userdata, flags, rc)

        def on_disconnect(client, userdata, rc, properties=None):
            """Handle disconnection."""
            self.connected = False

            # Only increment reconnect count and log if not shutting down
            if not self._shutting_down:
                self.reconnect_count += 1
                self.last_reconnect_time = time.time()
                _LOGGER.info("Disconnected from MQTT broker: %s", rc)

                # Schedule reconnection if not intentional disconnect
                if rc != 0:
                    _LOGGER.warning(
                        "Unintentional disconnect. Scheduling reconnection attempt."
                    )
                    asyncio.run_coroutine_threadsafe(
                        self._async_reconnect(),
                        self.hass.loop,
                    )
            else:
                _LOGGER.debug("Disconnected during shutdown, not attempting reconnect")

        def on_message(client, userdata, msg):
            """Handle incoming messages."""
            try:
                self.message_count += 1
                log_msg = f"Message #{self.message_count} received on topic: {msg.topic}"
                log_msg += f" (payload len: {len(msg.payload)})"
                _LOGGER.debug(log_msg)

                # Try to decode payload for debug logging
                if _LOGGER.isEnabledFor(logging.DEBUG):
                    try:
                        payload_str = msg.payload.decode('utf-8')
                        if len(payload_str) > 200:
                            payload_preview = f"{payload_str[:200]}..."
                        else:
                            payload_preview = payload_str
                        _LOGGER.debug("Payload: %s", payload_preview)
                    except UnicodeDecodeError:
                        _LOGGER.debug("Payload: <binary data>")

                # Process the message
                asyncio.run_coroutine_threadsafe(
                    self._async_process_message(msg),
                    self.hass.loop,
                )
            except Exception as ex:
                _LOGGER.exception("Error in message handler: %s", ex)

        def on_subscribe(client, userdata, mid, granted_qos, properties=None):
            """Handle subscription confirmation."""
            # Convert ReasonCodes to a serializable format
            try:
                serialized_qos = ensure_serializable(granted_qos)
                _LOGGER.debug("Subscription confirmed. MID: %s, QoS: %s", mid, serialized_qos)
            except Exception as ex:
                _LOGGER.exception("Error in subscription handler: %s", ex)

        # Set the callbacks
        self.client.on_connect = on_connect
        self.client.on_disconnect = on_disconnect
        self.client.on_message = on_message

        # Set the subscription callback with protocol version handling
        if hasattr(mqtt, 'MQTTv5'):
            self.client.on_subscribe = on_subscribe
        else:
            # For MQTT v3.1.1
            def on_subscribe_v311(client, userdata, mid, granted_qos):
                try:
                    on_subscribe(client, userdata, mid, granted_qos, None)
                except Exception as ex:
                    _LOGGER.exception("Error in v3.1.1 subscription handler: %s", ex)
            self.client.on_subscribe = on_subscribe_v311

    def _on_connect_callback(self, client, userdata, flags, rc):
        """Common connection callback for different MQTT versions."""
        try:
            if hasattr(mqtt, 'ReasonCodes'):
                try:
                    reason_code = mqtt.ReasonCodes(mqtt.CMD_CONNACK, rc)
                    _LOGGER.info("Connected to MQTT broker with result: %s", reason_code)
                except (TypeError, AttributeError):
                    _LOGGER.info("Connected to MQTT broker with result code: %s", rc)
            else:
                _LOGGER.info("Connected to MQTT broker with result code: %s", rc)

            if rc == 0:
                self.connected = True
                # Reset reconnect count on successful connection
                self.reconnect_count = 0
                # Re-subscribe if we get disconnected
                self._subscribe_topics()

                # Publish online status when connected
                if self._status_topic:
                    client.publish(
                        self._status_topic,
                        self._connected_payload,
                        qos=self.config.get(CONF_QOS, 1),
                        retain=True
                    )
            else:
                self.connected = False
                _LOGGER.error("Failed to connect to MQTT broker: %s", rc)
        except Exception as ex:
            _LOGGER.exception("Error in connect callback: %s", ex)

    async def _async_connect(self) -> bool:
        """Connect to the MQTT broker."""
        host = self.config.get(CONF_HOST)
        port = self.config.get(CONF_PORT)

        _LOGGER.debug("Connecting to MQTT broker at %s:%s", host, port)

        try:
            # Connect using the executor to avoid blocking
            await self.hass.async_add_executor_job(
                self.client.connect,
                host,
                port,
                60,  # Keep alive timeout
            )

            # Start the loop in a separate thread
            self.client.loop_start()

            # Wait for the connection to establish
            for _ in range(10):  # Try for up to 5 seconds
                if self.connected:
                    _LOGGER.info("Successfully connected to MQTT broker")
                    return True
                await asyncio.sleep(0.5)

            _LOGGER.error("Timed out waiting for MQTT connection")
            return False

        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.exception("Failed to connect to MQTT broker: %s", ex)
            return False

    async def _async_reconnect(self) -> None:
        """Reconnect to the MQTT broker with exponential backoff."""
        # Check if we're shutting down or reached the maximum number of attempts
        if self._shutting_down:
            _LOGGER.debug("Not reconnecting because shutdown is in progress")
            return

        if self.reconnect_count > MAX_RECONNECTION_ATTEMPTS:
            _LOGGER.error(
                "Maximum reconnection attempts (%d) reached, giving up",
                MAX_RECONNECTION_ATTEMPTS
            )
            return

        # Calculate backoff time based on reconnect count
        backoff = min(30, 2 ** min(self.reconnect_count, 5))  # Cap at 30 seconds
        reconnect_msg = f"Reconnecting in {backoff} seconds (attempt #{self.reconnect_count})"
        _LOGGER.info(reconnect_msg)

        try:
            await asyncio.sleep(backoff)

            # Check again if we're shutting down after the sleep
            if self._shutting_down or self.connected:
                return

            # Use clean_session=False for persistent sessions
            if hasattr(mqtt, 'MQTTv5'):
                try:
                    client_options = {'clean_start': False}
                    await self.hass.async_add_executor_job(
                        self.client.reconnect, **client_options
                    )
                except TypeError:
                    # Fallback for older clients without clean_start parameter
                    await self.hass.async_add_executor_job(
                        self.client.reconnect
                    )
            else:
                await self.hass.async_add_executor_job(
                    self.client.reconnect
                )
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.exception("Failed to reconnect to MQTT broker: %s", ex)
            # Schedule another reconnect attempt
            if not self._shutting_down:
                asyncio.create_task(self._async_reconnect())

    async def _async_cleanup_pending_commands(self) -> None:
        """Periodically clean up timed-out command requests."""
        while not self._shutting_down:
            try:
                # Run every 60 seconds
                await asyncio.sleep(60)

                # Bail if shutting down
                if self._shutting_down:
                    break

                current_time = time.time()
                expired_commands = []

                for command_id, command_data in self.pending_commands.items():
                    # Check if command is older than 5 minutes
                    if current_time - command_data["timestamp"] > 300:
                        expired_commands.append(command_id)
                        _LOGGER.debug("Cleaning up expired command: %s", command_id)

                # Remove expired commands
                for command_id in expired_commands:
                    future = self.pending_commands[command_id]["future"]
                    if not future.done():
                        future.set_exception(
                            asyncio.TimeoutError("Command expired during cleanup")
                        )
                    if command_id in self.pending_commands:
                        del self.pending_commands[command_id]

                _LOGGER.debug("Cleaned up %d expired commands", len(expired_commands))

            except asyncio.CancelledError:
                # Handle task cancellation correctly
                _LOGGER.debug("Command cleanup task cancelled")
                break
            except Exception as ex:  # pylint: disable=broad-except
                _LOGGER.exception("Error in command cleanup task: %s", ex)
                # Wait a bit before retrying to avoid tight loop
                await asyncio.sleep(5)

                # Don't restart if shutting down
                if self._shutting_down:
                    break

    def _subscribe_topics(self) -> None:
        """Subscribe to the OVMS topics."""
        if not self.connected:
            _LOGGER.warning("Cannot subscribe to topics, not connected")
            return

        try:
            # Format the topic
            qos = self.config.get(CONF_QOS)

            # Subscribe to all topics under the structure prefix
            topic = TOPIC_TEMPLATE.format(structure_prefix=self.structure_prefix)

            _LOGGER.info("Subscribing to OVMS topic: %s", topic)
            self.client.subscribe(topic, qos=qos)

            # Also subscribe to response topics for commands
            response_topic = RESPONSE_TOPIC_TEMPLATE.format(
                structure_prefix=self.structure_prefix,
                command_id="+"  # MQTT wildcard for any command ID
            )
            _LOGGER.info("Subscribing to response topic: %s", response_topic)
            self.client.subscribe(response_topic, qos=qos)

            # Add a subscription with vehicle ID but without username
            # Handle the case where topics might use different username patterns
            vehicle_id = self.config.get(CONF_VEHICLE_ID, "")
            prefix = self.config.get(CONF_TOPIC_PREFIX, "")
            if vehicle_id and prefix:
                alternative_topic = f"{prefix}/+/{vehicle_id}/#"
                _LOGGER.info("Also subscribing to alternative topic pattern: %s", alternative_topic)
                self.client.subscribe(alternative_topic, qos=qos)
        except Exception as ex:
            _LOGGER.exception("Error subscribing to topics: %s", ex)

    def _format_structure_prefix(self) -> str:
        """Format the topic structure prefix based on configuration."""
        try:
            structure = self.config.get(CONF_TOPIC_STRUCTURE, DEFAULT_TOPIC_STRUCTURE)
            prefix = self.config.get(CONF_TOPIC_PREFIX)
            vehicle_id = self.config.get(CONF_VEHICLE_ID)
            mqtt_username = self.config.get(CONF_MQTT_USERNAME, "")

            # If username format appears inconsistent, try to adapt
            if mqtt_username and vehicle_id:
                expected_username = f"ovms-mqtt-{vehicle_id.lower()}"
                if mqtt_username.lower() != expected_username:
                    # If username doesn't already contain vehicle ID, consider adding it
                    if vehicle_id.lower() not in mqtt_username.lower():
                        alternative_username = f"ovms-mqtt-{vehicle_id.lower()}"
                        log_msg = f"Username {mqtt_username} may not match pattern. "
                        log_msg += f"Also trying {alternative_username}"
                        _LOGGER.debug(log_msg)
                        # Don't replace the username yet, just log the possibility

            # Replace the variables in the structure
            structure_prefix = structure.format(
                prefix=prefix,
                vehicle_id=vehicle_id,
                mqtt_username=mqtt_username
            )

            _LOGGER.debug("Formatted structure prefix: %s", structure_prefix)
            return structure_prefix
        except Exception as ex:
            _LOGGER.exception("Error formatting structure prefix: %s", ex)
            # Fallback to a simple default
            return f"{prefix}/{vehicle_id}"

    def _update_firmware_version(self, device_id: str, version: str) -> None:
        """Update the firmware version in the device registry."""
        try:
            device_registry = dr.async_get(self.hass)

            # Create identifier tuple
            identifier = (DOMAIN, device_id)

            # Get device and update firmware version
            device_entry = device_registry.async_get_device({identifier})

            if device_entry:
                _LOGGER.info("Updating device %s firmware version to %s", device_id, version)
                device_registry.async_update_device(
                    device_entry.id, sw_version=version
                )
            else:
                # Log that device wasn't found
                _LOGGER.debug("Device not found for ID %s, will retry later", device_id)
                self.hass.async_create_task(self._retry_firmware_update(device_id, version))
        except Exception as ex:
            _LOGGER.exception("Error updating firmware version: %s", ex)

    async def _retry_firmware_update(self, device_id, version, attempts=3):
        """Retry firmware update after a delay."""
        for attempt in range(attempts):
            await asyncio.sleep(5 * (attempt + 1))  # Increasing delay
            try:
                device_registry = dr.async_get(self.hass)
                device_entry = device_registry.async_get_device({(DOMAIN, device_id)})
                if device_entry:
                    device_registry.async_update_device(
                        device_entry.id, sw_version=version
                    )
                    _LOGGER.info("Successfully updated firmware version to %s on retry", version)
                    return
            except Exception as ex:  # pylint: disable=broad-except
                _LOGGER.error("Error during firmware update retry: %s", ex)

        _LOGGER.warning("Failed to update firmware version after %d attempts", attempts)

    @callback
    def _process_version_message(self, topic: str, payload: str) -> None:
        """Process firmware version from MQTT message."""
        try:
            # Check if this is a firmware version message
            if not payload or not isinstance(payload, str):
                return

            # Skip if not a version topic
            if not any(version_key in topic.lower() for version_key in [
                "/version", "m.version", "m/version", "firmware"
            ]):
                return

            # Get vehicle ID from config
            vehicle_id = self.config.get("vehicle_id", "")
            if not vehicle_id:
                return

            # Update firmware version
            _LOGGER.debug("Detected firmware version topic: %s with value: %s",
                         topic, payload)
            self._update_firmware_version(vehicle_id, payload)

        except Exception as ex:
            _LOGGER.exception("Error processing version message: %s", ex)

    async def _async_process_message(self, msg) -> None:
        """Process an incoming MQTT message."""
        try:
            topic = msg.topic
            try:
                payload = msg.payload.decode("utf-8")
            except UnicodeDecodeError:
                _LOGGER.warning("Failed to decode payload for topic %s", topic)
                payload = "<binary data>"

            # Log message for debugging with truncation for long payloads
            if len(payload) > 50:
                log_payload = f"{payload[:50]}..."
            else:
                log_payload = payload
            _LOGGER.debug("Processing message: %s = %s", topic, log_payload)

            # Check if this is a response to a command
            if self._is_response_topic(topic):
                await self._handle_command_response(topic, payload)
                return

            # Check if this is the firmware version topic and update device info
            if "m/version" in topic or "metric/m/version" in topic:
                vehicle_id = self.config.get("vehicle_id")
                self._process_version_message(topic, payload)

            # Store in cache
            self.topic_cache[topic] = {
                "payload": payload,
                "timestamp": time.time(),
            }

            # Update device tracker if this is a GPS topic
            if self.has_device_tracker and topic in self.gps_topics:
                # This topic is part of the GPS coordinates, update device tracker
                if "lat" in topic.lower():
                    self.latitude_topic = topic
                elif "lon" in topic.lower():
                    self.longitude_topic = topic

            # Check if this is a new topic
            if topic not in self.discovered_topics:
                self.discovered_topics.add(topic or "")
                _LOGGER.debug("Discovered new topic: %s", topic)
                # Create a new entity for this topic
                await self._async_add_entity_for_topic(topic, payload)
            else:
                # Update existing entity
                entity_id = self.entity_registry.get(topic)
                if entity_id:
                    async_dispatcher_send(
                        self.hass,
                        f"{SIGNAL_UPDATE_ENTITY}_{entity_id}",
                        payload,
                    )
                elif not self._is_system_topic(topic):
                    # Skip logging warning for system topics
                    _LOGGER.warning(
                        "Topic %s in discovered_topics but no entity_id found in registry",
                        topic
                    )
        except Exception as ex:
            _LOGGER.exception("Error processing message: %s", ex)

    def _is_system_topic(self, topic: str) -> bool:
        """Check if topic is a system topic that doesn't need an entity."""
        return (
            topic.endswith("/event") or
            "client/rr/command" in topic or
            "client/rr/response" in topic
        )

    def _is_response_topic(self, topic: str) -> bool:
        """Check if the topic is a response topic."""
        # Extract command ID from response topic pattern
        try:
            pattern = RESPONSE_TOPIC_TEMPLATE.format(
                structure_prefix=self.structure_prefix,
                command_id="(.*)"
            )
            pattern = pattern.replace("+", "[^/]+")  # Replace MQTT wildcard with regex pattern

            match = re.match(pattern, topic)
            return bool(match)
        except Exception as ex:
            _LOGGER.exception("Error checking if topic is response topic: %s", ex)
            return False

    async def _handle_command_response(self, topic: str, payload: str) -> None:
        """Handle a response to a command."""
        try:
            # Extract command ID from response topic
            pattern = RESPONSE_TOPIC_TEMPLATE.format(
                structure_prefix=self.structure_prefix,
                command_id="(.*)"
            )
            pattern = pattern.replace("+", "[^/]+")  # Replace MQTT wildcard with regex pattern

            match = re.match(pattern, topic)
            if not match:
                _LOGGER.warning("Failed to extract command ID from response topic: %s", topic)
                return

            command_id = match.group(1)
            _LOGGER.debug("Received response for command ID: %s", command_id)

            # Check if we have a pending command with this ID
            if command_id in self.pending_commands:
                _LOGGER.debug("Found pending command for ID: %s", command_id)
                future = self.pending_commands[command_id]["future"]
                if not future.done():
                    future.set_result(payload)

                # Clean up
                if command_id in self.pending_commands:
                    del self.pending_commands[command_id]
            else:
                _LOGGER.debug("No pending command found for ID: %s", command_id)
        except Exception as ex:
            _LOGGER.exception("Error handling command response: %s", ex)

    async def _async_add_entity_for_topic(self, topic, payload) -> None:
        """Add a new entity for a discovered topic."""
        try:
            # Extract the entity type and name from the topic
            entity_type, entity_info = self._parse_topic(topic)
            _LOGGER.debug("Parse topic result: entity_type=%s, entity_info=%s", entity_type, entity_info)

            if not entity_type:
                _LOGGER.debug("Could not determine entity type for topic: %s", topic)
                return

            if not entity_info or not isinstance(entity_info, dict):
                _LOGGER.debug("Invalid entity info for topic: %s", topic)
                return

            _LOGGER.info(
                "Adding new entity for topic: %s (type: %s, name: %s)",
                topic, entity_type, entity_info['name']
            )

            # Track entity types for diagnostics
            if entity_type not in self.entity_types:
                self.entity_types[entity_type] = 0

            self.entity_types[entity_type] += 1

            # Create a unique name to prevent collisions
            # Use a hash of the full topic to ensure uniqueness
            topic_hash = hashlib.md5(topic.encode()).hexdigest()[:8]

            # Extract topic components for better naming
            topic_parts = topic.split('/')

            # Get vehicle ID for entity naming
            vehicle_id = self.config.get(CONF_VEHICLE_ID).lower()

            # Determine the entity type category (metric, status, location, etc.)
            entity_category = "unknown"
            original_name = entity_info["name"]

            # Extract category from topic path
            if len(topic_parts) >= 4:
                for part in topic_parts:
                    if part.lower() in ["metric", "status", "location", "notify", "command"]:
                        entity_category = part.lower()
                        break

            # Extract the actual metric path
            # Extract the actual metric path (everything after the category)
            metric_path = ""
            metric_parts = []
            try:
                category_index = topic_parts.index(entity_category)
                if category_index < len(topic_parts) - 1:
                    metric_parts = [p for p in topic_parts[category_index + 1:] if p]
                    metric_path = "_".join(metric_parts)
            except ValueError:
                # If category not found in topic, use original name
                metric_path = original_name
                metric_parts = original_name.split('_')

            # Create entity ID with proper format
            entity_name = f"ovms_{vehicle_id}_{entity_category}_{metric_path}".lower()

            # Check for existing entities with similar names
            similar_name_count = 0
            for eid in self.entity_registry.values():
                if eid.startswith(f"ovms_{vehicle_id}_{entity_category}_{metric_path}"):
                    similar_name_count += 1

            # If this is a duplicate, append a number
            if similar_name_count > 0:
                entity_name = f"{entity_name}_{similar_name_count}"

            # Create a user-friendly name based on metric path
            friendly_name = self._create_friendly_name(metric_parts, entity_category)

            # Use the original vehicle ID for unique IDs for consistency
            original_vehicle_id = self.config.get(
                CONF_ORIGINAL_VEHICLE_ID,
                self.config.get(CONF_VEHICLE_ID)
            )
            unique_id = f"{original_vehicle_id}_{entity_category}_{metric_path}_{topic_hash}"

            # Register this entity
            self.entity_registry[topic] = unique_id

            # Create entity data
            entity_data = {
                "topic": topic,
                "payload": payload,
                "entity_type": entity_type,
                "unique_id": unique_id,
                "name": entity_name,
                "friendly_name": friendly_name,
                "device_info": self._get_device_info(),
                "attributes": entity_info["attributes"],
            }

            # Validate entity info before returning
            if not entity_info or not isinstance(entity_info, dict):
                _LOGGER.warning("Created invalid entity info: %s", entity_info)
                # Create a minimal valid entity info as fallback
                entity_info = {
                    "name": entity_name or "unknown",
                    "friendly_name": friendly_name or "Unknown Sensor",
                    "attributes": entity_info["attributes"] or {},
                }

            _LOGGER.debug("Final entity info: %s", entity_info)
            log_msg = f"Parsed topic as: type={entity_type}, name={entity_info['name']}, "
            log_msg += f"category={entity_category}, friendly_name={entity_info['friendly_name']}"
            _LOGGER.debug(log_msg)

            # If platforms are loaded, send the entity to be created
            # Otherwise, queue it for later
            if self.platforms_loaded:
                async_dispatcher_send(
                    self.hass,
                    SIGNAL_ADD_ENTITIES,
                    entity_data,
                )
            else:
                _LOGGER.debug("Platforms not yet loaded, queuing entity: %s", entity_info["name"])
                await self.entity_queue.put(entity_data)

            # Check if this is a latitude or longitude topic for device tracker
            if "latitude" in topic.lower() or "lat" in topic.lower():
                self.latitude_topic = topic
                self.gps_topics.append(topic)
            elif "longitude" in topic.lower() or "lon" in topic.lower() or "lng" in topic.lower():
                self.longitude_topic = topic
                self.gps_topics.append(topic)
        except Exception as ex:
            _LOGGER.exception("Error adding entity for topic: %s", ex)

    def _create_friendly_name(self, metric_parts, entity_category):
        """Create a user-friendly name based on metric path."""
        try:
            if not metric_parts:
                return "Unknown"

            # Basic input validation
            try:
                # Make sure all parts are strings
                metric_parts = [str(part) for part in metric_parts if part]
            except (TypeError, ValueError):
                _LOGGER.warning("Invalid metric parts: %s", metric_parts)
                return "Unknown"

            if not metric_parts:
                return "Unknown"

            # Convert topic parts to metric path format (dot notation)
            metric_path = ".".join(metric_parts)

            # Check if this is a known metric
            metric_info = get_metric_by_path(metric_path)

            # Try pattern matching if no exact match
            if not metric_info and metric_parts:
                metric_info = get_metric_by_pattern(metric_parts)

            if metric_info and "name" in metric_info:
                return metric_info['name']

            # If no metric definition found, use the original method for fallback naming
            if len(metric_parts) == 0:
                return "Unknown"

            # Return the last part as a title without the category
            last_part = metric_parts[-1].replace("_", " ").title()
            return last_part
        except Exception as ex:
            _LOGGER.exception("Error creating friendly name: %s", ex)
            return "Unknown"

    async def _async_platforms_loaded(self) -> None:
        """Handle platforms loaded signal."""
        try:
            queued_count = self.entity_queue.qsize()
            _LOGGER.info("All platforms loaded, processing %d queued entities", queued_count)
            self.platforms_loaded = True

            # Process any queued entities
            while not self.entity_queue.empty():
                entity_data = await self.entity_queue.get()
                _LOGGER.debug("Processing queued entity: %s", entity_data["name"])
                async_dispatcher_send(
                    self.hass,
                    SIGNAL_ADD_ENTITIES,
                    entity_data,
                )
                self.entity_queue.task_done()

            # Try to subscribe again just in case initial subscription failed
            self._subscribe_topics()

            # If we have no discovered topics, try to discover by sending a test command
            if not self.discovered_topics and self.connected:
                _LOGGER.info("No topics discovered yet, trying to discover by sending a test command")
                try:
                    # Use a generic discovery command
                    command_id = uuid.uuid4().hex[:8]
                    command_topic = COMMAND_TOPIC_TEMPLATE.format(
                        structure_prefix=self.structure_prefix,
                        command_id=command_id
                    )
                    _LOGGER.debug("Sending test command to %s", command_topic)
                    self.client.publish(
                        command_topic,
                        "stat",
                        qos=self.config.get(CONF_QOS, 1)
                    )
                except Exception as ex:  # pylint: disable=broad-except
                    _LOGGER.warning("Error sending discovery command: %s", ex)

            # Start entity discovery
            await self._async_discover_entities()

            # Try to create a device tracker if GPS topics are found
            vehicle_id = self.config.get(CONF_VEHICLE_ID, "")
            if vehicle_id and not self.has_device_tracker:
                await self.create_device_tracker_from_sensors(vehicle_id)
        except Exception as ex:
            _LOGGER.exception("Error in platforms loaded handler: %s", ex)

    # pylint: disable=too-many-return-statements
    def _parse_topic(self, topic) -> Tuple[Optional[str], Optional[Dict]]:
        """Parse a topic to determine the entity type and info."""
        try:
            _LOGGER.debug("Parsing topic: %s", topic)

            # Special handling for status topic (module online status)
            if topic.endswith("/status"):
                vehicle_id = self.config.get(CONF_VEHICLE_ID, "")
                attributes = {"topic": topic, "category": "diagnostic"}
                return "sensor", {
                    "name": f"ovms_{vehicle_id}_status",
                    "friendly_name": f"OVMS {vehicle_id} Connection",
                    "attributes": attributes,
                }

            # Skip event topics - we don't need entities for these
            if topic.endswith("/event"):
                return None, None

            # Check if topic matches our structure prefix
            if not topic.startswith(self.structure_prefix):
                # Alternative check for different username pattern but same vehicle ID
                vehicle_id = self.config.get(CONF_VEHICLE_ID, "")
                prefix = self.config.get(CONF_TOPIC_PREFIX, "")
                if vehicle_id and prefix and f"/{vehicle_id}/" in topic and topic.startswith(prefix):
                    _LOGGER.debug("Topic doesn't match structure prefix but contains vehicle ID")
                    # Extract parts after vehicle ID
                    parts = topic.split(f"/{vehicle_id}/", 1)
                    if len(parts) > 1:
                        topic_suffix = parts[1]
                    else:
                        _LOGGER.debug("Couldn't extract topic suffix after vehicle ID")
                        return None, None
                else:
                    _LOGGER.debug("Topic does not match structure prefix: %s", self.structure_prefix)
                    return None, None
            else:
                # Extract the normal way
                topic_suffix = topic[len(self.structure_prefix):].lstrip('/')

            if not topic_suffix:
                _LOGGER.debug("Empty topic suffix after removing prefix")
                return None, None

            _LOGGER.debug("Topic suffix after removing prefix: %s", topic_suffix)

            # Split the remaining path into parts
            parts = topic_suffix.split("/")
            _LOGGER.debug("Topic parts: %s", parts)
            parts = [p for p in parts if p]
            _LOGGER.debug("Filtered topic parts: %s", parts)

            if len(parts) < 2:
                _LOGGER.debug("Topic has too few parts: %s", parts)
                return None, None

            # Check if this is a command/response topic - don't create entities for these
            if ('client/rr/command' in topic_suffix or 'client/rr/response' in topic_suffix):
                _LOGGER.debug("Skipping command/response topic: %s", topic)
                return None, None

            # Handle vendor-specific prefixes (like xvu)
            if len(parts) >= 2 and parts[0] == "metric":
                # Check for known vendor prefixes
                if parts[1] == "xvu":
                    # This is a vendor-specific metric, adjust parts to match standard pattern
                    if len(parts) > 3:
                        # Create a modified path that might match standard metrics
                        standard_parts = ["metric", parts[2]] + parts[3:]
                        _LOGGER.debug("Converting vendor-specific path to standard: %s", standard_parts)
                        metric_path = ".".join(standard_parts[1:])
                    else:
                        metric_path = ".".join(parts[1:])
                else:
                    metric_path = ".".join(parts[1:])
            else:
                metric_path = ".".join(parts)

            _LOGGER.debug("Metric path for lookup: %s", metric_path)

            # Try to match with known metric patterns
            # First, check if this is a standard OVMS metric
            metric_info = get_metric_by_path(metric_path)

            # If no exact match, try to match by patterns
            if not metric_info and parts:
                metric_info = get_metric_by_pattern(parts)

            # Determine entity type and category
            entity_type = "sensor"  # Default type
            category = determine_category_from_topic(parts)

            # Create a default name if parts exist
            name = "_".join(parts) if parts else "unknown"

            # Check if this should be a binary sensor
            if self._should_be_binary_sensor(parts, name, metric_path, metric_info):
                return "binary_sensor", {
                    "name": name,
                    "friendly_name": create_friendly_name(parts, metric_info),
                    "attributes": self._prepare_attributes(topic, category, parts, metric_info),
                }

            # Check for commands/switches
            if "command" in parts or any(
                switch_pattern in name.lower() for switch_pattern in
                ["switch", "toggle", "set", "enable", "disable"]
            ):
                return "switch", {
                    "name": name,
                    "friendly_name": create_friendly_name(parts, metric_info),
                    "attributes": self._prepare_attributes(topic, category, parts, metric_info),
                }

            # Check for location topics for device_tracker
            location_keywords = ["latitude", "longitude", "gps", "position", "location"]
            if any(keyword in name.lower() for keyword in location_keywords):
                return "device_tracker", {
                    "name": name,
                    "friendly_name": create_friendly_name(parts, metric_info),
                    "attributes": self._prepare_attributes(topic, category, parts, metric_info),
                }

            # Create friendly name
            friendly_name = create_friendly_name(parts, metric_info)

            # Create the entity info for default sensor type
            return entity_type, {
                "name": name,
                "friendly_name": friendly_name,
                "attributes": self._prepare_attributes(topic, category, parts, metric_info),
            }
        except Exception as ex:
            _LOGGER.exception("Error parsing topic: %s", ex)
            return None, None

    def _should_be_binary_sensor(self, parts, name, metric_path, metric_info):
        """Determine if topic should be a binary sensor."""
        try:
            # Check if this is a known binary metric
            if metric_path in BINARY_METRICS:
                return True

            # Check if the metric info defines it as a binary sensor
            if metric_info and "device_class" in metric_info:
                # Check if the device class is from binary_sensor
                if hasattr(metric_info["device_class"], "__module__"):
                    return "binary_sensor" in metric_info["device_class"].__module__

            # Check for binary patterns in name
            name_lower = name.lower()
            binary_keywords = [
                "active", "enabled", "running", "connected",
                "locked", "door", "charging"
            ]

            # Special handling for "on" to avoid false matches
            has_on_word = bool(re.search(r'\bon\b', name_lower))

            # Check for any other binary keywords
            has_binary_keyword = any(keyword in name_lower for keyword in binary_keywords)

            if has_on_word or has_binary_keyword:
                # Exclude certain words that might contain binary keywords but are numeric
                exclusions = [
                    "power", "energy", "duration", "consumption",
                    "acceleration", "direction", "monotonic"
                ]
                # Check if name contains any exclusions
                if not any(exclusion in name_lower for exclusion in exclusions):
                    return True

            return False
        except Exception as ex:
            _LOGGER.exception("Error determining if should be binary sensor: %s", ex)
            return False

    def _prepare_attributes(self, topic, category, parts, metric_info):
        """Prepare entity attributes."""
        try:
            attributes = {
                "topic": topic,
                "category": category,
                "parts": parts,
            }

            # Add additional attributes from metric definition
            if metric_info:
                # Only add attributes that aren't already in the entity definition
                for key, value in metric_info.items():
                    if key not in ["name", "device_class", "state_class", "unit"]:
                        attributes[key] = value

            return attributes
        except Exception as ex:
            _LOGGER.exception("Error preparing attributes: %s", ex)
            return {"topic": topic, "category": category}

    def _get_device_info(self) -> Dict[str, Any]:
        """Get device info for the OVMS module."""
        try:
            vehicle_id = self.config.get(CONF_VEHICLE_ID)

            return {
                "identifiers": {(DOMAIN, vehicle_id)},
                "name": f"OVMS - {vehicle_id}",
                "manufacturer": "Open Vehicles",
                "model": "OVMS Module",
                "sw_version": "Unknown",  # Could be updated if available in MQTT
            }
        except Exception as ex:
            _LOGGER.exception("Error getting device info: %s", ex)
            # Return minimal device info
            return {
                "identifiers": {(DOMAIN, self.config.get(CONF_VEHICLE_ID, "unknown"))},
                "name": f"OVMS - {self.config.get(CONF_VEHICLE_ID, 'unknown')}",
            }

    async def _async_discover_entities(self) -> None:
        """Discover existing entities by probing the MQTT broker."""
        _LOGGER.info("Starting entity discovery process")

        # Wait a bit to allow for initial connections
        await asyncio.sleep(5)

        # Check the topic cache for discovered topics
        if not self.topic_cache and not self.discovered_topics:
            _LOGGER.info("No topics discovered yet, waiting for data")

        # Process is ongoing as we receive messages

    async def async_send_command(
        self, command: str, parameters: str = "",
        command_id: str = None, timeout: int = 10
    ) -> Dict[str, Any]:
        """Send a command to the OVMS module and wait for a response."""
        if not self.connected:
            _LOGGER.error("Cannot send command, not connected to MQTT broker")
            return {"success": False, "error": "Not connected to MQTT broker"}

        # Check rate limiter
        if not self.command_limiter.can_call():
            time_to_next = self.command_limiter.time_to_next_call()
            _LOGGER.warning(
                "Command rate limit exceeded. Try again in %.1f seconds",
                time_to_next
            )
            return {
                "success": False,
                "error": f"Rate limit exceeded. Try again in {time_to_next:.1f} seconds",
                "command": command,
                "parameters": parameters,
            }

        if command_id is None:
            command_id = uuid.uuid4().hex[:8]

        _LOGGER.debug(
            "Sending command: %s, parameters: %s, command_id: %s",
            command, parameters, command_id
        )

        try:
            # Format the command topic
            command_topic = COMMAND_TOPIC_TEMPLATE.format(
                structure_prefix=self.structure_prefix,
                command_id=command_id
            )

            # Prepare the payload
            payload = command
            if parameters:
                payload = f"{command} {parameters}"

            # Create a future to wait for the response
            loop = asyncio.get_running_loop()
            future = loop.create_future()

            # Store the future for the response handler
            self.pending_commands[command_id] = {
                "future": future,
                "timestamp": time.time(),
                "command": command,
                "parameters": parameters,
            }

            # Send the command
            _LOGGER.debug("Publishing command to %s: %s", command_topic, payload)
            self.client.publish(command_topic, payload, qos=self.config.get(CONF_QOS))

            # Wait for the response with timeout
            _LOGGER.debug("Waiting for response for command_id: %s", command_id)
            response_payload = await asyncio.wait_for(future, timeout)

            _LOGGER.debug("Received response: %s", response_payload)

            # Try to parse the response as JSON
            response_data = None
            try:
                response_data = json.loads(response_payload)
            except json.JSONDecodeError:
                # Not JSON, just use the raw payload
                response_data = response_payload

            # Make sure response is serializable before returning
            serializable_response = ensure_serializable(response_data)

            return {
                "success": True,
                "command_id": command_id,
                "command": command,
                "parameters": parameters,
                "response": serializable_response,
            }

        except asyncio.TimeoutError:
            _LOGGER.warning("Command timed out: %s (ID: %s)", command, command_id)
            # Clean up
            if command_id in self.pending_commands:
                del self.pending_commands[command_id]

            return {
                "success": False,
                "error": "Timeout waiting for response",
                "command_id": command_id,
                "command": command,
                "parameters": parameters,
            }

        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.exception("Error sending command: %s", ex)
            # Clean up
            if command_id in self.pending_commands:
                del self.pending_commands[command_id]

            return {
                "success": False,
                "error": str(ex),
                "command_id": command_id,
                "command": command,
                "parameters": parameters,
            }

    async def async_shutdown(self) -> None:
        """Shutdown the MQTT client."""
        _LOGGER.info("Shutting down MQTT client")

        # Set the shutdown flag to prevent reconnection attempts
        self._shutting_down = True

        # Cancel the cleanup task
        if hasattr(self, '_cleanup_task') and self._cleanup_task:
            try:
                self._cleanup_task.cancel()
                await asyncio.wait_for(asyncio.shield(self._cleanup_task), timeout=2)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            except Exception as ex:
                _LOGGER.exception("Error cancelling cleanup task: %s", ex)

        if self.client:
            _LOGGER.debug("Stopping MQTT client loop")
            try:
                # Try to publish offline status before disconnecting
                if self._status_topic and self.connected:
                    _LOGGER.debug("Publishing offline status")
                    self.client.publish(
                        self._status_topic,
                        "offline",
                        qos=self.config.get(CONF_QOS, 1),
                        retain=True
                    )

                # Stop the loop and disconnect
                self.client.loop_stop()
                await self.hass.async_add_executor_job(self.client.disconnect)
                _LOGGER.debug("MQTT client disconnected")
            except Exception as ex:
                _LOGGER.exception("Error stopping MQTT client: %s", ex)

    async def create_device_tracker_from_sensors(self, vehicle_id: str) -> None:
        """Create a device tracker entity based on latitude/longitude sensors."""
        try:
            _LOGGER.info("Creating device tracker for vehicle: %s", vehicle_id)

            # Search for latitude and longitude topics in discovered topics
            lat_topic = None
            lon_topic = None

            # Patterns to search for in discovered topics
            lat_patterns = ["latitude", "lat"]
            lon_patterns = ["longitude", "lon", "lng"]

            # Find matching topics
            for topic in self.discovered_topics:
                topic_lower = topic.lower()

                # Check for latitude topics
                if any(pattern in topic_lower for pattern in lat_patterns):
                    lat_topic = topic
                    self.latitude_topic = topic
                    _LOGGER.debug("Found latitude topic: %s", lat_topic)

                # Check for longitude topics
                if any(pattern in topic_lower for pattern in lon_patterns):
                    lon_topic = topic
                    self.longitude_topic = topic
                    _LOGGER.debug("Found longitude topic: %s", lon_topic)

                # If we found both, we can stop searching
                if lat_topic and lon_topic:
                    break

            if not (lat_topic and lon_topic):
                _LOGGER.warning("Could not find latitude and longitude topics. Device tracker will not be created.")
                return

            # Get current values if available
            lat_value = self.topic_cache.get(lat_topic, {}).get("payload", "unknown")
            lon_value = self.topic_cache.get(lon_topic, {}).get("payload", "unknown")

            _LOGGER.debug("Current values - Latitude: %s, Longitude: %s", lat_value, lon_value)

            # Create device info
            device_info = self._get_device_info()

            # Create a unique ID for the device tracker
            device_tracker_id = f"{vehicle_id}_location"

            # Store sensor IDs for direct updates
            self.lat_sensor_id = f"{vehicle_id}_latitude_sensor"
            self.lon_sensor_id = f"{vehicle_id}_longitude_sensor"

            # Create entity data for the device tracker
            tracker_entity_data = {
                "entity_type": "device_tracker",
                "unique_id": device_tracker_id,
                "name": f"ovms_{vehicle_id}_location",
                "friendly_name": f"{vehicle_id} Location",
                "topic": "combined_location",  # This is a virtual topic
                "payload": {"latitude": 0, "longitude": 0},  # Initial values will be updated from sensors
                "device_info": device_info,
                "attributes": {
                    "category": "location",
                    "lat_topic": lat_topic,
                    "lon_topic": lon_topic,
                },
            }

            # Create entity data for individual sensor versions of latitude and longitude
            lat_sensor_data = {
                "entity_type": "sensor",
                "unique_id": self.lat_sensor_id,
                "name": f"ovms_{vehicle_id}_latitude",
                "friendly_name": f"{vehicle_id} Latitude",
                "topic": lat_topic,
                "payload": lat_value,
                "device_info": device_info,
                "attributes": {"category": "location"},
            }

            lon_sensor_data = {
                "entity_type": "sensor",
                "unique_id": self.lon_sensor_id,
                "name": f"ovms_{vehicle_id}_longitude",
                "friendly_name": f"{vehicle_id} Longitude",
                "topic": lon_topic,
                "payload": lon_value,
                "device_info": device_info,
                "attributes": {"category": "location"},
            }

            # Add the entities using the dispatcher
            async_dispatcher_send(
                self.hass,
                SIGNAL_ADD_ENTITIES,
                tracker_entity_data,
            )

            # Also create standalone sensor versions of latitude and longitude
            _LOGGER.info("Creating standalone sensor versions of GPS coordinates")
            async_dispatcher_send(
                self.hass,
                SIGNAL_ADD_ENTITIES,
                lat_sensor_data,
            )

            async_dispatcher_send(
                self.hass,
                SIGNAL_ADD_ENTITIES,
                lon_sensor_data,
            )

            # Mark that we've created a device tracker
            self.has_device_tracker = True
            self.gps_topics = [lat_topic, lon_topic]

            # Set up watchers for these topics to update the device tracker
            _LOGGER.info("Setting up GPS topic watchers for device tracker")
            self._watch_gps_topics(device_tracker_id)
        except Exception as ex:
            _LOGGER.exception("Error creating device tracker: %s", ex)

    def _watch_gps_topics(self, device_tracker_id: str) -> None:
        """Set up watchers for GPS topics to update the device tracker."""
        # Store previous coordinates to avoid unnecessary updates
        self._prev_latitude = None
        self._prev_longitude = None
        self._last_gps_update = 0
        
        @callback
        def gps_topics_updated(event_type=None, data=None) -> None:
            """Handle updates to GPS topics."""
            try:
                if not self.latitude_topic or not self.longitude_topic:
                    return

                # Get current values
                lat_value = self.topic_cache.get(self.latitude_topic, {}).get("payload", "unknown")
                lon_value = self.topic_cache.get(self.longitude_topic, {}).get("payload", "unknown")
                
                # Skip if either value is unknown/unavailable
                if lat_value in ("unknown", "unavailable", "") or lon_value in ("unknown", "unavailable", ""):
                    return

                try:
                    # Parse as valid numbers
                    lat_float = float(lat_value)
                    lon_float = float(lon_value)

                    # Skip if not in valid range
                    if not (-90 <= lat_float <= 90) or not (-180 <= lon_float <= 180):
                        return

                    # Check if coordinates have changed
                    coordinates_changed = (
                        self._prev_latitude is None or
                        self._prev_longitude is None or
                        abs(lat_float - self._prev_latitude) > 0.00001 or
                        abs(lon_float - self._prev_longitude) > 0.00001
                    )
                    
                    current_time = time.time()
                    time_since_last_update = current_time - self._last_gps_update
                    
                    # Only update if:
                    # 1. Coordinates have changed, OR
                    # 2. It's been at least 30 seconds since the last update
                    update_needed = coordinates_changed or time_since_last_update >= 30
                    
                    if update_needed:
                        # Store new values and update timestamp
                        self._prev_latitude = lat_float
                        self._prev_longitude = lon_float
                        self._last_gps_update = current_time

                        # Find and get GPS signal quality/accuracy
                        gps_sq_topic = None
                        for topic in self.discovered_topics:
                            if "gpssq" in topic.lower() or "gps_sq" in topic.lower() or "gps/sq" in topic.lower():
                                gps_sq_topic = topic
                                break
                                
                        gps_accuracy = 0  # Default value
                        if gps_sq_topic:
                            gps_sq_value = self.topic_cache.get(gps_sq_topic, {}).get("payload", "0")
                            try:
                                gps_accuracy = float(gps_sq_value)
                            except (ValueError, TypeError):
                                pass

                        # Create payload
                        payload = {
                            "latitude": lat_float,
                            "longitude": lon_float,
                            "gps_accuracy": gps_accuracy,
                            "last_updated": current_time
                        }

                        # Send update to device tracker
                        async_dispatcher_send(
                            self.hass,
                            f"{SIGNAL_UPDATE_ENTITY}_{device_tracker_id}",
                            payload,
                        )

                        # Also update the individual latitude and longitude sensors
                        # This is critical - ensure sensors get updates even if they weren't directly
                        # updated via their topics
                        if self.lat_sensor_id:
                            async_dispatcher_send(
                                self.hass,
                                f"{SIGNAL_UPDATE_ENTITY}_{self.lat_sensor_id}",
                                str(lat_float),  # Convert to string for consistency
                            )
                        
                        if self.lon_sensor_id:
                            async_dispatcher_send(
                                self.hass,
                                f"{SIGNAL_UPDATE_ENTITY}_{self.lon_sensor_id}",
                                str(lon_float),  # Convert to string for consistency
                            )

                        _LOGGER.debug("Updated GPS coordinates: lat=%s, lon=%s (changed=%s, time_since=%d)",
                                     lat_float, lon_float, coordinates_changed, time_since_last_update)
                    else:
                        _LOGGER.debug("Skipping GPS update (no change and only %d seconds since last update)",
                                     time_since_last_update)

                except (ValueError, TypeError) as ex:
                    _LOGGER.debug("Error parsing GPS values: %s, %s - %s", lat_value, lon_value, ex)
                    
            except Exception as ex:
                _LOGGER.exception("Error updating GPS topics: %s", ex)

        # Register for message reception only - avoid duplicate processing
        @callback
        def message_received(topic, payload, qos):
            """Handle message received."""
            try:
                if topic in self.gps_topics:
                    gps_topics_updated()
                # Also check for GPS signal quality topics
                elif "gpssq" in topic.lower() or "gps/sq" in topic.lower() or "gps_sq" in topic.lower():
                    gps_topics_updated()
            except Exception as ex:
                _LOGGER.exception("Error in message received handler: %s", ex)

        self.hass.bus.async_listen("mqtt_message_received", message_received)

        # Do an initial update to populate the sensors
        gps_topics_updated()
        
        # Ensure regular updates even when coordinates don't change
        # This ensures sensors don't go to "unknown" state
        async def periodic_check():
            """Periodically check GPS values to avoid sensors going to unknown state."""
            try:
                while not self._shutting_down:
                    # Check every 60 seconds
                    await asyncio.sleep(60)
                    if self._shutting_down:
                        break
                
                    # Get current sensor states
                    lat_value = self.topic_cache.get(self.latitude_topic, {}).get("payload", "unknown")
                    lon_value = self.topic_cache.get(self.longitude_topic, {}).get("payload", "unknown")
            
                    # Only update if either sensor is unknown
                    if lat_value in ("unknown", "unavailable", "") or lon_value in ("unknown", "unavailable", ""):
                        _LOGGER.debug("Sensor(s) in unknown state, forcing update")
                        gps_topics_updated()
            except asyncio.CancelledError:
                pass
            except Exception as ex:
                _LOGGER.exception("Error in periodic GPS check: %s", ex)
        # Start the periodic updater task
        self.hass.loop.create_task(periodic_check())
