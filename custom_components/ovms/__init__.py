"""The OVMS integration."""
import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    DOMAIN,
    LOGGER_NAME,
    CONFIG_VERSION,
    SIGNAL_PLATFORMS_LOADED
)

from .mqtt import OVMSMQTTClient
from .services import async_setup_services, async_unload_services

_LOGGER = logging.getLogger(LOGGER_NAME)

PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.DEVICE_TRACKER,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up OVMS from a config entry."""
    _LOGGER.info("Setting up OVMS integration")

    hass.data.setdefault(DOMAIN, {})

    # Check if we need to migrate the config entry
    if entry.version < CONFIG_VERSION:
        _LOGGER.info("Migrating config entry from version %s to %s", entry.version, CONFIG_VERSION)
        await async_migrate_entry(hass, entry)

    # Create MQTT client
    mqtt_client = OVMSMQTTClient(hass, entry.data)

    # Set up the MQTT client
    if not await mqtt_client.async_setup():
        _LOGGER.error("Failed to set up MQTT client")
        return False

    # Store the client in hass.data
    hass.data[DOMAIN][entry.entry_id] = {
        "mqtt_client": mqtt_client,
    }

    # Set up platforms
    _LOGGER.debug("Setting up platforms: %s", PLATFORMS)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Set up services
    await async_setup_services(hass)

    # Update listener for config entry changes
    entry.async_on_unload(entry.add_update_listener(async_update_options))

    # Signal that all platforms are loaded
    _LOGGER.info("All platforms loaded, notifying MQTT client")
    async_dispatcher_send(hass, SIGNAL_PLATFORMS_LOADED)

    return True


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate an old config entry to new version."""
    version = config_entry.version

    # If the config entry is already up-to-date, return True
    if version == CONFIG_VERSION:
        return True

    _LOGGER.debug("Migrating config entry from version %s to %s", version, CONFIG_VERSION)

    # Perform migrations based on version
    if version == 1:
        # Example migration code for future updates
        # new_data = {**config_entry.data}
        # new_data["new_field"] = "default_value"
        # hass.config_entries.async_update_entry(config_entry, data=new_data)
        # version = 2
        pass

    # Update the config entry version
    hass.config_entries.async_update_entry(config_entry, version=CONFIG_VERSION)
    return True


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update options."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.info("Unloading OVMS integration")

    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        # Shut down MQTT client
        mqtt_client = hass.data[DOMAIN][entry.entry_id]["mqtt_client"]
        await mqtt_client.async_shutdown()

        # Remove config entry from hass.data
        hass.data[DOMAIN].pop(entry.entry_id)

        # Unload services if this is the last config entry
        if len(hass.data[DOMAIN]) == 0:
            await async_unload_services(hass)

    return unload_ok
