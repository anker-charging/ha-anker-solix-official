"""Anker Solix integration."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import AnkerSolixOfficialCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up configuration entry."""
    ip_address = entry.data.get("ip_address", "unknown")
    _LOGGER.info("Setting up Anker Solix integration for device at %s", ip_address)

    coordinator = AnkerSolixOfficialCoordinator(hass, entry)

    # Raises ConfigEntryNotReady when the device is unreachable. Published to
    # hass.data only after it succeeds, so a failed attempt cannot leave a
    # half-initialized coordinator behind for a later unload to trip over.
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        # The coordinator opened a Modbus connection while trying; release it so
        # a setup that keeps retrying does not accumulate sockets/cleanup tasks.
        await coordinator.async_shutdown()
        raise

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, ["sensor", "select", "number", "switch"])

    # No entry.add_update_listener here on purpose: the coordinator itself calls
    # async_update_entry to persist the resolved title and initial_mode_sent, and
    # a blanket "reload on any change" listener would make those writes reload the
    # integration mid-setup (see issue #117). The reconfigure flow schedules its
    # own reload explicitly.
    _LOGGER.info("Successfully set up Anker Solix device at %s", ip_address)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload configuration entry."""
    ip_address = entry.data.get("ip_address", "unknown")
    _LOGGER.info("Unloading Anker Solix integration for device at %s", ip_address)

    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, ["sensor", "select", "number", "switch"])

    if unload_ok:
        # pop with a default: a previous unload may have already removed the
        # coordinator after being marked FAILED_UNLOAD, and HA retries unload.
        coordinator = hass.data[DOMAIN].pop(entry.entry_id, None)
        if coordinator is not None:
            await coordinator.async_shutdown()
        _LOGGER.info("Successfully unloaded Anker Solix device at %s", ip_address)
    else:
        _LOGGER.error("Failed to unload Anker Solix device at %s", ip_address)

    return unload_ok
