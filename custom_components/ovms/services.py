"""Services for OVMS integration."""
import logging
import uuid
from typing import Any, Dict, Optional

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.exceptions import HomeAssistantError

from .const import (
    CONF_QOS,
    CONF_TOPIC_PREFIX,
    CONF_VEHICLE_ID,
    DEFAULT_QOS,
    DOMAIN,
    LOGGER_NAME
)
from .utils import format_command_parameters

_LOGGER = logging.getLogger(LOGGER_NAME)

# Service name constants
SERVICE_SEND_COMMAND = "send_command"
SERVICE_SET_FEATURE = "set_feature"
SERVICE_CONTROL_CLIMATE = "control_climate"
SERVICE_CONTROL_CHARGING = "control_charging"

# Schema for the send_command service
SEND_COMMAND_SCHEMA = vol.Schema({
    vol.Required("vehicle_id"): cv.string,
    vol.Required("command"): cv.string,
    vol.Optional("parameters"): cv.string,
    vol.Optional("command_id"): cv.string,
    vol.Optional("timeout"): vol.Coerce(int),
})

# Schema for the set_feature service
SET_FEATURE_SCHEMA = vol.Schema({
    vol.Required("vehicle_id"): cv.string,
    vol.Required("feature"): cv.string,
    vol.Required("value"): cv.string,
})

# Schema for the control_climate service
CONTROL_CLIMATE_SCHEMA = vol.Schema({
    vol.Required("vehicle_id"): cv.string,
    vol.Optional("temperature"): vol.Coerce(float),
    vol.Optional("hvac_mode"): vol.In(["off", "heat", "cool", "auto"]),
    vol.Optional("duration"): vol.Coerce(int),
})

# Schema for the control_charging service
CONTROL_CHARGING_SCHEMA = vol.Schema({
    vol.Required("vehicle_id"): cv.string,
    vol.Required("action"): vol.In(["start", "stop", "status"]),
    vol.Optional("mode"): vol.In(["standard", "storage", "range", "performance"]),
    vol.Optional("limit"): vol.Coerce(int),
})


async def async_setup_services(hass: HomeAssistant) -> None:
    """Set up OVMS services."""

    async def async_find_mqtt_client(vehicle_id: str):
        """Find the MQTT client for a vehicle ID."""
        for entry_id, data in hass.data[DOMAIN].items():
            if isinstance(data, dict) and "mqtt_client" in data:
                entry = hass.config_entries.async_get_entry(entry_id)
                if entry and entry.data.get(CONF_VEHICLE_ID) == vehicle_id:
                    return data["mqtt_client"]

        raise HomeAssistantError(f"No OVMS integration found for vehicle_id: {vehicle_id}")

    async def async_send_command(call: ServiceCall) -> None:
        """Send a command to the OVMS module."""
        vehicle_id = call.data.get("vehicle_id")
        command = call.data.get("command")
        parameters = call.data.get("parameters", "")
        command_id = call.data.get("command_id", str(uuid.uuid4())[:8])
        timeout = call.data.get("timeout", 10)

        _LOGGER.debug("Service call send_command for vehicle %s: %s %s",
                     vehicle_id, command, parameters)

        try:
            # Find the MQTT client for this vehicle
            mqtt_client = await async_find_mqtt_client(vehicle_id)

            # Send the command and get the result
            result = await mqtt_client.async_send_command(
                command=command,
                parameters=parameters,
                command_id=command_id,
                timeout=timeout
            )

            # Return the result as service data
            return result

        except HomeAssistantError as err:
            _LOGGER.error("Error in send_command service: %s", err)
            raise
        except Exception as ex:
            _LOGGER.exception("Unexpected error in send_command service: %s", ex)
            raise HomeAssistantError(f"Failed to send command: {ex}")

    async def async_set_feature(call: ServiceCall) -> None:
        """Set a feature on the OVMS module."""
        vehicle_id = call.data.get("vehicle_id")
        feature = call.data.get("feature")
        value = call.data.get("value")

        _LOGGER.debug("Service call set_feature for vehicle %s: %s=%s",
                     vehicle_id, feature, value)

        try:
            # Find the MQTT client for this vehicle
            mqtt_client = await async_find_mqtt_client(vehicle_id)

            # Format the command
            command = "config set"
            parameters = f"{feature} {value}"

            # Send the command and get the result
            result = await mqtt_client.async_send_command(
                command=command,
                parameters=parameters
            )

            return result

        except HomeAssistantError as err:
            _LOGGER.error("Error in set_feature service: %s", err)
            raise
        except Exception as ex:
            _LOGGER.exception("Unexpected error in set_feature service: %s", ex)
            raise HomeAssistantError(f"Failed to set feature: {ex}")

    async def async_control_climate(call: ServiceCall) -> None:
        """Control the vehicle's climate system."""
        vehicle_id = call.data.get("vehicle_id")
        temperature = call.data.get("temperature")
        hvac_mode = call.data.get("hvac_mode")
        duration = call.data.get("duration")

        _LOGGER.debug("Service call control_climate for vehicle %s", vehicle_id)

        try:
            # Find the MQTT client for this vehicle
            mqtt_client = await async_find_mqtt_client(vehicle_id)

            # Build the climate command
            command = "climate"
            command_parts = []

            if hvac_mode == "off":
                command_parts.append("off")
            else:
                if hvac_mode:
                    command_parts.append(hvac_mode)

                if temperature:
                    command_parts.append(f"temp {temperature}")

                if duration:
                    command_parts.append(f"duration {duration}")

            # Send the command and get the result
            result = await mqtt_client.async_send_command(
                command=command,
                parameters=" ".join(command_parts)
            )

            return result

        except HomeAssistantError as err:
            _LOGGER.error("Error in control_climate service: %s", err)
            raise
        except Exception as ex:
            _LOGGER.exception("Unexpected error in control_climate service: %s", ex)
            raise HomeAssistantError(f"Failed to control climate: {ex}")

    async def async_control_charging(call: ServiceCall) -> None:
        """Control the vehicle's charging system."""
        vehicle_id = call.data.get("vehicle_id")
        action = call.data.get("action")
        mode = call.data.get("mode")
        limit = call.data.get("limit")

        _LOGGER.debug("Service call control_charging for vehicle %s: %s",
                     vehicle_id, action)

        try:
            # Find the MQTT client for this vehicle
            mqtt_client = await async_find_mqtt_client(vehicle_id)

            # Build the charge command
            command = "charge"
            command_parts = [action]

            if mode and action == "start":
                command_parts.append(f"mode {mode}")

            if limit is not None and action == "start":
                command_parts.append(f"limit {limit}")

            # Send the command and get the result
            result = await mqtt_client.async_send_command(
                command=command,
                parameters=" ".join(command_parts)
            )

            return result

        except HomeAssistantError as err:
            _LOGGER.error("Error in control_charging service: %s", err)
            raise
        except Exception as ex:
            _LOGGER.exception("Unexpected error in control_charging service: %s", ex)
            raise HomeAssistantError(f"Failed to control charging: {ex}")

    # Register the services
    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_COMMAND,
        async_send_command,
        schema=SEND_COMMAND_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_FEATURE,
        async_set_feature,
        schema=SET_FEATURE_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_CONTROL_CLIMATE,
        async_control_climate,
        schema=CONTROL_CLIMATE_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_CONTROL_CHARGING,
        async_control_charging,
        schema=CONTROL_CHARGING_SCHEMA,
    )


async def async_unload_services(hass: HomeAssistant) -> None:
    """Unload OVMS services."""
    services = [
        SERVICE_SEND_COMMAND,
        SERVICE_SET_FEATURE,
        SERVICE_CONTROL_CLIMATE,
        SERVICE_CONTROL_CHARGING,
    ]

    for service in services:
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)
