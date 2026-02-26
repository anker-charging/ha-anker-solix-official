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

    # Store coordinator
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Schedule a refresh immediately but don't block; background loop will handle connection and data push
    coordinator.async_set_updated_data({})

    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor", "select", "number", "switch"])

    _LOGGER.info("Successfully set up Anker Solix device at %s", ip_address)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload configuration entry."""
    ip_address = entry.data.get("ip_address", "unknown")
    _LOGGER.info("Unloading Anker Solix integration for device at %s", ip_address)

    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, ["sensor", "select", "number", "switch"])

    if unload_ok:
        # Close coordinator
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()
        _LOGGER.info("Successfully unloaded Anker Solix device at %s", ip_address)
    else:
        _LOGGER.error("Failed to unload Anker Solix device at %s", ip_address)

    return unload_ok
