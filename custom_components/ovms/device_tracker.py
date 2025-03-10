"""Support for tracking OVMS vehicles."""
import logging
import json
from typing import Any, Dict, Optional

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity import async_generate_entity_id
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
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

async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    """Set up device tracker for OVMS component."""
    @callback
    def async_add_device_tracker(data: Dict[str, Any]) -> None:
        """Add device tracker based on discovery data."""
        if data["entity_type"] != "device_tracker":
            return

        _LOGGER.info("Adding device tracker for: %s", data["name"])

        tracker = OVMSDeviceTracker(
            data["unique_id"],
            data["name"],
            data["topic"],
            data["payload"],
            data["device_info"],
            data["attributes"],
            hass,
            data.get("friendly_name"),
        )

        async_add_entities([tracker])

    # Subscribe to discovery events
    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_ADD_ENTITIES, async_add_device_tracker)
    )


class OVMSDeviceTracker(TrackerEntity, RestoreEntity):
    """OVMS device tracker."""

    def __init__(
        self,
        unique_id: str,
        name: str,
        topic: str,
        initial_payload: str,
        device_info: DeviceInfo,
        attributes: Dict[str, Any],
        hass: Optional[HomeAssistant] = None,
        friendly_name: Optional[str] = None,
    ) -> None:
        """Initialize the device tracker."""
        self._attr_unique_id = unique_id
        # Use the entity_id compatible name for internal use
        self._internal_name = name
        # Set the entity name that will display in UI to friendly name or name
        self._attr_name = friendly_name or name.replace("_", " ").title()
        self._topic = topic

        self._attr_device_info = device_info
        self._attr_source_type = SourceType.GPS
        self._attr_icon = "mdi:car-electric"
        self._attr_entity_category = None
        self._attr_extra_state_attributes = {
            **attributes,
            "topic": topic,
            "last_updated": dt_util.utcnow().isoformat(),
        }

        # Explicitly set entity_id - this ensures consistent naming
        if hass:
            self.entity_id = async_generate_entity_id(
                "device_tracker.{}",
                name.lower(),
                hass=hass
            )

        # Set default state attributes
        self._attr_source_type = SourceType.GPS

        # Try to determine if this should be a diagnostic entity
        self._determine_entity_category()

        # Try to parse initial location
        self._parse_payload(initial_payload)

    def _determine_entity_category(self) -> None:
        """Determine if this entity should be a diagnostic entity."""
        # Check if attributes already specify a category
        if "category" in self._attr_extra_state_attributes:
            category = self._attr_extra_state_attributes["category"]
            if category == "diagnostic":
                from homeassistant.helpers.entity import EntityCategory
                self._attr_entity_category = EntityCategory.DIAGNOSTIC
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

        # Apply entity category if found in metric info
        if metric_info and "entity_category" in metric_info:
            self._attr_entity_category = metric_info["entity_category"]

    async def async_added_to_hass(self) -> None:
        """Subscribe to updates."""
        await super().async_added_to_hass()

        # Restore previous state if available
        if (state := await self.async_get_last_state()) is not None:
            # Restore attributes if available
            if state.attributes:
                # Only restore attributes that don't affect internal state management
                restorable_attrs = {
                    k: v for k, v in state.attributes.items()
                    if k not in ["source_type", "latitude", "longitude"]
                }
                self._attr_extra_state_attributes.update(restorable_attrs)

                # Restore location data
                if "latitude" in state.attributes and "longitude" in state.attributes:
                    self._attr_latitude = state.attributes["latitude"]
                    self._attr_longitude = state.attributes["longitude"]

                    # Restore optional location attributes
                    for attr in ["altitude", "heading", "speed"]:
                        if attr in state.attributes:
                            self._attr_extra_state_attributes[attr] = state.attributes[attr]

        @callback
        def update_state(payload: str) -> None:
            """Update the tracker state."""
            self._parse_payload(payload)

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

    def _parse_payload(self, payload: str) -> None:
        """Parse the location payload."""
        _LOGGER.debug("Parsing location payload: %s", payload)

        try:
            # Try to parse as JSON
            data = json.loads(payload)

            if isinstance(data, dict):
                # Look for lat/lon values using different possible field names
                lat = None
                lon = None

                # Check for various naming conventions for GPS coordinates
                for lat_name in ["lat", "latitude", "LAT", "Latitude"]:
                    if lat_name in data:
                        lat = float(data[lat_name])
                        break

                for lon_name in ["lon", "lng", "longitude", "LON", "Longitude"]:
                    if lon_name in data:
                        lon = float(data[lon_name])
                        break

                if lat is not None and lon is not None:
                    self._attr_latitude = lat
                    self._attr_longitude = lon
                    _LOGGER.debug("Parsed location: %f, %f", lat, lon)
                else:
                    _LOGGER.warning("Could not find lat/lon in JSON data: %s", data)
                    return

                # Check for additional attributes
                for attr_field, attr_name in [
                    (["alt", "altitude", "ALT", "Altitude"], "altitude"),
                    (["spd", "speed", "SPD", "Speed"], "speed"),
                    (["hdg", "heading", "bearing", "direction"], "heading"),
                    (["acc", "accuracy", "hor_acc", "horizontal_accuracy"], "accuracy"),
                ]:
                    for field in attr_field:
                        if field in data:
                            try:
                                self._attr_extra_state_attributes[attr_name] = float(data[field])
                                break
                            except (ValueError, TypeError):
                                pass

        except (ValueError, TypeError, json.JSONDecodeError):
            # Not JSON, try comma-separated values
            parts = payload.split(",")
            if len(parts) >= 2:
                try:
                    lat = float(parts[0].strip())
                    lon = float(parts[1].strip())

                    if -90 <= lat <= 90 and -180 <= lon <= 180:
                        self._attr_latitude = lat
                        self._attr_longitude = lon
                        _LOGGER.debug("Parsed location from CSV: %f, %f", lat, lon)

                        # If we have more parts, they might be altitude, speed, etc.
                        if len(parts) >= 3:
                            try:
                                self._attr_extra_state_attributes["altitude"] = float(parts[2].strip())
                            except (ValueError, TypeError):
                                pass

                        if len(parts) >= 4:
                            try:
                                self._attr_extra_state_attributes["speed"] = float(parts[3].strip())
                            except (ValueError, TypeError):
                                pass

                        if len(parts) >= 5:
                            try:
                                self._attr_extra_state_attributes["heading"] = float(parts[4].strip())
                            except (ValueError, TypeError):
                                pass
                    else:
                        _LOGGER.warning("Invalid lat/lon values: %f, %f", lat, lon)
                except (ValueError, TypeError):
                    _LOGGER.warning("Could not parse location data as CSV: %s", payload)
