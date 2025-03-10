"""Support for OVMS switches."""
import logging
import json
from typing import Any, Callable, Dict, Optional

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
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

# A mapping of switch types
SWITCH_TYPES = {
    "climate": {
        "icon": "mdi:thermometer",
        "category": None,
        "command": "climate",
    },
    "charge": {
        "icon": "mdi:battery-charging",
        "category": None,
        "command": "charge",
    },
    "lock": {
        "icon": "mdi:lock",
        "category": None,
        "command": "lock",
    },
    "valet": {
        "icon": "mdi:key",
        "category": None,
        "command": "valet",
    },
    "debug": {
        "icon": "mdi:bug",
        "category": EntityCategory.DIAGNOSTIC,
        "command": "debug",
    },
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up OVMS switches based on a config entry."""
    @callback
    def async_add_switch(data: Dict[str, Any]) -> None:
        """Add switch based on discovery data."""
        if data["entity_type"] != "switch":
            return

        _LOGGER.info("Adding switch: %s", data["name"])

        # Get the MQTT client for publishing commands
        mqtt_client = hass.data[DOMAIN][entry.entry_id]["mqtt_client"]

        switch = OVMSSwitch(
            data["unique_id"],
            data["name"],
            data["topic"],
            data["payload"],
            data["device_info"],
            data["attributes"],
            mqtt_client.async_send_command,
            hass,
            data.get("friendly_name"),
        )

        async_add_entities([switch])

    # Subscribe to discovery events
    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_ADD_ENTITIES, async_add_switch)
    )


class OVMSSwitch(SwitchEntity, RestoreEntity):
    """Representation of an OVMS switch."""

    def __init__(
        self,
        unique_id: str,
        name: str,
        topic: str,
        initial_state: str,
        device_info: DeviceInfo,
        attributes: Dict[str, Any],
        command_function: Callable,
        hass: Optional[HomeAssistant] = None,
        friendly_name: Optional[str] = None,
    ) -> None:
        """Initialize the switch."""
        self._attr_unique_id = unique_id
        # Use the entity_id compatible name for internal use
        self._internal_name = name
        # Set the entity name that will display in UI to friendly name or name
        self._attr_name = friendly_name or name.replace("_", " ").title()
        self._topic = topic
        self._attr_device_info = device_info
        self._attr_extra_state_attributes = {
            **attributes,
            "topic": topic,
            "last_updated": dt_util.utcnow().isoformat(),
        }
        self._command_function = command_function

        # Explicitly set entity_id - this ensures consistent naming
        if hass:
            self.entity_id = async_generate_entity_id(
                "switch.{}",
                name.lower(),
                hass=hass
            )

        # Determine switch type and attributes
        self._determine_switch_type()

        # Set initial state
        self._attr_is_on = self._parse_state(initial_state)

        # Try to extract additional attributes if it's JSON
        self._process_json_payload(initial_state)

        # Derive the command to use for this switch
        self._command = self._derive_command()

    async def async_added_to_hass(self) -> None:
        """Subscribe to updates."""
        await super().async_added_to_hass()

        # Restore previous state if available
        if (state := await self.async_get_last_state()) is not None:
            if state.state not in (None, "unavailable", "unknown"):
                self._attr_is_on = state.state == "on"

            # Restore attributes if available
            if state.attributes:
                # Don't overwrite entity attributes like icon, etc.
                saved_attributes = {
                    k: v for k, v in state.attributes.items()
                    if k not in ["icon", "entity_category"]
                }
                self._attr_extra_state_attributes.update(saved_attributes)

        @callback
        def update_state(payload: str) -> None:
            """Update the switch state."""
            self._attr_is_on = self._parse_state(payload)

            # Update timestamp attribute
            now = dt_util.utcnow()
            self._attr_extra_state_attributes["last_updated"] = now.isoformat()

            # Try to extract additional attributes if it's JSON
            self._process_json_payload(payload)

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
        _LOGGER.debug("Parsing switch state: %s", state)

        # Try to parse as JSON first
        try:
            data = json.loads(state)

            # If JSON is a dict with a state/value field, use that
            if isinstance(data, dict):
                for key in ["state", "value", "status"]:
                    if key in data:
                        state = str(data[key])
                        break
            # If JSON is a boolean or number, use that directly
            elif isinstance(data, bool):
                return data
            elif isinstance(data, (int, float)):
                return bool(data)
            else:
                # Convert the entire JSON to string for normal parsing
                state = str(data)

        except (ValueError, json.JSONDecodeError):
            # Not JSON, continue with string parsing
            pass

        # Check for boolean-like values in string form
        if isinstance(state, str):
            if state.lower() in ("true", "on", "yes", "1", "enabled", "active"):
                return True
            if state.lower() in ("false", "off", "no", "0", "disabled", "inactive"):
                return False

        # Try numeric comparison for anything else
        try:
            return float(state) > 0
        except (ValueError, TypeError):
            _LOGGER.warning("Could not determine switch state from value: %s", state)
            return False

    def _determine_switch_type(self) -> None:
        """Determine the switch type and set icon and category."""
        self._attr_icon = None
        self._attr_entity_category = None

        # Check if attributes specify a category
        if "category" in self._attr_extra_state_attributes:
            category = self._attr_extra_state_attributes["category"]
            if category == "diagnostic":
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

        # Apply metric info if found
        if metric_info:
            if "icon" in metric_info:
                self._attr_icon = metric_info["icon"]
            if "entity_category" in metric_info:
                self._attr_entity_category = metric_info["entity_category"]
            return

        # If no metric info found, use original method as fallback
        for key, switch_type in SWITCH_TYPES.items():
            if key in self._internal_name.lower() or key in self._topic.lower():
                self._attr_icon = switch_type.get("icon")
                self._attr_entity_category = switch_type.get("category")
                break

    def _derive_command(self) -> str:
        """Derive the command to use for this switch."""
        # First check if the topic gives us the command directly
        parts = self._topic.split("/")
        if "command" in parts and len(parts) > parts.index("command") + 1:
            command_idx = parts.index("command")
            if command_idx + 1 < len(parts):
                return parts[command_idx + 1]

        # Otherwise try to determine from the name
        for key, switch_type in SWITCH_TYPES.items():
            if key in self._internal_name.lower():
                if "command" in switch_type:
                    return switch_type["command"]

        # Extract command from attribute if available
        if "command" in self._attr_extra_state_attributes:
            return self._attr_extra_state_attributes["command"]

        # Fall back to the base name
        command = self._internal_name.lower().replace("command_", "")
        return command

    def _process_json_payload(self, payload: str) -> None:
        """Process JSON payload to extract additional attributes."""
        try:
            json_data = json.loads(payload)
            if isinstance(json_data, dict):
                # Add useful attributes from the data
                for key, value in json_data.items():
                    if key not in ["value", "state", "status"] and key not in self._attr_extra_state_attributes:
                        self._attr_extra_state_attributes[key] = value

                # If there's a timestamp in the JSON, use it
                if "timestamp" in json_data:
                    self._attr_extra_state_attributes["device_timestamp"] = json_data["timestamp"]

        except (ValueError, json.JSONDecodeError):
            # Not JSON, that's fine
            pass

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        _LOGGER.debug("Turning on switch: %s using command: %s", self.name, self._command)

        # Use the command function to send the command
        result = await self._command_function(
            command=self._command,
            parameters="on",
        )

        if result["success"]:
            self._attr_is_on = True
            self.async_write_ha_state()
        else:
            _LOGGER.error("Failed to turn on switch %s: %s", self.name, result.get("error"))

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        _LOGGER.debug("Turning off switch: %s using command: %s", self.name, self._command)

        # Use the command function to send the command
        result = await self._command_function(
            command=self._command,
            parameters="off",
        )

        if result["success"]:
            self._attr_is_on = False
            self.async_write_ha_state()
        else:
            _LOGGER.error("Failed to turn off switch %s: %s", self.name, result.get("error"))
