"""Base entity for Anker Solix integration."""

from __future__ import annotations

import logging
from typing import Any, Callable, Coroutine, TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

if TYPE_CHECKING:
    from .coordinator import AnkerSolixOfficialCoordinator

_LOGGER = logging.getLogger(__name__)


class AnkerSolixBaseEntity(CoordinatorEntity):
    """Base class for Anker Solix entities."""

    def __init__(
        self,
        coordinator: "AnkerSolixOfficialCoordinator",
        entity_key: str,
        entity_config: dict[str, Any],
    ) -> None:
        """Initialize base entity.

        Args:
            coordinator: Data coordinator instance
            entity_key: Unique entity key
            entity_config: Entity configuration dict
        """
        super().__init__(coordinator)
        self._entity_key = entity_key
        self._config = entity_config

        # Set common attributes
        self._attr_has_entity_name = True
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{entity_key}"
        self._attr_translation_key = entity_config.get("translation_key", entity_key)
        self._attr_device_info = coordinator.device_info

        # Set icon if configured
        if "icon" in entity_config:
            self._attr_icon = entity_config["icon"]

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.is_connected()

    def _get_raw_value(self, default: Any = None) -> Any:
        """Get raw value from coordinator data.

        If write protection is active for this entity, returns the protected value
        instead of the actual device value. This prevents UI "flash back" when
        device is still processing a write command.

        Args:
            default: Default value if not found

        Returns:
            Raw value from coordinator data (or protected value if active)
        """
        # Check if this entity has write protection active
        is_protected, protected_value = self.coordinator.get_protected_value(self._entity_key)
        if is_protected:
            _LOGGER.debug(
                "Entity %s using protected value: %s (device value: %s)",
                self._entity_key,
                protected_value,
                self.coordinator.data.get(self._entity_key) if self.coordinator.data else None,
            )
            return protected_value

        if not self.coordinator.data:
            return default
        return self.coordinator.data.get(self._entity_key, default)


async def async_setup_entities_with_retry(
    hass: HomeAssistant,
    coordinator: "AnkerSolixOfficialCoordinator",
    async_add_entities: AddEntitiesCallback,
    entity_filter: Callable[[str, dict], bool],
    entity_factory: Callable[["AnkerSolixOfficialCoordinator", str, dict], Any],
    platform_name: str,
) -> None:
    """Set up entities with retry logic for delayed configuration.

    Args:
        hass: Home Assistant instance
        coordinator: Data coordinator
        async_add_entities: Callback to add entities
        entity_filter: Function to filter which configs to create entities for
        entity_factory: Function to create entity from config
        platform_name: Platform name for logging
    """
    # Try to get configuration
    data_points = await coordinator.ensure_config_ready()
    if not data_points:
        data_points = await coordinator.get_device_data_points()

    if data_points:
        # Configuration available, create entities immediately
        entities = [
            entity_factory(coordinator, key, config)
            for key, config in data_points.items()
            if entity_filter(key, config)
        ]
        if entities:
            async_add_entities(entities)
            _LOGGER.debug("Added %d %s entities", len(entities), platform_name)
        return

    # Configuration not ready, set up deferred loading
    _LOGGER.debug(
        "No device configuration available for %s, deferring %s setup",
        coordinator.ip_address,
        platform_name,
    )

    state = {"added": False}
    remove_token: dict[str, Callable | None] = {"fn": None}

    async def _try_add_entities() -> None:
        if state["added"]:
            return
        dps = await coordinator.get_device_data_points()
        if not dps:
            return
        entities = [
            entity_factory(coordinator, key, config)
            for key, config in dps.items()
            if entity_filter(key, config)
        ]
        if entities:
            async_add_entities(entities)
            state["added"] = True
            _LOGGER.debug("Deferred setup: added %d %s entities", len(entities), platform_name)
            if remove_token["fn"]:
                remove_token["fn"]()

    def _listener() -> None:
        coordinator.hass.async_create_task(_try_add_entities())

    remove_token["fn"] = coordinator.async_add_listener(_listener)
    await _try_add_entities()
