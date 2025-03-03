import logging
import json
import re
from datetime import timedelta
from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.components.sensor import SensorEntity
from homeassistant.components import mqtt
from homeassistant.util import slugify
from .const import DOMAIN, CONF_BROKER, CONF_PORT, CONF_USERNAME, CONF_PASSWORD, CONF_TOPIC_PREFIX, CONF_QOS, CONF_DEBUG_LOGGING

_LOGGER = logging.getLogger(__name__)

# Common OVMS metric patterns and friendly names
METRIC_PATTERNS = {
    "v/b/soc": "Battery State of Charge",
    "v/b/range/est": "Estimated Range",
    "v/b/12v/voltage": "12V Battery Voltage",
    "v/b/p/temp/avg": "Battery Temperature",
    "xvu/b/soc/abs": "Absolute Battery SOC",
    "xvu/b/soh/vw": "Battery Health",
    "v/p/latitude": "Latitude",
    "v/p/longitude": "Longitude",
    "v/p/odometer": "Odometer",
    "v/p/gpssq": "GPS Signal Quality",
    "v/c/limit/soc": "Charge Limit",
    "v/c/duration/full": "Full Charge Duration",
    "xvu/c/eff/calc": "Charging Efficiency",
    "xvu/c/ac/p": "AC Charging Power",
    "xvu/e/hv/chgmode": "Charging Mode",
    "v/e/batteryage": "Battery Age",
}

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    """Set up OVMS MQTT sensor platform from a config entry."""
    _LOGGER.info("Setting up OVMS MQTT sensor platform")

    config = entry.data
    topic_prefix = config.get(CONF_TOPIC_PREFIX, "ovms")
    qos = config.get(CONF_QOS, 1)
    debug_mode = config.get(CONF_DEBUG_LOGGING, False)

    # Initialize data structure
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}
    
    hass.data[DOMAIN] = {
        'entities': {},
        'config': config,
        'vehicle_ids': set(),
    }
    
    _LOGGER.info(f"OVMS MQTT config: prefix={topic_prefix}, QoS={qos}, debug={debug_mode}")
    
    # MQTT message handler
    @callback
    def handle_mqtt_message(msg):
        """Handle incoming MQTT messages."""
        topic = msg.topic
        
        try:
            payload_str = msg.payload.decode('utf-8').strip()
        except (UnicodeDecodeError, AttributeError):
            payload_str = str(msg.payload).strip()
        
        if debug_mode:
            _LOGGER.info(f"MQTT message received: topic={topic}, payload={payload_str}")
        else:
            _LOGGER.debug(f"MQTT message received: topic={topic}, payload={payload_str}")
        
        # Log detailed topic structure
        parts = topic.split('/')
        _LOGGER.info(f"Topic parts: {parts}")
        
        # Process metric topics
        if "/metric/" in topic:
            parse_and_process_metric(hass, topic, payload_str, async_add_entities)
    
    def parse_and_process_metric(hass, topic, payload_str, async_add_entities):
        """Parse MQTT topic and process the metric."""
        # Expected format: ovms/username/vehicle_id/metric/path/to/metric
        _LOGGER.info(f"Parsing topic structure: {topic}")
        parts = topic.split('/')
        _LOGGER.info(f"Split into {len(parts)} parts: {parts}")
        
        try:
            # Parse vehicle_id and metric path
            if "metric" in parts:
                metric_index = parts.index("metric")
                _LOGGER.info(f"Found 'metric' at index {metric_index}")
                
                if metric_index >= 3:  # Need at least prefix/username/vehicle_id
                    vehicle_id = parts[2]  # vehicle_id is at index 2
                    _LOGGER.info(f"Extracted vehicle_id from index 2: '{vehicle_id}'")
                    
                    if metric_index < len(parts) - 1:
                        metric_path = '/'.join(parts[metric_index+1:])
                        _LOGGER.info(f"Extracted metric_path from index {metric_index+1} onwards: '{metric_path}'")
                        
                        # Track discovered vehicle_ids
                        if vehicle_id not in hass.data[DOMAIN]['vehicle_ids']:
                            _LOGGER.info(f"New vehicle discovered: {vehicle_id}")
                            hass.data[DOMAIN]['vehicle_ids'].add(vehicle_id)
                            _LOGGER.info(f"Current known vehicles: {hass.data[DOMAIN]['vehicle_ids']}")
                            
                            # Create common metrics for this vehicle
                            create_metrics_for_vehicle(hass, vehicle_id, topic_prefix, async_add_entities)
                        else:
                            _LOGGER.debug(f"Known vehicle: {vehicle_id}")
                        
                        # Parse and process the value
                        value = parse_value(payload_str)
                        _LOGGER.info(f"Parsed value: {value} (type: {type(value).__name__})")
                        
                        create_or_update_entity(hass, vehicle_id, metric_path, value, topic, async_add_entities)
                    else:
                        _LOGGER.warning(f"No metric path found after 'metric' in topic: {topic}")
                else:
                    _LOGGER.warning(f"Not enough parts before 'metric' in topic: {topic}")
            else:
                _LOGGER.warning(f"No 'metric' keyword found in topic: {topic}")
        except Exception as e:
            _LOGGER.error(f"Error parsing topic {topic}: {str(e)}", exc_info=True)
    
    def create_metrics_for_vehicle(hass, vehicle_id, topic_prefix, async_add_entities):
        """Create entities for common metrics for a newly discovered vehicle."""
        _LOGGER.info(f"Creating common metrics for vehicle: {vehicle_id}")
        entities_to_add = []
        
        for metric_key, friendly_name in METRIC_PATTERNS.items():
            unique_id = f"ovms_{slugify(vehicle_id)}_{slugify(metric_key)}"
            
            # Skip if entity already exists
            if unique_id in hass.data[DOMAIN]['entities']:
                _LOGGER.debug(f"Skipping existing entity: {unique_id}")
                continue
                
            _LOGGER.info(f"Creating common metric: {vehicle_id}/{metric_key}")
            
            # Create entity with None value (will be updated when data arrives)
            sensor = OvmsSensor(
                vehicle_id=vehicle_id,
                metric_key=metric_key,
                value=None,
                topic=f"{topic_prefix}/+/{vehicle_id}/metric/{metric_key}",
                friendly_name=friendly_name
            )
            
            # Store and queue for addition
            hass.data[DOMAIN]['entities'][unique_id] = sensor
            entities_to_add.append(sensor)
        
        # Add all entities at once
        if entities_to_add:
            _LOGGER.info(f"Adding {len(entities_to_add)} common metrics for vehicle {vehicle_id}")
            async_add_entities(entities_to_add)
            _LOGGER.info(f"Added entities successfully")
    
    def parse_value(payload_str):
        """Parse the payload string into an appropriate data type."""
        _LOGGER.debug(f"Parsing payload: {payload_str}")
        
        if not payload_str:
            _LOGGER.debug("Empty payload, returning None")
            return None
            
        # Try boolean
        if payload_str.lower() in ('true', 'false'):
            result = payload_str.lower() == 'true'
            _LOGGER.debug(f"Parsed as boolean: {result}")
            return result
            
        # Try number
        try:
            if '.' in payload_str:
                result = float(payload_str)
                _LOGGER.debug(f"Parsed as float: {result}")
                return result
            else:
                result = int(payload_str)
                _LOGGER.debug(f"Parsed as integer: {result}")
                return result
        except ValueError:
            _LOGGER.debug("Not a number")
            pass
            
        # Try JSON
        try:
            result = json.loads(payload_str)
            _LOGGER.debug(f"Parsed as JSON: {result}")
            return result
        except json.JSONDecodeError:
            _LOGGER.debug("Not valid JSON")
            pass
            
        # Return as string
        _LOGGER.debug(f"Using as string: {payload_str}")
        return payload_str
    
    def create_or_update_entity(hass, vehicle_id, metric_key, value, topic, async_add_entities):
        """Create a new entity or update an existing one."""
        unique_id = f"ovms_{slugify(vehicle_id)}_{slugify(metric_key)}"
        _LOGGER.info(f"Entity ID: {unique_id} for vehicle: {vehicle_id}, metric: {metric_key}")
        
        if unique_id in hass.data[DOMAIN]['entities']:
            # Update existing entity
            _LOGGER.info(f"Updating existing entity: {unique_id} = {value}")
            entity = hass.data[DOMAIN]['entities'][unique_id]
            entity.update_value(value)
        else:
            # Create new entity
            _LOGGER.info(f"Creating new entity: {vehicle_id}/{metric_key}")
            
            # Try to get a friendly name for this metric
            friendly_name = METRIC_PATTERNS.get(metric_key)
            _LOGGER.info(f"Friendly name: {friendly_name if friendly_name else 'None, using default'}")
            
            sensor = OvmsSensor(
                vehicle_id=vehicle_id,
                metric_key=metric_key,
                value=value,
                topic=topic,
                friendly_name=friendly_name
            )
            
            hass.data[DOMAIN]['entities'][unique_id] = sensor
            _LOGGER.info(f"Adding entity to Home Assistant: {unique_id}")
            async_add_entities([sensor])
            _LOGGER.info(f"Entity added successfully")
    
    # Subscribe to OVMS topics
    _LOGGER.info(f"Subscribing to OVMS topics: {topic_prefix}/#")
    subscription = await mqtt.async_subscribe(
        hass, 
        f"{topic_prefix}/#", 
        handle_mqtt_message, 
        qos
    )
    _LOGGER.info(f"MQTT subscription successful")
    
    # Store subscription for cleanup
    hass.data[DOMAIN]['subscription'] = subscription
    
    # Create test messages only in debug mode
    async def publish_test_messages():
        """Publish test MQTT messages."""
        test_vehicle = "TEST123"
        test_topics = [
            (f"{topic_prefix}/ovms-user/{test_vehicle}/metric/v/b/soc", "75.5"),
            (f"{topic_prefix}/ovms-user/{test_vehicle}/metric/v/p/odometer", "12345"),
            (f"{topic_prefix}/ovms-user/{test_vehicle}/metric/v/b/range/est", "350")
        ]
        
        _LOGGER.info(f"Publishing {len(test_topics)} test messages")
        for topic, value in test_topics:
            _LOGGER.info(f"Publishing test data: {topic} = {value}")
            await mqtt.async_publish(hass, topic, value, qos)
        _LOGGER.info("Test messages published")
    
    # Publish test data only in debug mode
    if debug_mode:
        _LOGGER.info("Debug mode enabled, publishing test messages")
        await publish_test_messages()
    
    return True


class OvmsSensor(SensorEntity):
    """Representation of an OVMS MQTT sensor."""

    def __init__(self, vehicle_id, metric_key, value, topic, friendly_name=None):
        """Initialize the sensor."""
        super().__init__()
        self._vehicle_id = vehicle_id
        self._metric_key = metric_key
        self._topic = topic
        self._attr_native_value = value
        
        # Create a unique ID
        self._attr_unique_id = f"ovms_{slugify(vehicle_id)}_{slugify(metric_key)}"
        
        # Make a user-friendly name
        if friendly_name:
            self._attr_name = f"OVMS {vehicle_id} {friendly_name}"
        else:
            self._attr_name = f"OVMS {vehicle_id} {metric_key.replace('/', ' ').title()}"
        
        # Set attributes
        self._attr_available = True
        self._attr_extra_state_attributes = {
            "vehicle_id": vehicle_id,
            "metric_key": metric_key,
            "mqtt_topic": topic,
            "source": "OVMS MQTT Integration"
        }
        
        _LOGGER.debug(f"Initialized entity: {self._attr_unique_id}")
    
    @property
    def device_info(self):
        """Return device info for this sensor."""
        return {
            "identifiers": {(DOMAIN, self._vehicle_id)},
            "name": f"OVMS Vehicle {self._vehicle_id}",
            "manufacturer": "Open Vehicle Monitoring System",
            "model": "OVMS",
            "sw_version": "1.0.0",
        }
    
    def update_value(self, value):
        """Update the sensor's value."""
        old_value = self._attr_native_value
        self._attr_native_value = value
        
        if old_value != value:
            _LOGGER.info(f"Updated {self._attr_unique_id}: {old_value} → {value}")
        
        self.async_write_ha_state()
