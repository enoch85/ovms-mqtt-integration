"""Utility functions for OVMS metrics."""

from .patterns import TOPIC_PATTERNS

def get_metric_by_path(metric_path):
    """Get metric definition by exact path match."""
    # This function now needs to import METRIC_DEFINITIONS from the metrics module
    # This is a circular import, so we need to import inside the function
    from . import METRIC_DEFINITIONS
    return METRIC_DEFINITIONS.get(metric_path)


def get_metric_by_pattern(topic_parts):
    """Try to match a metric by pattern in topic parts."""
    # First, try to find an exact match of the last path component
    if topic_parts:
        last_part = topic_parts[-1].lower()
        for pattern, info in TOPIC_PATTERNS.items():
            if pattern == last_part:
                return info

    # Then try partial matches in topic parts
    for pattern, info in TOPIC_PATTERNS.items():
        if any(pattern in part.lower() for part in topic_parts):
            return info

    return None


def determine_category_from_topic(topic_parts):
    """Determine the most likely category from topic parts."""
    # This function needs to import categories and PREFIX_CATEGORIES from metrics
    from . import (
        CATEGORY_BATTERY,
        CATEGORY_CHARGING,
        CATEGORY_CLIMATE,
        CATEGORY_DOOR,
        CATEGORY_LOCATION,
        CATEGORY_MOTOR,
        CATEGORY_TRIP,
        CATEGORY_DIAGNOSTIC,
        CATEGORY_POWER,
        CATEGORY_NETWORK,
        CATEGORY_SYSTEM,
        CATEGORY_TIRE,
        CATEGORY_VW_EUP,
        PREFIX_CATEGORIES,
    )

    # Check for known categories in topic
    for part in topic_parts:
        part_lower = part.lower()
        if part_lower in [
            CATEGORY_BATTERY,
            CATEGORY_CHARGING,
            CATEGORY_CLIMATE,
            CATEGORY_DOOR,
            CATEGORY_LOCATION,
            CATEGORY_MOTOR,
            CATEGORY_TRIP,
            CATEGORY_DIAGNOSTIC,
            CATEGORY_POWER,
            CATEGORY_NETWORK,
            CATEGORY_SYSTEM,
            CATEGORY_TIRE,
            CATEGORY_VW_EUP,
        ]:
            return part_lower

    # Try matching by prefix
    full_path = ".".join(topic_parts)
    for prefix, category in PREFIX_CATEGORIES.items():
        if full_path.startswith(prefix):
            return category

    # Default category
    return CATEGORY_SYSTEM


def create_friendly_name(topic_parts, metric_info=None):
    """Create a friendly name from topic parts using metric definitions when available."""
    if not topic_parts:
        return "Unknown"

    # If we have metric info, use its name
    if metric_info and "name" in metric_info:
        return metric_info["name"]

    # Otherwise, build a name from the last part of the topic
    last_part = topic_parts[-1].replace("_", " ").title()

    # Return just the last part without category
    return last_part
