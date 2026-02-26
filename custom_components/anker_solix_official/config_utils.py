"""Configuration parsing utilities to eliminate code duplication."""
from __future__ import annotations

from typing import Any, Iterable


def _parse_range_string(range_str: str) -> tuple[int, int] | None:
    """Parse a single range string like '10000-10050' into (start, end) tuple."""
    parts = [p.strip() for p in range_str.replace(" ", "").split("-") if p.strip()]
    if len(parts) == 2 and all(part.isdigit() for part in parts):
        start, end = int(parts[0]), int(parts[1])
        if start > end:
            start, end = end, start
        return (start, end)
    return None


def _parse_batch_ranges(raw_ranges: Any) -> list[tuple[int, int, str]]:
    """Parse batch read ranges from configuration with register type.

    Supports new format with holding/input types:
        batch_read_ranges:
          input:
            - 10000-10050
            - 32768-32774
          holding:
            - 10060-10072

    Also supports legacy format (defaults to input type):
        batch_read_ranges:
          - 10000-10074
          - 32768-32774

    Returns:
        List of (start, end, register_type) tuples.
        register_type is either "holding" or "input".
    """
    ranges: list[tuple[int, int, str]] = []
    if not raw_ranges:
        return ranges

    # New format: dict with 'holding' and/or 'input' keys
    if isinstance(raw_ranges, dict):
        for reg_type in ("holding", "input"):
            type_ranges = raw_ranges.get(reg_type)
            if not type_ranges:
                continue
            if isinstance(type_ranges, list):
                for item in type_ranges:
                    if isinstance(item, str):
                        parsed = _parse_range_string(item)
                        if parsed:
                            ranges.append((parsed[0], parsed[1], reg_type))
        return ranges

    # Legacy format: list of range strings (default to input type)
    items: Iterable[Any]
    if isinstance(raw_ranges, str):
        items = [part.strip() for part in raw_ranges.split(",") if part.strip()]
    elif isinstance(raw_ranges, Iterable):
        items = raw_ranges
    else:
        return ranges

    for item in items:
        if isinstance(item, str):
            parsed = _parse_range_string(item)
            if parsed:
                ranges.append((parsed[0], parsed[1], "input"))

    return ranges


def parse_device_configuration(cfg: dict) -> tuple[dict[str, Any], list[tuple[int, int, str]]]:
    """Parse device configuration and extract all data points and batch read ranges.

    This function consolidates configuration parsing logic that was duplicated
    across coordinator.py (3 locations) and other modules.

    Args:
        cfg: Device configuration dictionary loaded from YAML

    Returns:
        Tuple of (data points dict, batch read ranges list)
    """
    if not isinstance(cfg, dict):
        return {}, []

    data_points: dict[str, Any] = {}

    # 1) Read sensor data points from multiple sections
    for section in ("read_quantities", "control_items", "controls", "data_points"):
        part = cfg.get(section)
        if isinstance(part, dict):
            data_points.update(part)

    # 2) Parse write quantities (enumeration selections and switches)
    write_cfg = cfg.get("write_quantities")
    if isinstance(write_cfg, dict):
        enum_cfg = write_cfg.get("enumeration_selection")
        if isinstance(enum_cfg, dict):
            for k, item in enum_cfg.items():
                if not isinstance(item, dict):
                    continue

                addr = item.get("address")
                dtype = item.get("data_type")

                if addr is None or dtype is None:
                    continue

                # Determine control type and display type
                control_type = item.get("control_type", "select")
                if control_type == "switch":
                    display_type = "switch"
                else:
                    display_type = "select"

                # Build data point configuration
                dp = {
                    "address": addr,
                    "data_type": dtype,
                    "count": item.get("count", 1),
                    "translation_key": item.get("translation_key", k),
                    "icon": item.get("icon"),
                    "unit": item.get("unit", "/"),
                    "gain": item.get("gain", 1),
                    "data_type_category": "control",
                    "control_type": control_type,
                    "display_type": display_type,
                    "options": item.get("options", {}),
                }
                # Support separate read entity for switches with read-only status registers
                if item.get("read_entity_key") is not None:
                    dp["read_entity_key"] = item.get("read_entity_key")
                # Support direction selector flag
                if item.get("is_direction_selector"):
                    dp["is_direction_selector"] = True
                # Support capability filtering
                if item.get("capability_entity"):
                    dp["capability_entity"] = item.get("capability_entity")
                if item.get("option_capability_bits"):
                    dp["option_capability_bits"] = item.get("option_capability_bits")
                # Support visibility control
                if item.get("visibility_entity"):
                    dp["visibility_entity"] = item.get("visibility_entity")
                if item.get("visibility_value") is not None:
                    dp["visibility_value"] = item.get("visibility_value")
                if item.get("visibility_bit") is not None:
                    dp["visibility_bit"] = item.get("visibility_bit")
                data_points[k] = dp

    batch_ranges = _parse_batch_ranges(cfg.get("batch_read_ranges"))

    return data_points, batch_ranges
