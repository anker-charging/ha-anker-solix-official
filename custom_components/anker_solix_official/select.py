"""Select platform."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import AnkerSolixOfficialCoordinator
from .base_entity import AnkerSolixBaseEntity, async_setup_entities_with_retry

_LOGGER = logging.getLogger(__name__)


def _is_select_entity(key: str, config: dict) -> bool:
    """Check if config represents a select entity."""
    return (
        config.get("data_type_category") == "control"
        and config.get("display_type") == "select"
    )


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up select platform."""
    coordinator: AnkerSolixOfficialCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    await async_setup_entities_with_retry(
        hass=hass,
        coordinator=coordinator,
        async_add_entities=async_add_entities,
        entity_filter=_is_select_entity,
        entity_factory=lambda c, k, cfg: ModbusLocalDeviceSelect(c, k, cfg),
        platform_name="select",
    )


class ModbusLocalDeviceSelect(AnkerSolixBaseEntity, SelectEntity):
    """Modbus local device select entity."""

    def __init__(
        self,
        coordinator: AnkerSolixOfficialCoordinator,
        key: str,
        config: dict[str, Any],
    ) -> None:
        """Initialize select."""
        super().__init__(coordinator, key, config)

        # Set default icon if not configured
        if not self._attr_icon:
            self._attr_icon = "mdi:menu"

        # Build option mappings (full list, will be filtered dynamically)
        options = config.get("options", {})
        self._all_options = options  # value -> translation_key
        self._all_translation_keys = list(options.values())
        self._options_map = {v: k for k, v in options.items()}  # translation_key -> value
        self._reverse_options_map = options  # value -> translation_key

        # Capability filtering config
        self._capability_entity = config.get("capability_entity")
        self._option_capability_bits = config.get("option_capability_bits", {})

        # Track if default direction has been auto-selected (to avoid duplicate logbook entries)
        self._default_direction_logged = False

    def _get_capability_mask(self) -> int | None:
        """Get the capability mask from the capability entity."""
        if not self._capability_entity:
            return None
        if not self.coordinator.data:
            return None
        mask_value = self.coordinator.data.get(self._capability_entity)
        if mask_value is None:
            return None
        try:
            return int(mask_value)
        except (ValueError, TypeError):
            return None

    def _get_filtered_options(self) -> list[str]:
        """Get options filtered by capability mask."""
        if not self._capability_entity or not self._option_capability_bits:
            # No filtering configured, return all options
            return self._all_translation_keys

        mask = self._get_capability_mask()
        if mask is None:
            # No mask available yet, return all options
            return self._all_translation_keys

        filtered = []
        for value, translation_key in self._all_options.items():
            bit_position = self._option_capability_bits.get(value)
            if bit_position is None:
                # No bit requirement, always include
                filtered.append(translation_key)
            elif mask & (1 << bit_position):
                # Bit is set, include this option
                filtered.append(translation_key)
            else:
                _LOGGER.debug(
                    "Option %s (value=%s) filtered out: BIT%d not set in mask 0x%04X",
                    translation_key, value, bit_position, mask
                )
        return filtered

    @property
    def available(self) -> bool:
        """Return if entity is available.

        Supports visibility_bit for bit-based visibility check.
        """
        if not self.coordinator.is_connected():
            return False

        visibility_entity = self._config.get("visibility_entity")
        if visibility_entity:
            visibility_bit = self._config.get("visibility_bit")
            if visibility_bit is not None:
                # Bit-based visibility check
                if self.coordinator.data:
                    mask_value = self.coordinator.data.get(visibility_entity)
                    if mask_value is None:
                        return False
                    try:
                        mask = int(mask_value)
                        return bool(mask & (1 << visibility_bit))
                    except (ValueError, TypeError):
                        return False
                return False
            else:
                # Value-based visibility check (legacy)
                visibility_value = self._config.get("visibility_value")
                if self.coordinator.data:
                    current_value = self.coordinator.data.get(visibility_entity)
                    if current_value is None:
                        return False
                    try:
                        return int(current_value) == int(visibility_value)
                    except (ValueError, TypeError):
                        return False
                return False

        return True

    @property
    def options(self) -> list[str]:
        """Return options list filtered by capability mask."""
        return self._get_filtered_options()

    @property
    def current_option(self) -> str | None:
        """Return currently selected option."""
        if not self.available:
            return None

        # For direction selector: auto-fill with default if not selected
        if self._config.get("is_direction_selector"):
            user_selection = self.coordinator.get_user_selection(self._entity_key)
            if user_selection is None:
                # Auto-select default direction (charge) and store it
                default_direction = "charge"
                self.coordinator.set_user_selection(self._entity_key, default_direction)
                _LOGGER.info(
                    "Auto-selected default direction: %s (user can change it)",
                    default_direction
                )
                # Log to HA logbook to inform user (only once)
                if not self._default_direction_logged:
                    self._default_direction_logged = True
                    self.hass.async_create_task(
                        self.hass.services.async_call(
                            "logbook",
                            "log",
                            {
                                "name": self.coordinator.device_name or "Anker Solix",
                                "message": "Charge/discharge direction auto-set to: Charge (can be changed manually)",
                                "entity_id": self.entity_id,
                                "domain": DOMAIN,
                            },
                            blocking=False,
                        )
                    )
                return default_direction
            return user_selection

        # For normal select entities: check write protection first
        is_protected, protected_value = self.coordinator.get_protected_value(self._entity_key)
        if is_protected and protected_value is not None:
            return protected_value

        # For normal select entities: read from device
        value = self._get_raw_value()
        if value is None:
            return None

        # Convert numeric value to translation key
        translation_key = self._reverse_options_map.get(str(value))
        return translation_key

    async def async_select_option(self, option: str) -> None:
        """Select option."""
        # For direction selector: only store user's selection (no register write)
        if self._config.get("is_direction_selector"):
            # Store user's selection (persists until user changes it or HA restarts)
            self.coordinator.set_user_selection(self._entity_key, option)
            self.async_write_ha_state()
            _LOGGER.info(
                "Direction selector %s set to '%s' (stored in memory, not written to device)",
                self._entity_key, option
            )
            return

        # Convert translation key to numeric value
        value = self._options_map.get(option)
        if value is None:
            _LOGGER.error("Failed to map option '%s' to numeric value", option)
            return

        address = self._config.get("address")
        if address is None:
            _LOGGER.error("Select %s has no address configured", self._entity_key)
            return

        try:
            address = int(address)
            value = int(value)
        except (ValueError, TypeError) as e:
            _LOGGER.error("Invalid address or value for select %s: %s", self._entity_key, e)
            return

        data_type = self._config.get("data_type", "UINT16")

        _LOGGER.info(
            "Writing select %s | address=%d (0x%04X), option='%s', value=%d, data_type=%s",
            self._entity_key, address, address, option, value, data_type
        )

        try:
            success = await self.coordinator.modbus_manager.write_register(
                address, value, data_type
            )

            if success:
                _LOGGER.info(
                    "Write select SUCCESS | %s: option='%s', value=%d, address=%d (0x%04X)",
                    self._entity_key, option, value, address, address
                )

                # Enable write protection to prevent UI flashing during mode transition
                # Device may take several seconds to process mode change
                protection_duration = self._config.get("write_protection_duration", 15.0)
                self.coordinator.set_write_protection(
                    self._entity_key, option, protection_duration
                )

                # Update UI immediately with user's selection
                self.async_write_ha_state()

                # Note: Do NOT call async_request_refresh() here
                # Let the normal 5-second polling update the value after protection expires
            else:
                _LOGGER.error(
                    "Write select FAILED | %s: option='%s', value=%d, address=%d (0x%04X), result=False",
                    self._entity_key, option, value, address, address
                )
        except Exception as e:
            _LOGGER.error(
                "Write select EXCEPTION | %s: option='%s', value=%d, address=%d (0x%04X), error=%s",
                self._entity_key, option, value, address, address, e
            )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes."""
        return {
            "modbus_address": self._config.get("address"),
            "data_type": self._config.get("data_type"),
            "register_count": self._config.get("count"),
            "available_options": self._config.get("options"),
        }
