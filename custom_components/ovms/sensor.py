"""Support for OVMS sensors."""
import logging
import json
import uuid
import hashlib
from datetime import datetime
from typing import Any, Dict, Optional, List

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfLength,
    UnitOfPower,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.entity import async_generate_entity_id
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    LOGGER_NAME,
    SIGNAL_ADD_ENTITIES,
    SIGNAL_UPDATE_ENTITY
)

from .metrics import (
    METRIC_DEFINITIONS,
    TOPIC_PATTERNS,
    get_metric_by_path,
    get_metric_by_pattern,
)

_LOGGER = logging.getLogger(LOGGER_NAME)

# A mapping of sensor name patterns to device classes and units
SENSOR_TYPES = {
    "soc": {
        "device_class": SensorDeviceClass.BATTERY,
        "state_class": SensorStateClass.MEASUREMENT,
        "unit": PERCENTAGE,
        "icon": "mdi:battery",
    },
    "range": {
        "device_class": SensorDeviceClass.DISTANCE,
        "state_class": SensorStateClass.MEASUREMENT,
        "unit": UnitOfLength.KILOMETERS,
        "icon": "mdi:map-marker-distance",
    },
    "temperature": {
        "device_class": SensorDeviceClass.TEMPERATURE,
        "state_class": SensorStateClass.MEASUREMENT,
        "unit": UnitOfTemperature.CELSIUS,
        "icon": "mdi:thermometer",
    },
    "power": {
        "device_class": SensorDeviceClass.POWER,
        "state_class": SensorStateClass.MEASUREMENT,
        "unit": UnitOfPower.WATT,
        "icon": "mdi:flash",
    },
    "current": {
        "device_class": SensorDeviceClass.CURRENT,
        "state_class": SensorStateClass.MEASUREMENT,
        "unit": UnitOfElectricCurrent.AMPERE,
        "icon": "mdi:current-ac",
    },
    "voltage": {
        "device_class": SensorDeviceClass.VOLTAGE,
        "state_class": SensorStateClass.MEASUREMENT,
        "unit": UnitOfElectricPotential.VOLT,
        "icon": "mdi:flash",
    },
    "energy": {
        "device_class": SensorDeviceClass.ENERGY,
        "state_class": SensorStateClass.TOTAL_INCREASING,
        "unit": UnitOfEnergy.KILO_WATT_HOUR,
        "icon": "mdi:battery-charging",
    },
    "speed": {
        "device_class": SensorDeviceClass.SPEED,
        "state_class": SensorStateClass.MEASUREMENT,
        "unit": UnitOfSpeed.KILOMETERS_PER_HOUR,
        "icon": "mdi:speedometer",
    },
    # Additional icons for EV-specific metrics
    "odometer": {
        "icon": "mdi:counter",
        "state_class": SensorStateClass.TOTAL_INCREASING,
    },
    "efficiency": {
        "icon": "mdi:leaf",
        "state_class": SensorStateClass.MEASUREMENT,
    },
    "charging_time": {
        "icon": "mdi:timer",
        "state_class": SensorStateClass.MEASUREMENT,
    },
    "climate": {
        "icon": "mdi:fan",
        "state_class": SensorStateClass.MEASUREMENT,
    },
    "hvac": {
        "icon": "mdi:air-conditioner",
        "state_class": SensorStateClass.MEASUREMENT,
    },
    "motor": {
        "icon": "mdi:engine",
        "state_class": SensorStateClass.MEASUREMENT,
    },
    "trip": {
        "icon": "mdi:map-marker-path",
        "state_class": SensorStateClass.MEASUREMENT,
    },
    # Diagnostic sensors
    "status": {
        "entity_category": EntityCategory.DIAGNOSTIC,
        "icon": "mdi:information-outline",
    },
    "signal": {
        "entity_category": EntityCategory.DIAGNOSTIC,
        "device_class": SensorDeviceClass.SIGNAL_STRENGTH,
        "icon": "mdi:signal",
    },
    "firmware": {
        "entity_category": EntityCategory.DIAGNOSTIC,
        "icon": "mdi:package-up",
    },
    "version": {
        "entity_category": EntityCategory.DIAGNOSTIC,
        "icon": "mdi:tag-text",
    },
    "task": {
        "entity_category": EntityCategory.DIAGNOSTIC,
        "icon": "mdi:list-status",
    }
}

# List of device classes that should have numeric values
NUMERIC_DEVICE_CLASSES = [
    SensorDeviceClass.BATTERY,
    SensorDeviceClass.CURRENT,
    SensorDeviceClass.ENERGY,
    SensorDeviceClass.HUMIDITY,
    SensorDeviceClass.POWER,
    SensorDeviceClass.TEMPERATURE,
    SensorDeviceClass.VOLTAGE,
    SensorDeviceClass.DISTANCE,
    SensorDeviceClass.SPEED,
]

# Special string values that should be converted to None for numeric sensors
SPECIAL_STATE_VALUES = ["unavailable", "unknown", "none", "", "null", "nan"]


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up OVMS sensors based on a config entry."""
    @callback
    def async_add_sensor(data: Dict[str, Any]) -> None:
        """Add sensor based on discovery data."""
        if not data or not isinstance(data, dict):
            _LOGGER.warning("Invalid entity data received: %s", data)
            return

        entity_type = data.get("entity_type")
        if not entity_type or entity_type != "sensor":
            _LOGGER.debug("Skipping non-sensor entity: %s", entity_type)
            return

        _LOGGER.info("Adding sensor: %s", data.get("name", "unknown"))

        # Handle cell sensors differently
        if "cell_sensors" in data:
            _LOGGER.debug("Adding cell sensors from parent entity: %s", data.get("parent_entity"))
            try:
                sensors = []
                for cell_config in data["cell_sensors"]:
                    sensor = CellVoltageSensor(
                        cell_config["unique_id"],
                        cell_config["name"],
                        cell_config.get("topic", ""),
                        cell_config.get("state"),
                        cell_config.get("device_info", {}),
                        cell_config.get("attributes", {}),
                        cell_config.get("friendly_name"),
                        hass,
                    )
                    sensors.append(sensor)

                async_add_entities(sensors)
            except Exception as e:
                _LOGGER.error("Error creating cell sensors: %s", e)
            return

        try:
            sensor = OVMSSensor(
                data.get("unique_id", str(uuid.uuid4())),
                data.get("name", "unknown"),
                data.get("topic", ""),
                data.get("payload", ""),
                data.get("device_info", {}),
                data.get("attributes", {}),
                data.get("friendly_name"),
                hass,
            )

            async_add_entities([sensor])
        except Exception as e:
            _LOGGER.error("Error creating sensor: %s", e)

    # Subscribe to discovery events
    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_ADD_ENTITIES, async_add_sensor)
    )

    # Signal that all platforms are loaded
    async_dispatcher_send(hass, "ovms_sensor_platform_loaded")


class CellVoltageSensor(SensorEntity, RestoreEntity):
    """Representation of a cell voltage sensor."""

    def __init__(
        self,
        unique_id: str,
        name: str,
        topic: str,
        initial_state: Any,
        device_info: DeviceInfo,
        attributes: Dict[str, Any],
        friendly_name: Optional[str] = None,
        hass: Optional[HomeAssistant] = None,
    ):
        """Initialize the sensor."""
        self._attr_unique_id = unique_id
        self._internal_name = name
        self._attr_name = friendly_name or name.replace("_", " ").title()
        self._topic = topic
        self._attr_device_info = device_info or {}
        self._attr_extra_state_attributes = {
            **attributes,
            "topic": topic,
            "last_updated": dt_util.utcnow().isoformat(),
        }
        self.hass = hass

        # Initialize device class and other attributes from parent
        self._attr_device_class = attributes.get("device_class")
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = attributes.get("unit_of_measurement")
        self._attr_icon = attributes.get("icon")

        # Only set native value after attributes are initialized
        if self._requires_numeric_value() and self._is_special_state_value(initial_state):
            self._attr_native_value = None
        else:
            self._attr_native_value = initial_state

        # Explicitly set entity_id - this ensures consistent naming
        if hass:
            entity_id = f"sensor.{name.lower()}"

            self.entity_id = async_generate_entity_id(
                "sensor.{}", name.lower(),
                hass=hass,
            )

    async def async_added_to_hass(self) -> None:
        """Subscribe to updates."""
        await super().async_added_to_hass()

        # Restore previous state if available
        if (state := await self.async_get_last_state()) is not None:
            if state.state not in ["unavailable", "unknown", None]:
                # Only restore the state if it's not a special state
                self._attr_native_value = state.state
            # Restore attributes if available
            if state.attributes:
                # Don't overwrite entity attributes like unit, etc.
                saved_attributes = {
                    k: v for k, v in state.attributes.items()
                    if k not in ["device_class", "state_class", "unit_of_measurement"]
                }
                self._attr_extra_state_attributes.update(saved_attributes)

        @callback
        def update_state(payload: Any) -> None:
            """Update the sensor state."""

            # Parse the value appropriately for the sensor type
            if self._requires_numeric_value() and self._is_special_state_value(payload):
                self._attr_native_value = None
            else:
                try:
                    value = float(payload)
                    self._attr_native_value = value
                except (ValueError, TypeError):
                    self._attr_native_value = payload

            # Update timestamp attribute
            now = dt_util.utcnow()
            self._attr_extra_state_attributes["last_updated"] = now.isoformat()

            self.async_write_ha_state()

        # Subscribe to updates for this entity
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_UPDATE_ENTITY}_{self.unique_id}",
                update_state,
            )
        )

    def _requires_numeric_value(self) -> bool:
        """Check if this sensor requires a numeric value based on its device class."""
        return (
            self._attr_device_class in NUMERIC_DEVICE_CLASSES or
            self._attr_state_class in [SensorStateClass.MEASUREMENT, SensorStateClass.TOTAL, SensorStateClass.TOTAL_INCREASING]
        )

    def _is_special_state_value(self, value) -> bool:
        """Check if a value is a special state value that should be converted to None."""
        if value is None:
            return True
        if isinstance(value, str) and value.lower() in SPECIAL_STATE_VALUES:
            return True
        return False


class OVMSSensor(SensorEntity, RestoreEntity):
    """Representation of an OVMS sensor."""

    def __init__(
        self,
        unique_id: str,
        name: str,
        topic: str,
        initial_state: str,
        device_info: DeviceInfo,
        attributes: Dict[str, Any],
        friendly_name: Optional[str] = None,
        hass: Optional[HomeAssistant] = None,
    ) -> None:
        """Initialize the sensor."""
        self._attr_unique_id = unique_id
        # Use the entity_id compatible name for internal use
        self._internal_name = name
        # Set the entity name that will display in UI to friendly name or name
        self._attr_name = friendly_name or name.replace("_", " ").title()
        self._topic = topic
        self._attr_device_info = device_info or {}
        self._attr_extra_state_attributes = {
            **attributes,
            "topic": topic,
            "last_updated": dt_util.utcnow().isoformat(),
        }
        self.hass = hass

        # Explicitly set entity_id - this ensures consistent naming
        if hass:
            self.entity_id = async_generate_entity_id(
                "sensor.{}", name.lower(),
                hass=hass,
            )

        # Try to determine device class and unit
        self._determine_sensor_type()

        # Only set native value after attributes are initialized
        self._attr_native_value = self._parse_value(initial_state)

        # Try to extract additional attributes from initial state if it's JSON
        self._process_json_payload(initial_state)

        # Initialize cell sensors tracking
        self._cell_sensors_created = False
        self._cell_sensors = []

    async def async_added_to_hass(self) -> None:
        """Subscribe to updates."""
        await super().async_added_to_hass()

        # Restore previous state if available
        if (state := await self.async_get_last_state()) is not None:
            if state.state not in ["unavailable", "unknown", None]:
                # Only restore the state if it's not a special state
                self._attr_native_value = state.state
            # Restore attributes if available
            if state.attributes:
                # Don't overwrite entity attributes like unit, etc.
                saved_attributes = {
                    k: v for k, v in state.attributes.items()
                    if k not in ["device_class", "state_class", "unit_of_measurement"]
                }
                self._attr_extra_state_attributes.update(saved_attributes)

        @callback
        def update_state(payload: str) -> None:
            """Update the sensor state."""
            self._attr_native_value = self._parse_value(payload)

            # Update timestamp attribute
            now = dt_util.utcnow()
            self._attr_extra_state_attributes["last_updated"] = now.isoformat()

            # Try to extract additional attributes from payload if it's JSON
            self._process_json_payload(payload)

            self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_UPDATE_ENTITY}_{self.unique_id}",
                update_state,
            )
        )

    def _calculate_median(self, values):
        """Calculate the median of a list of values."""
        if not values:
            return None
        sorted_values = sorted(values)
        n = len(sorted_values)
        if n % 2 == 0:
            return (sorted_values[n//2 - 1] + sorted_values[n//2]) / 2
        else:
            return sorted_values[n//2]

    def _determine_sensor_type(self) -> None:
        """Determine the sensor type based on metrics definitions."""
        # Default values
        self._attr_device_class = None
        self._attr_state_class = None
        self._attr_native_unit_of_measurement = None
        self._attr_entity_category = None
        self._attr_icon = None

        # Check if attributes specify a category
        if "category" in self._attr_extra_state_attributes:
            category = self._attr_extra_state_attributes["category"]
            # Also apply diagnostic entity category to network and system sensors
            if category in ["diagnostic", "network", "system"]:
                from homeassistant.helpers.entity import EntityCategory
                self._attr_entity_category = EntityCategory.DIAGNOSTIC
                if category != "diagnostic":  # Don't return for network/system to allow further processing
                    _LOGGER.debug("Setting EntityCategory.DIAGNOSTIC for %s category: %s",
                                 category, self._internal_name)

        # Special check for timer mode sensors to avoid numeric conversion issues
        if "timermode" in self._internal_name.lower() or "timer_mode" in self._internal_name.lower():
            # For timer mode sensors, explicitly override device class and state class
            self._attr_device_class = None
            self._attr_state_class = None
            self._attr_icon = "mdi:timer-outline"
            return

        # Try to find matching metric by converting topic to dot notation
        topic_suffix = self._topic
        if self._topic.count('/') >= 3:  # Skip the prefix part
            parts = self._topic.split('/')
            # Find where the actual metric path starts
            for i, part in enumerate(parts):
                if part in ["metric", "status", "notify", "command", "m", "v", "s", "t"]:
                    topic_suffix = '/'.join(parts[i:])
                    break

        metric_path = topic_suffix.replace("/", ".")

        # Try exact match first
        metric_info = get_metric_by_path(metric_path)

        # If no exact match, try by pattern in name and topic
        if not metric_info:
            topic_parts = topic_suffix.split('/')
            name_parts = self._internal_name.split('_')
            metric_info = get_metric_by_pattern(topic_parts) or get_metric_by_pattern(name_parts)

        # Apply metric info if found
        if metric_info:
            if "device_class" in metric_info:
                self._attr_device_class = metric_info["device_class"]
            if "state_class" in metric_info:
                self._attr_state_class = metric_info["state_class"]
            if "unit" in metric_info:
                self._attr_native_unit_of_measurement = metric_info["unit"]
            if "entity_category" in metric_info:
                self._attr_entity_category = metric_info["entity_category"]
            if "icon" in metric_info:
                self._attr_icon = metric_info["icon"]
            return

        # If no metric info was found, use the original pattern matching as fallback
        for key, sensor_type in SENSOR_TYPES.items():
            if key in self._internal_name.lower() or key in self._topic.lower():
                self._attr_device_class = sensor_type.get("device_class")
                self._attr_state_class = sensor_type.get("state_class")
                self._attr_native_unit_of_measurement = sensor_type.get("unit")
                self._attr_entity_category = sensor_type.get("entity_category")
                self._attr_icon = sensor_type.get("icon")
                break

    def _requires_numeric_value(self) -> bool:
        """Check if this sensor requires a numeric value based on its device class."""
        return (
            self._attr_device_class in NUMERIC_DEVICE_CLASSES or
            self._attr_state_class in [SensorStateClass.MEASUREMENT, SensorStateClass.TOTAL, SensorStateClass.TOTAL_INCREASING]
        )

    def _is_special_state_value(self, value) -> bool:
        """Check if a value is a special state value that should be converted to None."""
        if value is None:
            return True
        if isinstance(value, str) and value.lower() in SPECIAL_STATE_VALUES:
            return True
        return False

    def _parse_value(self, value: str) -> Any:
        """Parse the value from the payload."""
        # Handle special state values for numeric sensors
        if self._requires_numeric_value() and self._is_special_state_value(value):
            return None

        # Special handling for yes/no values in numeric sensors
        if self._requires_numeric_value() and isinstance(value, str):
            # Convert common boolean strings to numeric values
            if value.lower() in ["no", "off", "false", "disabled"]:
                return 0
            if value.lower() in ["yes", "on", "true", "enabled"]:
                return 1

        # Check if this is a comma-separated list of numbers (including negative numbers)
        if isinstance(value, str) and "," in value:
            try:
                # Try to parse all parts as floats
                parts = [float(part.strip()) for part in value.split(",") if part.strip()]
                if parts:
                    # Store the array in attributes
                    self._attr_extra_state_attributes["cell_values"] = parts
                    self._attr_extra_state_attributes["cell_count"] = len(parts)

                    # Calculate and store statistics
                    median_value = self._calculate_median(parts)
                    avg_value = sum(parts) / len(parts)
                    min_value = min(parts)
                    max_value = max(parts)

                    # Store statistics as attributes
                    self._attr_extra_state_attributes["median"] = median_value
                    self._attr_extra_state_attributes["min"] = min_value
                    self._attr_extra_state_attributes["max"] = max_value

                    # Store individual cell values with descriptive names
                    stat_type = "cell"
                    if "temp" in self._internal_name.lower():
                        stat_type = "temp"
                    elif "voltage" in self._internal_name.lower():
                        stat_type = "voltage"

                    for i, val in enumerate(parts):
                        self._attr_extra_state_attributes[f"{stat_type}_{i+1}"] = val

                    # Set flag to skip creating individual cell sensors
                    self._cell_sensors_created = True

                    # Return average as the main value, rounded to 4 decimal places for display
                    return round(avg_value, 4)
            except (ValueError, TypeError):
                # If any part can't be converted to float, fall through to other methods
                pass

        # Try parsing as JSON first
        try:
            json_val = json.loads(value)

            # Handle special JSON values
            if self._is_special_state_value(json_val):
                return None

            # If JSON is a dict, extract likely value
            if isinstance(json_val, dict):
                result = None
                if "value" in json_val:
                    result = json_val["value"]
                elif "state" in json_val:
                    result = json_val["state"]
                else:
                    # Return first numeric value found
                    for key, val in json_val.items():
                        if isinstance(val, (int, float)):
                            result = val
                            break

                # Handle special values in result
                if self._is_special_state_value(result):
                    return None

                # If we have a result, return it; otherwise fall back to string representation
                if result is not None:
                    return result

                # If we need a numeric value but couldn't extract one, return None
                if self._requires_numeric_value():
                    return None
                return str(json_val)

            # If JSON is a scalar, use it directly
            if isinstance(json_val, (int, float)):
                return json_val

            if isinstance(json_val, str):
                # Handle special string values
                if self._is_special_state_value(json_val):
                    return None

                # If we need a numeric value but got a string, try to convert it
                if self._requires_numeric_value():
                    try:
                        return float(json_val)
                    except (ValueError, TypeError):
                        return None
                return json_val

            if isinstance(json_val, bool):
                # If we need a numeric value, convert bool to int
                if self._requires_numeric_value():
                    return 1 if json_val else 0
                return json_val

            # For arrays or other types, convert to string if not numeric
            if self._requires_numeric_value():
                return None
            return str(json_val)

        except (ValueError, json.JSONDecodeError):
            # Not JSON, try numeric
            try:
                # Check if it's a float
                if isinstance(value, str) and "." in value:
                    return float(value)
                # Check if it's an int
                return int(value)
            except (ValueError, TypeError):
                # If we need a numeric value but couldn't convert, return None
                if self._requires_numeric_value():
                    return None
                # Otherwise return as string
                return value

    def _process_json_payload(self, payload: str) -> None:
        """Process JSON payload to extract additional attributes."""
        try:
            json_data = json.loads(payload)
            if isinstance(json_data, dict):
                # Add useful attributes from the data
                for key, value in json_data.items():
                    if key not in ["value", "state", "data"] and key not in self._attr_extra_state_attributes:
                        self._attr_extra_state_attributes[key] = value

                # If there's a timestamp in the JSON, use it
                if "timestamp" in json_data:
                    self._attr_extra_state_attributes["device_timestamp"] = json_data["timestamp"]

                # If there's a unit in the JSON, use it for native unit
                if "unit" in json_data and not self._attr_native_unit_of_measurement:
                    unit = json_data["unit"]
                    self._attr_native_unit_of_measurement = unit

        except (ValueError, json.JSONDecodeError):
            # Not JSON, that's fine
            pass

    def _create_cell_sensors(self, cell_values):
        """Create individual sensors for each cell value."""
        # Skip creating individual sensors if the flag is set
        if hasattr(self, '_cell_sensors_created') and self._cell_sensors_created:
            return

        # Add topic hash to make unique IDs truly unique
        topic_hash = hashlib.md5(self._topic.encode()).hexdigest()[:8]

        # Extract vehicle_id from unique_id
        vehicle_id = self.unique_id.split('_')[0]
        category = self._attr_extra_state_attributes.get("category", "battery")

        # Parse topic to extract just the metric path
        topic_suffix = self._topic
        if self._topic.count('/') >= 3:
            parts = self._topic.split('/')
            for i, part in enumerate(parts):
                if part in ["metric", "status", "notify", "command", "m", "v", "s", "t"]:
                    topic_suffix = '/'.join(parts[i:])
                    break

        # Convert to metric path just like in mqtt.py
        topic_parts = topic_suffix.split('/')
        metric_path = "_".join(topic_parts)

        self._cell_sensors = []
        sensor_configs = []

        # Create sensors using same pattern as mqtt.py
        for i, value in enumerate(cell_values):
            # Generate unique entity name that includes the parent metric path to prevent collisions
            entity_name = f"ovms_{vehicle_id}_{category}_{metric_path}_cell_{i+1}".lower()

            # Generate unique ID using hash
            cell_unique_id = f"{vehicle_id}_{category}_{topic_hash}_cell_{i+1}"

            # Create friendly name for cell
            friendly_name = f"{self.name} Cell {i+1}"

            # Ensure value is numeric for sensors with device class
            if self._requires_numeric_value() and self._is_special_state_value(value):
                parsed_value = None
            else:
                parsed_value = value

            # Create sensor config
            sensor_config = {
                "unique_id": cell_unique_id,
                "name": entity_name,
                "friendly_name": friendly_name,
                "state": parsed_value,
                "entity_id": f"sensor.{entity_name}",
                "device_info": self.device_info,
                "topic": f"{self._topic}/cell/{i+1}",
                "attributes": {
                    "cell_index": i,
                    "parent_entity": self.entity_id,
                    "parent_topic": self._topic,
                    "category": category,
                    "device_class": self.device_class,
                    "state_class": SensorStateClass.MEASUREMENT,
                    "unit_of_measurement": self.native_unit_of_measurement,
                },
            }

            sensor_configs.append(sensor_config)
            self._cell_sensors.append(cell_unique_id)

        # Flag cells as created
        self._cell_sensors_created = True

        # Create and add entities through the entity discovery mechanism
        if self.hass:
            async_dispatcher_send(
                self.hass,
                SIGNAL_ADD_ENTITIES,
                {
                    "entity_type": "sensor",
                    "cell_sensors": sensor_configs,
                    "parent_entity": self.entity_id,
                }
            )

    def _update_cell_sensor_values(self, cell_values):
        """Update the values of existing cell sensors."""
        if not self.hass or not hasattr(self, '_cell_sensors'):
            return

        # Update each cell sensor with its new value
        for i, value in enumerate(cell_values):
            if i < len(self._cell_sensors):
                cell_id = self._cell_sensors[i]
                # Use the dispatcher to signal an update
                async_dispatcher_send(
                    self.hass,
                    f"{SIGNAL_UPDATE_ENTITY}_{cell_id}",
                    value,
                )
