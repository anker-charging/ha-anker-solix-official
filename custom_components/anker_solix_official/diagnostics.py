"""Diagnostics support for Anker Solix Official integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

TO_REDACT = {"ip_address", "device_sn", "device_name"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    return {
        "config_entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "connection": {
            "last_update_success": coordinator.last_update_success,
            "consecutive_failures": coordinator._consecutive_failures,
            "ever_connected": coordinator._ever_connected,
            "initial_mode_sent": coordinator._initial_mode_sent,
            "ip_address": async_redact_data(
                {"ip_address": coordinator.ip_address}, TO_REDACT
            )["ip_address"],
            "ip_matches_config_entry": coordinator.ip_address
            == entry.data.get("ip_address"),
        },
        "device": {
            "model": coordinator.device_info.get("model"),
            "firmware": (coordinator.data or {}).get("device_sw_version"),
        },
        "register_data": async_redact_data(coordinator.data or {}, TO_REDACT),
    }
