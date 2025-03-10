"""Support for OVMS binary sensors."""
import logging
from typing import Any, Dict, Optional

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity import async_generate_entity_id
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import (
    LOGGER_NAME,
    SIGNAL_ADD_ENTITIES,
    SIGNAL_UPDATE_ENTITY
)

from .metrics import (
    METRIC_DEFINITIONS,
    TOPIC_PATTERNS,
    BINARY_METRICS,
    get_metric_by_path,
    get_metric_by_pattern,
)

_LOGGER = logging.getLogger(LOGGER_NAME)

# A mapping of binary sensor name patterns to device classes
BINARY_SENSOR_TYPES = {
    "door": BinarySensorDeviceClass.DOOR,
    "window": BinarySensorDeviceClass.WINDOW,
    "lock": BinarySensorDeviceClass.LOCK,
    "plug": BinarySensorDeviceClass.PLUG,
    "charger": BinarySensorDeviceClass.BATTERY_CHARGING,
    "charging": BinarySensorDeviceClass.BATTERY_CHARGING,
    "battery": BinarySensorDeviceClass.BATTERY,
    "motion": BinarySensorDeviceClass.MOTION,
    "connectivity": BinarySensorDeviceClass.CONNECTIVITY,
    "power": BinarySensorDeviceClass.POWER,
    "running": {
        "device_class": None,
        "icon": "mdi:car-electric",
    },
    "online": {
        "device_class": BinarySensorDeviceClass.CONNECTIVITY,
        "icon": "mdi:car-connected",
    },
    "hvac": {
        "device_class": None,
        "icon": "mdi:air-conditioner",
    }
}

async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up OVMS binary sensors based on a config entry."""
    @callback
    def async_add_binary_sensor(data: Dict[str, Any]) -> None:
        """Add binary sensor based on discovery data."""
        if data["entity_type"] != "binary_sensor":
            return

        _LOGGER.info("Adding binary sensor: %s", data["name"])

        sensor = OVMSBinarySensor(
            data["unique_id"],
            data["name"],
            data["topic"],
            data["payload"],
            data["device_info"],
            data["attributes"],
            hass,
            data.get("friendly_name"),
        )

        async_add_entities([sensor])

    # Subscribe to discovery events
    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_ADD_ENTITIES, async_add_binary_sensor)
    )


# pylint: disable=too-few-public-methods
class OVMSBinarySensor(BinarySensorEntity, RestoreEntity):
    """Representation of an OVMS binary sensor."""

    # pylint: disable=too-many-arguments,too-many-instance-attributes
    def __init__(
        self,
        unique_id: str,
        name: str,
        topic: str,
        initial_state: str,
        device_info: DeviceInfo,
        attributes: Dict[str, Any],
        hass: Optional[HomeAssistant] = None,
        friendly_name: Optional[str] = None,
    ) -> None:
        """Initialize the binary sensor."""
        self._attr_unique_id = unique_id
        # Use the entity_id compatible name for internal use
        self._internal_name = name
        # Set the entity name that will display in UI to friendly name or name
        self._attr_name = friendly_name or name.replace("_", " ").title()
        self._topic = topic
        self._attr_is_on = self._parse_state(initial_state)
        self._attr_device_info = device_info
        self._attr_extra_state_attributes = {
            **attributes,
            "topic": topic,
            "last_updated": dt_util.utcnow().isoformat(),
        }

        # Explicitly set entity_id - this ensures consistent naming
        if hass:
            self.entity_id = async_generate_entity_id(
                "binary_sensor.{}",
                name.lower(),
                hass=hass
            )

        # Try to determine device class
        self._determine_device_class()

    async def async_added_to_hass(self) -> None:
        """Subscribe to updates."""
        await super().async_added_to_hass()

        # Restore previous state if available
        if (state := await self.async_get_last_state()) is not None:
            if state.state not in (None, "unavailable", "unknown"):
                self._attr_is_on = state.state == "on"

            # Restore attributes if available
            if state.attributes:
                # Don't overwrite entity attributes like device_class, icon
                saved_attributes = {
                    k: v for k, v in state.attributes.items()
                    if k not in ["device_class", "icon"]
                }
                self._attr_extra_state_attributes.update(saved_attributes)

        @callback
        def update_state(payload: str) -> None:
            """Update the sensor state."""
            self._attr_is_on = self._parse_state(payload)

            # Update timestamp attribute
            now = dt_util.utcnow()
            self._attr_extra_state_attributes["last_updated"] = now.isoformat()

            self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_UPDATE_ENTITY}_{self.unique_id}",
                update_state,
            )
        )

    def _parse_state(self, state: str) -> bool:
        """Parse the state string to a boolean."""
        if state.lower() in ("true", "on", "yes", "1", "open", "locked"):
            return True
        if state.lower() in ("false", "off", "no", "0", "closed", "unlocked"):
            return False

        # Try numeric comparison
        try:
            return float(state) > 0
        except (ValueError, TypeError):
            return False

    def _check_category_from_attributes(self) -> bool:
        """Check for entity category from attributes."""
        if "category" in self._attr_extra_state_attributes:
            category = self._attr_extra_state_attributes["category"]
            # Apply diagnostic entity category to network and system sensors
            if category in ["diagnostic", "network", "system"]:
                self._attr_entity_category = EntityCategory.DIAGNOSTIC
                if category != "diagnostic":
                    # Don't return for network/system to allow further processing
                    _LOGGER.debug("Setting EntityCategory.DIAGNOSTIC for %s category: %s",
                                category, self._internal_name)

            return category == "diagnostic"
        return False

    def _get_metric_path_from_topic(self) -> str:
        """Extract metric path from topic."""
        topic_suffix = self._topic
        if self._topic.count('/') >= 3:  # Skip the prefix part
            parts = self._topic.split('/')
            # Find where the actual metric path starts
            for i, part in enumerate(parts):
                if part in ["metric", "status", "notify", "command", "m", "v", "s", "t"]:
                    topic_suffix = '/'.join(parts[i:])
                    break

        return topic_suffix.replace("/", ".")

    def _set_attributes_from_metric(self) -> bool:
        """Set attributes based on metric information."""
        # Get metric path from topic
        metric_path = self._get_metric_path_from_topic()

        # Try exact match first
        metric_info = get_metric_by_path(metric_path)

        # If no exact match, try by pattern in name and topic
        if not metric_info:
            topic_parts = self._topic.split('/')
            name_parts = self._internal_name.split('_')
            metric_info = get_metric_by_pattern(topic_parts) or get_metric_by_pattern(name_parts)

        # Apply metric info if found
        if metric_info:
            if "device_class" in metric_info:
                self._attr_device_class = metric_info["device_class"]
            if "icon" in metric_info:
                self._attr_icon = metric_info["icon"]
            if "entity_category" in metric_info:
                self._attr_entity_category = metric_info["entity_category"]
            return True
        return False

    def _match_by_pattern(self) -> None:
        """Match by pattern in name for device class and icon."""
        for key, device_class in BINARY_SENSOR_TYPES.items():
            if key in self._internal_name.lower():
                if isinstance(device_class, dict):
                    self._attr_device_class = device_class.get("device_class")
                    self._attr_icon = device_class.get("icon")
                else:
                    self._attr_device_class = device_class
                break

    def _determine_device_class(self) -> None:
        """Determine the device class based on metrics definitions."""
        # Default value
        self._attr_device_class = None
        self._attr_icon = None
        self._attr_entity_category = None

        # Check for category from attributes
        self._check_category_from_attributes()

        # Try to find matching metric
        if self._set_attributes_from_metric():
            return

        # Use name pattern matching as fallback
        self._match_by_pattern()
