"""Anker Solix switch platform."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import AnkerSolixOfficialCoordinator
from .base_entity import AnkerSolixBaseEntity, async_setup_entities_with_retry

_LOGGER = logging.getLogger(__name__)

# Constants
_OPTION_ENABLED = "enabled"
_OPTION_DISABLED = "disabled"
_DEFAULT_ON_VALUE = 1
_DEFAULT_OFF_VALUE = 0


def _is_switch_entity(key: str, config: dict) -> bool:
    """Check if config represents a switch entity."""
    return (
        config.get("data_type_category") == "control"
        and config.get("control_type") == "switch"
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Anker Solix switch entities."""
    coordinator: AnkerSolixOfficialCoordinator = hass.data[DOMAIN][entry.entry_id]

    await async_setup_entities_with_retry(
        hass=hass,
        coordinator=coordinator,
        async_add_entities=async_add_entities,
        entity_filter=_is_switch_entity,
        entity_factory=lambda c, k, cfg: AnkerSolixSwitch(c, k, cfg),
        platform_name="switch",
    )


class AnkerSolixSwitch(AnkerSolixBaseEntity, SwitchEntity):
    """Anker Solix Switch Entity."""

    def __init__(
        self,
        coordinator: AnkerSolixOfficialCoordinator,
        entity_key: str,
        entity_config: dict[str, Any],
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, entity_key, entity_config)

        # Set default icon if not configured
        if not self._attr_icon:
            self._attr_icon = "mdi:toggle-switch"

        # Support separate read/write for switches with read-only status registers
        # read_entity_key: entity key to read state from (optional)
        # address: for writing control commands
        self._read_entity_key = self._config.get("read_entity_key")
        self._write_address = self._config.get("address")

    def _get_option_value(self, option_key: str, default_value: int) -> int:
        """Get register value for option.

        Args:
            option_key: Option key name (e.g., 'enabled' or 'disabled')
            default_value: Default return value

        Returns:
            Corresponding register integer value
        """
        options = self._config.get("options", {})
        for value, key in options.items():
            if key == option_key:
                try:
                    return int(value)
                except (ValueError, TypeError):
                    _LOGGER.warning(
                        "Invalid value for option %s in %s, using default",
                        option_key,
                        self._entity_key,
                    )
                    return default_value
        return default_value

    def _get_state_value(self) -> Any:
        """Get the current state value from coordinator data.

        If read_entity_key is configured, read from that entity's data.
        Otherwise fall back to the standard entity key.

        Also checks write protection for both the switch entity and the read entity.
        """
        # Check write protection for this switch entity first
        is_protected, protected_value = self.coordinator.get_protected_value(
            self._entity_key
        )
        if is_protected:
            _LOGGER.debug(
                "Switch %s using protected value: %s", self._entity_key, protected_value
            )
            return protected_value

        # If using separate read entity, also check its protection
        if self._read_entity_key is not None:
            is_read_protected, read_protected_value = (
                self.coordinator.get_protected_value(self._read_entity_key)
            )
            if is_read_protected:
                _LOGGER.debug(
                    "Switch %s using protected value from %s: %s",
                    self._entity_key,
                    self._read_entity_key,
                    read_protected_value,
                )
                return read_protected_value

            # Read from the separate entity's data
            data = self.coordinator.data
            if data:
                return data.get(self._read_entity_key)
            return None

        # Fall back to default behavior
        return self._get_raw_value()

    @property
    def is_on(self) -> bool | None:
        """Return True if switch is on."""
        raw_value = self._get_state_value()
        if raw_value is None:
            return None

        on_value = self._get_option_value(_OPTION_ENABLED, _DEFAULT_ON_VALUE)

        try:
            return int(raw_value) == on_value
        except (ValueError, TypeError):
            _LOGGER.warning("Invalid raw value for %s: %s", self._entity_key, raw_value)
            return None

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Turn the switch on."""
        on_value = self._get_option_value(_OPTION_ENABLED, _DEFAULT_ON_VALUE)
        await self._async_set_state(on_value, "on")

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Turn the switch off."""
        off_value = self._get_option_value(_OPTION_DISABLED, _DEFAULT_OFF_VALUE)
        await self._async_set_state(off_value, "off")

    async def _async_set_state(self, value: int, state_name: str) -> None:
        """Set switch state.

        Args:
            value: Register value to write
            state_name: State name ('on' or 'off') for logging
        """
        address = self._write_address
        if address is None:
            _LOGGER.error("Switch %s has no address configured", self._entity_key)
            return

        try:
            address = int(address)
        except (ValueError, TypeError):
            _LOGGER.error(
                "Invalid address type for switch %s: %s", self._entity_key, address
            )
            return

        data_type = self._config.get("data_type", "UINT16")

        dlog = self.coordinator.device_logger

        dlog.warning(
            "Writing switch %s | address=%d (0x%04X), state='%s', value=%d, data_type=%s",
            self._entity_key,
            address,
            address,
            state_name,
            value,
            data_type,
        )

        try:
            result = await self.coordinator.modbus_manager.write_register(
                address, value, data_type
            )
            if result.success:
                self.coordinator.set_write_protection(self._entity_key, value, 10.0)

                if self._read_entity_key:
                    self.coordinator.set_write_protection(
                        self._read_entity_key, value, 10.0
                    )
                    _LOGGER.debug(
                        "Write protection set for both %s and %s, value=%d, duration=10s",
                        self._entity_key,
                        self._read_entity_key,
                        value,
                    )

                self.async_write_ha_state()

                device_name = self.coordinator.device_name or "Anker Solix"
                entity_name = self.name or self._entity_key
                state_display = "ON" if state_name == "on" else "OFF"
                log_message = f"{entity_name} → {state_display}"

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

                dlog.warning("📝 %s %s → %s", device_name, entity_name, state_display)
            else:
                dlog.error(
                    "Write switch FAILED | entity=%s, state='%s', value=%d, address=%d (0x%04X), "
                    "reason=%s, raw_response=%s, tx_frame=%s",
                    self._entity_key,
                    state_name,
                    value,
                    address,
                    address,
                    result.error_reason,
                    result.raw_response or "N/A",
                    result.tx_frame or "N/A",
                )
        except Exception as e:
            dlog.error(
                "Write switch EXCEPTION | %s: state='%s', value=%d, address=%d (0x%04X), error=%s",
                self._entity_key,
                state_name,
                value,
                address,
                address,
                e,
            )
