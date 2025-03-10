"""MQTT Client for OVMS Integration."""
import asyncio
import logging
import re
import time
import json
import uuid
import hashlib
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union
from collections import deque

import paho.mqtt.client as mqtt

from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_PROTOCOL,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant
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
    TOPIC_WILDCARD,
)
from .rate_limiter import CommandRateLimiter
from .metrics import (
    METRIC_DEFINITIONS,
    TOPIC_PATTERNS,
    METRIC_CATEGORIES,
    BINARY_METRICS,
    PREFIX_CATEGORIES,
    get_metric_by_path,
    get_metric_by_pattern,
    determine_category_from_topic,
    create_friendly_name,
)

_LOGGER = logging.getLogger(LOGGER_NAME)

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
        except:
            return str(obj)   # Fall back to string representation
    else:
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
        self.entity_queue = deque()
        self.platforms_loaded = False

        # Status tracking
        self._status_topic = None
        self._connected_payload = "online"

    async def async_setup(self) -> bool:
        """Set up the MQTT client."""
        _LOGGER.debug("Setting up MQTT client")

        # Initialize empty tracking sets/dictionaries
        self.discovered_topics = set()
        self.entity_registry = {}
        self.entity_types = {}
        self.topic_cache = {}

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
        except Exception as e:
            _LOGGER.error("Failed to create MQTT client: %s", e)
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
            except (TypeError, AttributeError) as e:
                _LOGGER.debug("Failed to set MQTT v5 properties: %s", e)
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
            _LOGGER.info("Disconnected from MQTT broker: %s", rc)
            self.connected = False
            self.reconnect_count += 1
            self.last_reconnect_time = time.time()

            # Schedule reconnection if not intentional disconnect
            if rc != 0:
                _LOGGER.warning("Unintentional disconnect. Scheduling reconnection attempt.")
                asyncio.run_coroutine_threadsafe(
                    self._async_reconnect(),
                    self.hass.loop,
                )

        def on_message(client, userdata, msg):
            """Handle incoming messages."""
            self.message_count += 1
            _LOGGER.debug("Message #%d received on topic: %s (payload len: %d)",
                         self.message_count, msg.topic, len(msg.payload))

            # Try to decode payload for debug logging
            if _LOGGER.isEnabledFor(logging.DEBUG):
                try:
                    payload_str = msg.payload.decode('utf-8')
                    _LOGGER.debug("Payload: %s", payload_str[:200] + "..." if len(payload_str) > 200 else payload_str)
                except UnicodeDecodeError:
                    _LOGGER.debug("Payload: <binary data>")

            # Process the message
            asyncio.run_coroutine_threadsafe(
                self._async_process_message(msg),
                self.hass.loop,
            )

        def on_subscribe(client, userdata, mid, granted_qos, properties=None):
            """Handle subscription confirmation."""
            # Convert ReasonCodes to a serializable format
            serialized_qos = ensure_serializable(granted_qos)
            _LOGGER.debug("Subscription confirmed. MID: %s, QoS: %s", mid, serialized_qos)

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
                on_subscribe(client, userdata, mid, granted_qos, None)
            self.client.on_subscribe = on_subscribe_v311

    def _on_connect_callback(self, client, userdata, flags, rc):
        """Common connection callback for different MQTT versions."""
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
            # Re-subscribe if we get disconnected
            self._subscribe_topics()

            # Publish online status when connected
            if self._status_topic:
                client.publish(self._status_topic, self._connected_payload,
                              qos=self.config.get(CONF_QOS, 1), retain=True)
        else:
            self.connected = False
            _LOGGER.error("Failed to connect to MQTT broker: %s", rc)

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
        # Calculate backoff time based on reconnect count
        backoff = min(30, 2 ** min(self.reconnect_count, 5))  # Cap at 30 seconds
        _LOGGER.info("Reconnecting in %d seconds (attempt #%d)", backoff, self.reconnect_count)

        await asyncio.sleep(backoff)

        if not self.connected:  # Avoid reconnecting if we're already connected
            try:
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
                asyncio.create_task(self._async_reconnect())

    async def _async_cleanup_pending_commands(self) -> None:
        """Periodically clean up timed-out command requests."""
        try:
            while True:
                # Run every 60 seconds
                await asyncio.sleep(60)

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
                        future.set_exception(asyncio.TimeoutError("Command expired during cleanup"))
                    del self.pending_commands[command_id]

                _LOGGER.debug("Cleaned up %d expired commands", len(expired_commands))

        except asyncio.CancelledError:
            _LOGGER.debug("Command cleanup task cancelled")
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.exception("Error in command cleanup task: %s", ex)

    def _subscribe_topics(self) -> None:
        """Subscribe to the OVMS topics."""
        if not self.connected:
            _LOGGER.warning("Cannot subscribe to topics, not connected")
            return

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

    def _format_structure_prefix(self) -> str:
        """Format the topic structure prefix based on configuration."""
        structure = self.config.get(CONF_TOPIC_STRUCTURE, DEFAULT_TOPIC_STRUCTURE)
        prefix = self.config.get(CONF_TOPIC_PREFIX)
        vehicle_id = self.config.get(CONF_VEHICLE_ID)
        mqtt_username = self.config.get(CONF_MQTT_USERNAME, "")

        # If username format appears inconsistent, try to adapt
        if mqtt_username and vehicle_id and mqtt_username.lower() != f"ovms-mqtt-{vehicle_id.lower()}":
            # If username doesn't already contain vehicle ID, consider adding it for better pattern matching
            if vehicle_id.lower() not in mqtt_username.lower():
                alternative_username = f"ovms-mqtt-{vehicle_id.lower()}"
                _LOGGER.debug("Username %s may not match pattern. Also trying %s",
                             mqtt_username, alternative_username)
                # Don't replace the username yet, just log the possibility

        # Replace the variables in the structure
        structure_prefix = structure.format(
            prefix=prefix,
            vehicle_id=vehicle_id,
            mqtt_username=mqtt_username
        )

        _LOGGER.debug("Formatted structure prefix: %s", structure_prefix)
        return structure_prefix

    async def _async_process_message(self, msg) -> None:
        """Process an incoming MQTT message."""
        topic = msg.topic
        try:
            payload = msg.payload.decode("utf-8")
        except UnicodeDecodeError:
            _LOGGER.warning("Failed to decode payload for topic %s", topic)
            payload = "<binary data>"

        _LOGGER.debug("Processing message: %s = %s", topic, payload[:50] + "..." if len(payload) > 50 else payload)

        # Check if this is a response to a command
        if self._is_response_topic(topic):
            await self._handle_command_response(topic, payload)
            return

        # Check if this is the firmware version topic and update device info
        if "metric/m/version" in topic:
            _LOGGER.debug("Firmware version received: %s", payload)
            device_id = self.config.get(CONF_VEHICLE_ID)
            self._update_firmware_version(device_id, payload)

        # Store in cache
        self.topic_cache[topic] = {
            "payload": payload,
            "timestamp": time.time(),
        }

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
            elif not topic.endswith("/event") and "client/rr/command" not in topic and "client/rr/response" not in topic:
                # Skip logging warning for event, command, and response topics
                _LOGGER.warning("Topic %s in discovered_topics but no entity_id found in registry", topic)

    def _update_firmware_version(self, device_id: str, version: str) -> None:
        """Update the firmware version in the device registry."""
        try:
            device_registry = dr.async_get(self.hass)
            device_entry = device_registry.async_get_device({(DOMAIN, device_id)})
            if device_entry:
                device_registry.async_update_device(
                    device_entry.id, sw_version=version
                )
                _LOGGER.info("Updated firmware version to %s", version)
        except Exception as ex:
            _LOGGER.error("Error updating firmware version: %s", ex)

    def _is_response_topic(self, topic: str) -> bool:
        """Check if the topic is a response topic."""
        # Extract command ID from response topic pattern
        pattern = RESPONSE_TOPIC_TEMPLATE.format(
            structure_prefix=self.structure_prefix,
            command_id="(.*)"
        )
        pattern = pattern.replace("+", "[^/]+")  # Replace MQTT wildcard with regex pattern

        match = re.match(pattern, topic)
        return bool(match)

    async def _handle_command_response(self, topic: str, payload: str) -> None:
        """Handle a response to a command."""
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

    async def _async_add_entity_for_topic(self, topic, payload) -> None:
        """Add a new entity for a discovered topic."""
        # Extract the entity type and name from the topic
        entity_type, entity_info = self._parse_topic(topic)
        _LOGGER.debug("Parse topic result: entity_type=%s, entity_info=%s", entity_type, entity_info)

        if not entity_type:
            _LOGGER.debug("Could not determine entity type for topic: %s", topic)
            return

        if not entity_info or not isinstance(entity_info, dict):
            _LOGGER.debug("Invalid entity info for topic: %s", topic)
            return

        _LOGGER.info("Adding new entity for topic: %s (type: %s, name: %s)",
                    topic, entity_type, entity_info['name'])

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

        # Create entity ID with proper format: sensor.ovms_{vehicle_id}_{category}_{metric_path}
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
        original_vehicle_id = self.config.get(CONF_ORIGINAL_VEHICLE_ID, self.config.get(CONF_VEHICLE_ID))
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
        _LOGGER.debug("Parsed topic as: type=%s, name=%s, category=%s, friendly_name=%s",
                    entity_type, entity_info['name'], entity_category, entity_info['friendly_name'])

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
            self.entity_queue.append(entity_data)

    def _create_friendly_name(self, metric_parts, entity_category):
        """Create a user-friendly name based on metric path."""
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

    async def _async_platforms_loaded(self) -> None:
        """Handle platforms loaded signal."""
        _LOGGER.info("All platforms loaded, processing %d queued entities", len(self.entity_queue))
        self.platforms_loaded = True

        # Process any queued entities
        queued_entities = list(self.entity_queue)
        for entity_data in queued_entities:
            entity_data = self.entity_queue.popleft()
            _LOGGER.debug("Processing queued entity: %s", entity_data["name"])
            async_dispatcher_send(
                self.hass,
                SIGNAL_ADD_ENTITIES,
                entity_data,
            )

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
                response_topic = RESPONSE_TOPIC_TEMPLATE.format(
                    structure_prefix=self.structure_prefix,
                    command_id=command_id
                )
                _LOGGER.debug("Sending test command to %s", command_topic)
                self.client.publish(command_topic, "stat", qos=self.config.get(CONF_QOS, 1))
            except Exception as ex:
                _LOGGER.warning("Error sending discovery command: %s", ex)

        # Start entity discovery
        await self._async_discover_entities()

    def _parse_topic(self, topic) -> Tuple[Optional[str], Optional[Dict]]:
        """Parse a topic to determine the entity type and info."""
        _LOGGER.debug("Parsing topic: %s", topic)

        # Special handling for status topic (module online status)
        if topic.endswith("/status"):
            vehicle_id = self.config.get(CONF_VEHICLE_ID, "")
            attributes = {"topic": topic, "category": "diagnostic"}
            return "binary_sensor", {
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
        is_binary = False
        if metric_info and "device_class" in metric_info:
            from homeassistant.components.binary_sensor import BinarySensorDeviceClass
            # Check if the device class is from binary_sensor
            if hasattr(metric_info["device_class"], "__module__") and "binary_sensor" in metric_info["device_class"].__module__:
                is_binary = True
                entity_type = "binary_sensor"

        # Also check if this is a known binary metric
        if metric_path in BINARY_METRICS:
            is_binary = True
            entity_type = "binary_sensor"

        # Check for binary patterns in name - use word boundary only for "on" to avoid false matches
        name_lower = name.lower()
        if (re.search(r'\bon\b', name_lower) or                  # "on" with word boundaries
            "active" in name_lower or
            "enabled" in name_lower or
            "running" in name_lower or
            "connected" in name_lower or
            "locked" in name_lower or
            "door" in name_lower or
            "charging" in name_lower):
            is_binary = True
            entity_type = "binary_sensor"

        # Ensure GPS-related metrics are never binary sensors
        if ("latitude" in name.lower() or "longitude" in name.lower() or
            "gps" in name.lower() or "location" in name.lower()):
            is_binary = False

        # Ensure other numeric data is never binary
        if ("power" in name.lower() or "energy" in name.lower() or
            "duration" in name.lower() or "consumption" in name.lower() or
            "acceleration" in name.lower() or "direction" in name.lower() or
            "monotonic" in name.lower()):
            is_binary = False
            entity_type = "sensor"

        # Check for commands/switches
        if "command" in parts or any(switch_pattern in name.lower() for switch_pattern in
                                ["switch", "toggle", "set", "enable", "disable"]):
            entity_type = "switch"

        # Create friendly name
        friendly_name = create_friendly_name(parts, metric_info)

        # Prepare attributes
        attributes = {
            "topic": topic,
            "category": category,
            "parts": parts,
        }

        # Add additional attributes from metric definition
        if metric_info:
            # Only add attributes that aren't already in the entity definition
            for k, v in metric_info.items():
                if k not in ["name", "device_class", "state_class", "unit"]:
                    attributes[k] = v

        # Create the entity info
        entity_info = {
            "name": name,
            "friendly_name": friendly_name,
            "attributes": attributes,
        }

        # Validate entity info before returning
        if not entity_info or not isinstance(entity_info, dict):
            _LOGGER.warning("Created invalid entity info: %s", entity_info)
            # Create a minimal valid entity info as fallback
            entity_info = {
                "name": name or "unknown",
                "friendly_name": friendly_name or "Unknown Sensor",
                "attributes": attributes or {},
            }

        _LOGGER.debug("Final entity info: %s", entity_info)
        _LOGGER.debug("Parsed topic as: type=%s, name=%s, category=%s, friendly_name=%s",
                    entity_type, entity_info['name'], category, entity_info['friendly_name'])
        return entity_type, entity_info

    def _get_device_info(self) -> Dict[str, Any]:
        """Get device info for the OVMS module."""
        vehicle_id = self.config.get(CONF_VEHICLE_ID)

        return {
            "identifiers": {(DOMAIN, vehicle_id)},
            "name": f"OVMS - {vehicle_id}",
            "manufacturer": "Open Vehicles",
            "model": "OVMS Module",
            "sw_version": "Unknown",  # Could be updated if available in MQTT
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

    async def async_send_command(self, command: str, parameters: str = "",
                                 command_id: str = None, timeout: int = 10) -> Dict[str, Any]:
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

        _LOGGER.debug("Sending command: %s, parameters: %s, command_id: %s",
                     command, parameters, command_id)

        # Format the command topic
        command_topic = COMMAND_TOPIC_TEMPLATE.format(
            structure_prefix=self.structure_prefix,
            command_id=command_id
        )

        # Format the response topic for logging
        response_topic = RESPONSE_TOPIC_TEMPLATE.format(
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

        try:
            # Wait for the response with timeout
            _LOGGER.debug("Waiting for response on %s", response_topic)
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
        # Cancel the cleanup task
        if hasattr(self, '_cleanup_task') and self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        if self.client:
            _LOGGER.info("Shutting down MQTT client")
            self.client.loop_stop()

            await self.hass.async_add_executor_job(self.client.disconnect)
