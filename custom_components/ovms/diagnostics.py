"""Diagnostics support for OVMS."""
from typing import Any, Dict

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .const import DOMAIN

# Fields to redact from diagnostic data
REDACT_FIELDS = {
    CONF_PASSWORD,
    CONF_USERNAME,
    "credentials",
    "auth",
    "password",
    "token",
    "secret"
}

async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> Dict[str, Any]:
    """Return diagnostics for a config entry."""
    mqtt_client = hass.data[DOMAIN][entry.entry_id]["mqtt_client"]

    # Get basic diagnostic info
    diagnostics_data = {
        "config_entry": {
            "entry_id": entry.entry_id,
            "version": entry.version,
            "domain": entry.domain,
            "title": entry.title,
            "data": async_redact_data(entry.data, REDACT_FIELDS),
            "options": async_redact_data(entry.options, REDACT_FIELDS),
        },
        "mqtt_connection": {
            "connected": mqtt_client.connected,
            "discovered_topics_count": len(mqtt_client.discovered_topics),
            "topic_count": len(mqtt_client.topic_cache),
            "structure_prefix": mqtt_client.structure_prefix,
            "message_count": mqtt_client.message_count,
            "reconnect_count": mqtt_client.reconnect_count,
            "pending_commands": len(mqtt_client.pending_commands),
            "rate_limiter": {
                "calls_remaining": mqtt_client.command_limiter.calls_remaining(),
                "max_calls": mqtt_client.command_limiter.max_calls,
                "period": mqtt_client.command_limiter.period,
            }
        },
        "entities": {
            "total": len(mqtt_client.entity_registry),
            "by_type": mqtt_client.entity_types,
        },
    }

    # Include sample topics (without values)
    sample_topics = list(mqtt_client.topic_cache.keys())[:10]  # First 10 topics

    diagnostics_data["sample_topics"] = {
        topic: {
            "last_payload_length": len(mqtt_client.topic_cache[topic]["payload"])
                if "payload" in mqtt_client.topic_cache[topic] else 0,
            "last_update": mqtt_client.topic_cache[topic].get("timestamp", 0),
        } for topic in sample_topics
    }

    # Include parsed topic information
    topic_parsing_examples = {}
    for topic in sample_topics[:3]:  # Take first 3 for parsing examples
        entity_type, entity_info = mqtt_client._parse_topic(topic)
        if entity_type and entity_info:
            topic_parsing_examples[topic] = {
                "entity_type": entity_type,
                "name": entity_info.get("name", "unknown"),
                "category": entity_info.get("attributes", {}).get("category", "unknown"),
            }

    diagnostics_data["topic_parsing_examples"] = topic_parsing_examples

    return diagnostics_data
