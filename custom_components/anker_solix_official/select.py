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
    coordinator: AnkerSolixOfficialCoordinator = hass.data[DOMAIN][
        config_entry.entry_id
    ]

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
        self._options_map = {
            v: k for k, v in options.items()
        }  # translation_key -> value
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
                    translation_key,
                    value,
                    bit_position,
                    mask,
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
                    default_direction,
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
        is_protected, protected_value = self.coordinator.get_protected_value(
            self._entity_key
        )
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
        # For direction selector: store selection and auto-rewrite power register
        if self._config.get("is_direction_selector"):
            # Store user's selection (persists until user changes it or HA restarts)
            self.coordinator.set_user_selection(self._entity_key, option)
            self.async_write_ha_state()

            # Log to HA logbook + log (same as other control operations)
            device_name = self.coordinator.device_name or "Anker Solix"
            entity_name = self.name or self._entity_key
            display_option = option.replace("_", " ").title()
            log_message = f"{entity_name} → {display_option}"

            await self.coordinator.hass.services.async_call(
                "logbook",
                "log",
                {
                    "name": device_name,
                    "message": log_message,
                    "entity_id": self.entity_id,
                    "domain": DOMAIN,
                },
                blocking=False,
            )

            _LOGGER.info("📝 %s %s → %s", device_name, entity_name, display_option)

            # Auto-rewrite power register with new direction sign
            # Find the power entity that uses this direction selector
            await self._auto_rewrite_power_on_direction_change(option)

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
            _LOGGER.error(
                "Invalid address or value for select %s: %s", self._entity_key, e
            )
            return

        data_type = self._config.get("data_type", "UINT16")

        dlog = self.coordinator.device_logger

        dlog.warning(
            "Writing select %s | address=%d (0x%04X), option='%s', value=%d, data_type=%s",
            self._entity_key,
            address,
            address,
            option,
            value,
            data_type,
        )

        try:
            result = await self.coordinator.modbus_manager.write_register(
                address, value, data_type
            )

            if result.success:
                dlog.warning(
                    "Write select SUCCESS | %s: option='%s', value=%d, address=%d (0x%04X)",
                    self._entity_key,
                    option,
                    value,
                    address,
                    address,
                )

                protection_duration = self._config.get(
                    "write_protection_duration", 15.0
                )
                self.coordinator.set_write_protection(
                    self._entity_key, option, protection_duration
                )

                self.async_write_ha_state()

                device_name = self.coordinator.device_name or "Anker Solix"
                entity_name = self.name or self._entity_key
                display_option = option.replace("_", " ").title()
                log_message = f"{entity_name} → {display_option}"

                await self.coordinator.hass.services.async_call(
                    "logbook",
                    "log",
                    {
                        "name": device_name,
                        "message": log_message,
                        "entity_id": self.entity_id,
                        "domain": DOMAIN,
                    },
                    blocking=False,
                )

                dlog.warning("📝 %s → %s", entity_name, display_option)
            else:
                dlog.error(
                    "Write select FAILED | entity=%s, option='%s', value=%d, address=%d (0x%04X), "
                    "reason=%s, raw_response=%s, tx_frame=%s",
                    self._entity_key,
                    option,
                    value,
                    address,
                    address,
                    result.error_reason,
                    result.raw_response or "N/A",
                    result.tx_frame or "N/A",
                )
        except Exception as e:
            dlog.error(
                "Write select EXCEPTION | %s: option='%s', value=%d, address=%d (0x%04X), error=%s",
                self._entity_key,
                option,
                value,
                address,
                address,
                e,
            )

    async def _auto_rewrite_power_on_direction_change(self, new_direction: str) -> None:
        """Auto-rewrite power register when direction changes.

        When user switches charge/discharge direction, the power register
        must be rewritten with the new sign. Otherwise the device continues
        with the old direction until the user manually re-submits the power value.

        Args:
            new_direction: "charge" or "discharge"
        """
        try:
            # Find the power entity that references this direction selector
            config_cache = self.coordinator._full_config_cache
            if not config_cache:
                _LOGGER.debug("No config cache, skip auto-rewrite")
                return

            control_items = config_cache.get("control_items", {})
            power_entity_key = None
            power_config = None

            for entity_key, config in control_items.items():
                if config.get("direction_entity") == self._entity_key:
                    power_entity_key = entity_key
                    power_config = config
                    break

            if not power_entity_key or not power_config:
                _LOGGER.debug(
                    "No power entity linked to direction selector %s", self._entity_key
                )
                return

            # Get current power value from user_selections
            current_power = self.coordinator.get_user_selection(power_entity_key)
            if current_power is None or current_power == 0:
                _LOGGER.debug(
                    "No power value set for %s (value=%s), skip auto-rewrite",
                    power_entity_key,
                    current_power,
                )
                return

            # Calculate write value with new direction sign
            power_value = abs(float(current_power))
            gain = power_config.get("gain", 1)
            write_value = power_value * gain

            if new_direction == "charge":
                write_value = -abs(write_value)
            else:
                write_value = abs(write_value)

            address = int(power_config.get("address"))
            data_type = power_config.get("data_type", "INT32")

            _LOGGER.info(
                "🔄 Direction changed to '%s', auto-rewriting power register: "
                "address=%d (0x%04X), power=%s, write_value=%s",
                new_direction,
                address,
                address,
                current_power,
                int(write_value),
            )

            result = await self.coordinator.modbus_manager.write_register(
                address,
                int(write_value),
                data_type,
            )

            if result.success:
                # Update write protection to prevent UI flash-back
                protection_duration = power_config.get(
                    "write_protection_duration", 10.0
                )
                self.coordinator.set_write_protection(
                    power_entity_key, current_power, protection_duration
                )

                # Log to HA logbook
                device_name = self.coordinator.device_name or "Anker Solix"
                direction_label = "Charge" if new_direction == "charge" else "Discharge"
                unit = power_config.get("unit", "W")
                log_message = f"Direction changed → auto-rewrite: [{direction_label}] {int(power_value)} {unit}"

                await self.coordinator.hass.services.async_call(
                    "logbook",
                    "log",
                    {
                        "name": device_name,
                        "message": log_message,
                        "entity_id": self.entity_id,
                        "domain": DOMAIN,
                    },
                    blocking=False,
                )

                _LOGGER.info(
                    "📝 %s Direction changed → auto-rewrite: [%s] %s %s",
                    device_name,
                    direction_label,
                    int(power_value),
                    unit,
                )
            else:
                _LOGGER.warning(
                    "Auto-rewrite power register FAILED after direction change: "
                    "address=%d, write_value=%s, reason=%s",
                    address,
                    int(write_value),
                    result.error_reason,
                )

        except Exception as e:
            _LOGGER.error(
                "Error during auto-rewrite power on direction change: %s",
                e,
                exc_info=True,
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
