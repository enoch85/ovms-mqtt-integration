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
from homeassistant.helpers.dispatcher import async_dispatcher_send

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
    TOPIC_TEMPLATE,
    COMMAND_TOPIC_TEMPLATE,
    RESPONSE_TOPIC_TEMPLATE,
    TOPIC_WILDCARD,
)
from .rate_limiter import CommandRateLimiter

_LOGGER = logging.getLogger(LOGGER_NAME)

# Signal constants
SIGNAL_ADD_ENTITIES = f"{DOMAIN}_add_entities"
SIGNAL_UPDATE_ENTITY = f"{DOMAIN}_update_entity"
SIGNAL_PLATFORMS_LOADED = f"{DOMAIN}_platforms_loaded"


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
        
        # Format the topic structure prefix
        self.structure_prefix = self._format_structure_prefix()
        _LOGGER.debug("Using structure prefix: %s", self.structure_prefix)
        
        # Create the MQTT client
        self.client = await self._create_mqtt_client()
        
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
        client = mqtt.Client(client_id=client_id, protocol=protocol)
        
        # Configure authentication if provided
        username = self.config.get(CONF_USERNAME)
        if username:
            password = self.config.get(CONF_PASSWORD)
            _LOGGER.debug("Setting username and password for MQTT client")
            client.username_pw_set(
                username=username,
                password=password,
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
        
    def _format_structure_prefix(self) -> str:
        """Format the topic structure prefix based on configuration."""
        structure = self.config.get(CONF_TOPIC_STRUCTURE, DEFAULT_TOPIC_STRUCTURE)
        prefix = self.config.get(CONF_TOPIC_PREFIX)
        vehicle_id = self.config.get(CONF_VEHICLE_ID)
        mqtt_username = self.config.get(CONF_MQTT_USERNAME, "")
        
        # Replace the variables in the structure
        structure_prefix = structure.format(
            prefix=prefix,
            vehicle_id=vehicle_id,
            mqtt_username=mqtt_username
        )
        
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
        
        # Store in cache
        self.topic_cache[topic] = {
            "payload": payload,
            "timestamp": time.time(),
        }
        
        # Check if this is a new topic
        if topic not in self.discovered_topics:
            self.discovered_topics.add(topic)
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
        
        if not entity_type:
            _LOGGER.debug("Could not determine entity type for topic: %s", topic)
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
        
        # Store original name for display
        entity_info["original_name"] = entity_info["name"]
        
        # Check for existing entities with similar names
        similar_name_count = 0
        for eid in self.entity_registry.values():
            if eid.startswith(f"{self.config.get(CONF_VEHICLE_ID)}_{entity_info['name']}"):
                similar_name_count += 1
        
        # If this is a duplicate, append a number
        if similar_name_count > 0:
            entity_info["name"] = f"{entity_info['name']}_{similar_name_count}"
            
        # Use the original vehicle ID for entity unique IDs for consistency
        original_vehicle_id = self.config.get(CONF_ORIGINAL_VEHICLE_ID, self.config.get(CONF_VEHICLE_ID))
        unique_id = f"{original_vehicle_id}_{entity_info['name']}_{topic_hash}"
        
        # Register this entity
        self.entity_registry[topic] = unique_id
        
        # Create entity data
        entity_data = {
            "topic": topic,
            "payload": payload,
            "entity_type": entity_type,
            "unique_id": unique_id,
            "name": entity_info["name"],
            "device_info": self._get_device_info(),
            "attributes": entity_info.get("attributes", {}),
        }
        
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
        
    async def _async_platforms_loaded(self) -> None:
        """Handle platforms loaded signal."""
        _LOGGER.info("All platforms loaded, processing %d queued entities", len(self.entity_queue))
        self.platforms_loaded = True
        
        # Process any queued entities
        while self.entity_queue:
            entity_data = self.entity_queue.popleft()
            _LOGGER.debug("Processing queued entity: %s", entity_data["name"])
            async_dispatcher_send(
                self.hass,
                SIGNAL_ADD_ENTITIES,
                entity_data,
            )
            
        # Start entity discovery
        await self._async_discover_entities()
        
    def _parse_topic(self, topic) -> Tuple[Optional[str], Optional[Dict]]:
        """Parse a topic to determine the entity type and info."""
        _LOGGER.debug("Parsing topic: %s", topic)
        
        # Check if topic matches our structure prefix
        if not topic.startswith(self.structure_prefix):
            _LOGGER.debug("Topic does not match structure prefix: %s", self.structure_prefix)
            return None, None
            
        # Remove the structure prefix
        topic_suffix = topic[len(self.structure_prefix):].lstrip('/')
        _LOGGER.debug("Topic suffix after removing prefix: %s", topic_suffix)
        
        # Split the remaining path into parts
        parts = topic_suffix.split("/")
        _LOGGER.debug("Topic parts: %s", parts)
        
        if len(parts) < 2:
            _LOGGER.debug("Topic has too few parts: %s", parts)
            return None, None
        
        # More specific classification rules
        # Use regex patterns for more precise matching
        battery_pattern = re.compile(r'battery|soc|charge|energy', re.IGNORECASE)
        temperature_pattern = re.compile(r'temp|temperature', re.IGNORECASE)
        door_pattern = re.compile(r'door|lock|window|trunk|hood', re.IGNORECASE)
        location_pattern = re.compile(r'location|gps|position|coordinates', re.IGNORECASE)
        switch_pattern = re.compile(r'command|toggle|switch|set', re.IGNORECASE)
        binary_pattern = re.compile(r'connected|enabled|active|status|state', re.IGNORECASE)
        
        # First determine the category from the topic parts
        category = "other"
        if any(battery_pattern.search(part) for part in parts):
            category = "battery"
            _LOGGER.debug("Identified as battery category")
        elif any(temperature_pattern.search(part) for part in parts):
            category = "climate"
            _LOGGER.debug("Identified as climate category")
        elif any(door_pattern.search(part) for part in parts):
            category = "door"
            _LOGGER.debug("Identified as door category")
        elif any(location_pattern.search(part) for part in parts):
            category = "location"
            _LOGGER.debug("Identified as location category")
        
        # Now determine entity type based on more specific rules
        if category == "location":
            entity_type = "device_tracker"
            name = "location"
            _LOGGER.debug("Identified as device_tracker")
        elif category == "door" or (category == "battery" and any(binary_pattern.search(part) for part in parts)):
            entity_type = "binary_sensor"
            name = "_".join(parts)
            _LOGGER.debug("Identified as binary_sensor")
        elif any(switch_pattern.search(part) for part in parts):
            entity_type = "switch"
            name = f"command_{parts[-1]}" if parts else "unknown_command"
            _LOGGER.debug("Identified as switch")
        else:
            entity_type = "sensor"
            name = "_".join(parts)
            _LOGGER.debug("Identified as sensor")
            
        # Create the entity info
        entity_info = {
            "name": name,
            "attributes": {
                "topic": topic,
                "category": category,
                "parts": parts,
            },
        }
            
        _LOGGER.debug("Parsed topic as: type=%s, name=%s, category=%s", 
                     entity_type, entity_info['name'], category)
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
        if not self.topic_cache:
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
