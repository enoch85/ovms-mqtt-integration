"""Utility functions for OVMS integration."""
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple, Union

from homeassistant.const import (
    UnitOfLength,
    UnitOfMass,
    UnitOfSpeed,
    UnitOfTemperature,
    UnitOfVolume,
)

from .const import LOGGER_NAME

_LOGGER = logging.getLogger(LOGGER_NAME)


def convert_temperature(value: float, to_unit: str) -> float:
    """Convert temperature between units."""
    if to_unit == UnitOfTemperature.CELSIUS:
        return value
    elif to_unit == UnitOfTemperature.FAHRENHEIT:
        return (value * 9/5) + 32
    return value


def convert_distance(value: float, to_unit: str) -> float:
    """Convert distance between units."""
    if to_unit == UnitOfLength.KILOMETERS:
        return value
    elif to_unit == UnitOfLength.MILES:
        return value * 0.621371
    return value


def convert_speed(value: float, to_unit: str) -> float:
    """Convert speed between units."""
    if to_unit == UnitOfSpeed.KILOMETERS_PER_HOUR:
        return value
    elif to_unit == UnitOfSpeed.MILES_PER_HOUR:
        return value * 0.621371
    return value


def convert_volume(value: float, to_unit: str) -> float:
    """Convert volume between units."""
    if to_unit == UnitOfVolume.LITERS:
        return value
    elif to_unit == UnitOfVolume.GALLONS:
        return value * 0.264172
    return value


def get_unit_system(use_metric: bool) -> Dict[str, str]:
    """Get the unit system based on preference."""
    if use_metric:
        return {
            "temperature": UnitOfTemperature.CELSIUS,
            "distance": UnitOfLength.KILOMETERS,
            "speed": UnitOfSpeed.KILOMETERS_PER_HOUR,
            "volume": UnitOfVolume.LITERS,
            "mass": UnitOfMass.KILOGRAMS,
        }
    else:
        return {
            "temperature": UnitOfTemperature.FAHRENHEIT,
            "distance": UnitOfLength.MILES,
            "speed": UnitOfSpeed.MILES_PER_HOUR,
            "volume": UnitOfVolume.GALLONS,
            "mass": UnitOfMass.POUNDS,
        }


def clean_topic(topic: str) -> str:
    """Clean special characters from topic for use in entity IDs."""
    return topic.replace("/", "_").replace("#", "all").replace("+", "any")


def parse_numeric_value(value: Any) -> Optional[float]:
    """Parse a numeric value from various input types."""
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return float(value)

    if isinstance(value, str):
        try:
            # Remove units and other non-numeric characters
            numeric_str = re.sub(r'[^\d.-]', '', value)
            return float(numeric_str)
        except (ValueError, TypeError):
            pass

    return None


def extract_value_from_json(json_str: str, key_path: Optional[str] = None) -> Any:
    """Extract a specific value from a JSON string using a key path.

    Key path can be in dot notation, e.g. "battery.soc" to access nested objects.
    """
    try:
        data = json.loads(json_str)

        if not key_path:
            return data

        # Navigate through the key path
        keys = key_path.split(".")
        result = data
        for key in keys:
            if isinstance(result, dict) and key in result:
                result = result[key]
            else:
                return None

        return result
    except (json.JSONDecodeError, ValueError):
        return None


def safe_float(value: Any) -> Optional[float]:
    """Safely convert a value to float."""
    if value is None:
        return None

    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def topic_matches_pattern(topic: str, pattern: str) -> bool:
    """Check if a topic matches a pattern, handling MQTT wildcards.

    Supports + (single level) and # (multi level) wildcards.
    """
    # Convert MQTT wildcards to regex patterns
    regex_pattern = pattern.replace("+", "[^/]+").replace("#", ".*")
    # Add start/end markers
    regex_pattern = f"^{regex_pattern}$"

    return bool(re.match(regex_pattern, topic))


def generate_unique_id(components: List[str]) -> str:
    """Generate a unique ID from multiple components.

    This ensures the ID is URL and filesystem safe.
    """
    import hashlib

    # Join components and create hash
    combined = "_".join(str(c) for c in components if c)
    if not combined:
        return "unknown"

    # Create a hash if the string is too long
    if len(combined) > 32:
        return hashlib.md5(combined.encode()).hexdigest()

    # Otherwise just clean up the string
    return re.sub(r'[^a-zA-Z0-9_]', '_', combined)


def parse_gps_coordinates(payload: str) -> Tuple[Optional[float], Optional[float]]:
    """Parse GPS coordinates from various payload formats."""
    # Try to parse as JSON first
    try:
        data = json.loads(payload)

        # Common field names for latitude and longitude
        lat_names = ["lat", "latitude", "LAT", "Latitude"]
        lon_names = ["lon", "lng", "longitude", "LON", "Longitude"]

        lat = None
        lon = None

        if isinstance(data, dict):
            # Check for various field names
            for lat_field in lat_names:
                if lat_field in data:
                    lat = safe_float(data[lat_field])
                    break

            for lon_field in lon_names:
                if lon_field in data:
                    lon = safe_float(data[lon_field])
                    break

            if lat is not None and lon is not None:
                return lat, lon

        # If JSON parsing didn't yield coordinates, try other formats
    except (json.JSONDecodeError, ValueError):
        pass

    # Try comma-separated values
    if isinstance(payload, str) and "," in payload:
        parts = payload.split(",")
        if len(parts) >= 2:
            try:
                lat = float(parts[0].strip())
                lon = float(parts[1].strip())

                # Validate coordinates are in the right range
                if -90 <= lat <= 90 and -180 <= lon <= 180:
                    return lat, lon
            except (ValueError, TypeError):
                pass

    return None, None


def format_command_parameters(command: str, parameters: Union[str, Dict[str, Any]]) -> str:
    """Format command parameters for OVMS command execution."""
    if not parameters:
        return command

    if isinstance(parameters, dict):
        # Convert dict to space-separated key=value pairs
        param_str = " ".join(f"{k}={v}" for k, v in parameters.items())
        return f"{command} {param_str}"

    # If parameters is already a string, just append it
    return f"{command} {parameters}"
