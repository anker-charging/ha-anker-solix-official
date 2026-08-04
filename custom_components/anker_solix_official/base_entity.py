"""Base entity for Anker Solix integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Coroutine, TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.translation import async_get_translations
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, WRITE_CONDITION_REVERT_DELAY

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
        self._register_address = entity_config.get("address")

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
        if not self.coordinator.is_connected():
            return False
        
        if self._register_address is not None:
            if not self.coordinator.is_register_available(self._register_address):
                return False
        
        return True

    def _is_capability_supported(self) -> bool:
        """Check capability_entity + capability_bit gate (parallel machine capability negotiation).

        Returns True (fail-open) whenever the capability mask cannot be
        determined yet: this covers both "never successfully read" (key
        absent from coordinator.data, e.g. address still a TODO placeholder)
        and "read attempt failed" (register marked unavailable via
        is_register_available), so entities are only hidden once the mask
        has actually been read and a bit is confirmed unset.
        """
        capability_entity = self._config.get("capability_entity")
        capability_bit = self._config.get("capability_bit")
        if not capability_entity or capability_bit is None:
            return True

        if not self.coordinator.data or capability_entity not in self.coordinator.data:
            _LOGGER.debug(
                "Capability gate: %s fail-open (key '%s' not in coordinator.data)",
                self._entity_key,
                capability_entity,
            )
            return True

        mask_address = self.coordinator.get_data_point_address(capability_entity)
        if mask_address is not None and not self.coordinator.is_register_available(
            mask_address
        ):
            _LOGGER.debug(
                "Capability gate: %s fail-open (register %d marked unavailable)",
                self._entity_key,
                mask_address,
            )
            return True

        mask_value = self.coordinator.data.get(capability_entity)
        try:
            mask = int(mask_value)
        except (ValueError, TypeError):
            _LOGGER.debug(
                "Capability gate: %s fail-open (mask value %r is not int)",
                self._entity_key,
                mask_value,
            )
            return True

        result = bool(mask & (1 << capability_bit))
        _LOGGER.debug(
            "Capability gate: %s mask=0x%04X bit=%d -> %s (entity will be %s)",
            self._entity_key,
            mask,
            capability_bit,
            result,
            "visible" if result else "HIDDEN",
        )
        return result

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

    def _check_write_condition(self) -> tuple[bool, str | None]:
        """Check write_condition before writing.

        Returns:
            (passed, hint_translation_key)
        """
        condition = self._config.get("write_condition")
        if not condition:
            return True, None

        if not self.coordinator.data:
            _LOGGER.warning(
                "Write condition check failed for %s: coordinator.data is None",
                self._entity_key,
            )
            return False, condition.get("hint")

        entity_key = condition.get("entity")
        if not entity_key:
            return True, None

        current_value = self.coordinator.data.get(entity_key)
        if current_value is None:
            _LOGGER.warning(
                "Write condition check failed for %s: entity '%s' not found in coordinator.data (available keys: %s)",
                self._entity_key,
                entity_key,
                list(self.coordinator.data.keys())[:10],
            )
            return False, condition.get("hint")

        try:
            current_value = float(current_value)
        except (ValueError, TypeError):
            _LOGGER.warning(
                "Write condition check failed for %s: cannot convert '%s' to float",
                self._entity_key,
                current_value,
            )
            return False, condition.get("hint")

        operator = condition.get("operator", "eq")
        target = condition.get("value")

        passed = self._evaluate_operator(current_value, operator, target)
        _LOGGER.debug(
            "Write condition check for %s: %s %s %s = %s (current_value=%s)",
            self._entity_key,
            current_value,
            operator,
            target,
            passed,
            current_value,
        )
        return passed, condition.get("hint") if not passed else None

    @staticmethod
    def _evaluate_operator(value: float, operator: str, target: Any) -> bool:
        """Evaluate a comparison operator with float tolerance."""
        # Defensive check: if target is None, treat as pass (no condition to check)
        if target is None:
            return True
        
        # For list-based operators, ensure target is iterable
        if operator in ("in", "not_in"):
            if not isinstance(target, (list, tuple, set)):
                target = [target]
            return (
                any(abs(value - float(t)) < 0.5 for t in target)
                if operator == "in"
                else not any(abs(value - float(t)) < 0.5 for t in target)
            )
        
        # For numeric comparisons, ensure target is numeric
        if not isinstance(target, (int, float)):
            try:
                target = float(target)
            except (ValueError, TypeError):
                return True
        
        if operator == "eq":
            if isinstance(target, int) or (isinstance(target, float) and target == int(target)):
                return abs(value - target) < 0.5
            return abs(value - target) < 1e-6
        elif operator == "ne":
            if isinstance(target, int) or (isinstance(target, float) and target == int(target)):
                return abs(value - target) >= 0.5
            return abs(value - target) >= 1e-6
        elif operator == "gt":
            return value > target
        elif operator == "gte":
            return value >= target
        elif operator == "lt":
            return value < target
        elif operator == "lte":
            return value <= target
        
        return True

    async def _revert_ui_state(self, user_value: Any) -> None:
        """Revert UI state to device value after validation failure.

        Forces a state change event so the frontend receives the revert update,
        then clears the user_selection after a delay to show the device value.

        Args:
            user_value: The user's attempted value (used to force state change).
        """
        self.coordinator.set_user_selection(self._entity_key, user_value)
        self.async_write_ha_state()

        async def _revert_state():
            await asyncio.sleep(WRITE_CONDITION_REVERT_DELAY)
            if self.hass and self.entity_id in self.hass.states.async_entity_ids():
                self.coordinator.clear_user_selection(self._entity_key)
                self.async_write_ha_state()

        self.hass.async_create_task(_revert_state())

    async def _raise_write_condition_error(
        self, hint_key: str | None, user_value: Any = None, persist_user_value: bool = False
    ) -> None:
        """Handle write_condition failure: persist UI state and raise error.

        This ensures the frontend correctly reverts to the device value instead
        of keeping an optimistic update after a failed write.

        Args:
            hint_key: Translation key for the error message.
            user_value: If provided, save to user_selections to force state change.
            persist_user_value: If True, keep user_selection (for select/switch).
                               If False, clear it after forcing state change (for number).
        """
        if user_value is not None:
            if persist_user_value:
                # select/switch: keep user_selection, just update UI
                self.coordinator.set_user_selection(self._entity_key, user_value)
                self.async_write_ha_state()
            else:
                # number: use revert mechanism to show device value
                await self._revert_ui_state(user_value)

        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key=hint_key or "write_condition_not_met",
        )

    async def _show_soft_warning(
        self,
        warning_key: str,
        placeholders: dict[str, str] | None = None,
    ) -> None:
        """Show a non-blocking warning notification.

        Uses persistent_notification to display a warning in the Notifications
        panel (bell icon in sidebar). Does not block the operation.

        Args:
            warning_key: Translation key for the warning message.
            placeholders: Optional dict of placeholder values for the message.
        """
        if not self.hass:
            _LOGGER.warning("Cannot show soft warning: hass not available")
            return

        # hass.data has no built-in "translations" key; the official way to
        # resolve a translated exceptions.*.message string at runtime is via
        # the translation helper, using the user's configured language.
        language = self.hass.config.language
        localize_key = f"component.{DOMAIN}.exceptions.{warning_key}.message"
        try:
            translations = await async_get_translations(
                self.hass, language, "exceptions", {DOMAIN}
            )
        except Exception as err:
            _LOGGER.debug(
                "Failed to load translations for soft warning %s: %s", warning_key, err
            )
            translations = {}
        message = translations.get(localize_key, warning_key)

        if placeholders:
            for key, value in placeholders.items():
                message = message.replace(f"{{{key}}}", str(value))

        notification_id = f"{DOMAIN}_{warning_key}_{self._entity_key}"
        await self.hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "message": message,
                "title": "Anker SOLIX Warning",
                "notification_id": notification_id,
            },
        )

        _LOGGER.info(
            "Soft warning shown for %s: %s (placeholders: %s)",
            self._entity_key,
            warning_key,
            placeholders,
        )

    async def _dismiss_soft_warning(self, warning_key: str) -> None:
        """Dismiss a previously shown soft warning notification, if any.

        Mirrors the notification_id built in _show_soft_warning, so any
        entity/rule combination using that method to create a warning can
        reuse this to clear it once the triggering condition no longer
        holds. Safe to call unconditionally: dismissing a notification_id
        that was never created (or already dismissed) is a silent no-op.

        Args:
            warning_key: Same key used when the warning was created.
        """
        if not self.hass:
            return

        notification_id = f"{DOMAIN}_{warning_key}_{self._entity_key}"
        await self.hass.services.async_call(
            "persistent_notification",
            "dismiss",
            {"notification_id": notification_id},
        )



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
