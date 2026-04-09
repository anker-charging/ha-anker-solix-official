"""Number input platform."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.exceptions import ServiceValidationError
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.const import ATTR_ENTITY_ID

from .const import DOMAIN
from .coordinator import AnkerSolixOfficialCoordinator
from .base_entity import AnkerSolixBaseEntity, async_setup_entities_with_retry

# Signal for mutual exclusion updates
SIGNAL_MUTUAL_EXCLUSION_UPDATE = f"{DOMAIN}_mutual_exclusion_update"

_LOGGER = logging.getLogger(__name__)

# Event type for logbook
EVENT_ANKER_SOLIX_CONTROL = "anker_solix_control"


def _is_number_entity(key: str, config: dict) -> bool:
    """Check if config represents a number input entity."""
    is_control = config.get("data_type_category") == "control"
    is_input = config.get("display_type") == "input"
    result = is_control and is_input
    if result:
        _LOGGER.info(
            "Number entity filter MATCH | key=%s, data_type_category=%s, display_type=%s",
            key,
            config.get("data_type_category"),
            config.get("display_type"),
        )
    return result


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up number input platform."""
    coordinator: AnkerSolixOfficialCoordinator = hass.data[DOMAIN][
        config_entry.entry_id
    ]

    await async_setup_entities_with_retry(
        hass=hass,
        coordinator=coordinator,
        async_add_entities=async_add_entities,
        entity_filter=_is_number_entity,
        entity_factory=lambda c, k, cfg: ModbusLocalDeviceNumber(c, k, cfg),
        platform_name="number",
    )


class ModbusLocalDeviceNumber(AnkerSolixBaseEntity, NumberEntity):
    """Modbus local device number input entity."""

    def __init__(
        self,
        coordinator: AnkerSolixOfficialCoordinator,
        key: str,
        config: dict[str, Any],
    ) -> None:
        """Initialize number input."""
        super().__init__(coordinator, key, config)

        # Set default icon if not configured
        if not self._attr_icon:
            self._attr_icon = "mdi:counter"

        # Set value range
        self._attr_native_min_value = config.get("min_value", 0)
        self._config_max_value = config.get("max_value", 100)
        self._attr_native_step = config.get("step", 1)

        # Dynamic max power entity keys (for direction-dependent upper limit)
        self._max_charge_power_entity = config.get("max_charge_power_entity")
        self._max_discharge_power_entity = config.get("max_discharge_power_entity")

        # Force BOX mode to prevent HA from auto-switching to slider
        self._attr_mode = NumberMode.BOX

        # Set unit
        unit = config.get("unit")
        if unit and unit != "/":
            self._attr_native_unit_of_measurement = unit

        # Track last known value to prevent unnecessary UI updates
        # This prevents user input from being overwritten during editing
        self._last_known_value: float | int | None = None

        # Track last set time for logbook deduplication prevention
        self._last_set_time: float | None = None

        # Read-once mode: only read device value once on first load
        # After that, always show user's last set value
        self._read_once = config.get("read_once", False)
        self._has_initial_read = False
        self._initial_value: float | int | None = None

        _LOGGER.info(
            "Number entity initialized | key=%s, address=%s, min=%s, max=%s, step=%s, unit=%s, dynamic_max=%s",
            key,
            config.get("address"),
            self._attr_native_min_value,
            self._config_max_value,
            self._attr_native_step,
            self._attr_native_unit_of_measurement,
            bool(self._max_charge_power_entity or self._max_discharge_power_entity),
        )

    async def async_added_to_hass(self) -> None:
        """Run when entity is added to hass."""
        await super().async_added_to_hass()

        # Subscribe to mutual exclusion update signal
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_MUTUAL_EXCLUSION_UPDATE,
                self._handle_mutual_exclusion_update,
            )
        )

    @callback
    def _handle_mutual_exclusion_update(self, entity_key: str) -> None:
        """Handle mutual exclusion update signal.

        Only update if this entity is the target of the mutual exclusion.
        """
        if entity_key == self._entity_key:
            _LOGGER.debug(
                "Mutual exclusion update received for %s, updating UI", self._entity_key
            )
            self.async_write_ha_state()

    @property
    def native_max_value(self) -> float:
        """Return dynamic max value based on current charge/discharge direction."""
        if not self._max_charge_power_entity and not self._max_discharge_power_entity:
            return self._config_max_value

        direction_entity = self._config.get("direction_entity")
        if not direction_entity:
            return self._config_max_value

        direction = self.coordinator.get_user_selection(direction_entity)
        data = self.coordinator.data

        if direction == "charge" and self._max_charge_power_entity and data:
            raw = data.get(self._max_charge_power_entity)
            if raw is not None:
                try:
                    val = abs(int(raw))
                    if val > 0:
                        return max(val, self._attr_native_min_value)
                except (ValueError, TypeError):
                    pass
        elif direction == "discharge" and self._max_discharge_power_entity and data:
            raw = data.get(self._max_discharge_power_entity)
            if raw is not None:
                try:
                    val = abs(int(raw))
                    if val > 0:
                        return max(val, self._attr_native_min_value)
                except (ValueError, TypeError):
                    pass

        return self._config_max_value

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        # First check base availability (coordinator connected)
        if not self.coordinator.is_connected():
            return False

        # Check visibility condition
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
    def native_value(self) -> float | int | None:
        """Return current value.

        Note: gain is already applied in modbus_client.py, no need to apply again here.
        Supports read_mode for mutual exclusion:
        - positive_only: Only show positive values, negative shows 0
        - negative_only: Only show absolute value of negative, positive shows 0

        Supports direction_entity for combined power setpoint:
        - Always shows absolute value (direction is shown by separate select entity)

        Supports never_read_device mode:
        - NEVER read from device, only show user's last input
        - Used for pure control entities like power setpoint
        """
        if not self.available:
            return None

        # Never-read mode: NEVER read from device, only show user's input
        never_read = self._config.get("never_read_device", False)
        if never_read:
            user_value = self.coordinator.get_user_selection(self._entity_key)
            _LOGGER.debug(
                "[never_read_device] %s: user_selection=%s, config=%s",
                self._entity_key,
                user_value,
                never_read,
            )
            if user_value is not None:
                return user_value
            # User hasn't set a value yet, return default
            default_value = self._config.get("default_value", 0)
            gain = self._config.get("gain", 1)
            _LOGGER.debug(
                "[never_read_device] %s: no user selection, returning default=%s",
                self._entity_key,
                default_value,
            )
            if gain == 1:
                return int(default_value)
            return float(default_value)

        # Read-once mode: after initial read, always return last known value
        if self._read_once:
            # If user has set a value, always return that
            if self._last_known_value is not None:
                return self._last_known_value
            # If we've done initial read, return that value
            if self._has_initial_read and self._initial_value is not None:
                return self._initial_value
            # First read: get value from device and remember it
            if not self._has_initial_read:
                raw_value = self._get_raw_value()
                if raw_value is not None:
                    try:
                        numeric_value = abs(float(raw_value))
                        gain = self._config.get("gain", 1)
                        if gain == 1:
                            self._initial_value = int(numeric_value)
                        else:
                            self._initial_value = numeric_value
                        self._has_initial_read = True
                        _LOGGER.debug(
                            "Read-once initial value for %s: %s",
                            self._entity_key,
                            self._initial_value,
                        )
                        return self._initial_value
                    except (ValueError, TypeError):
                        pass
                return None

        # Check if write protection is active (returns user display value directly)
        is_protected, protected_value = self.coordinator.get_protected_value(
            self._entity_key
        )
        if is_protected and protected_value is not None:
            # Write protection value is already in display format, skip read_mode processing
            try:
                gain = self._config.get("gain", 1)
                if gain == 1:
                    return int(protected_value)
                return float(protected_value)
            except (ValueError, TypeError):
                return None

        value = self._get_raw_value()
        if value is None:
            return None

        try:
            numeric_value = float(value)

            # Handle direction_entity mode: always show absolute value
            direction_entity = self._config.get("direction_entity")
            if direction_entity:
                numeric_value = abs(numeric_value)
            else:
                # Handle read_mode for mutual exclusion (only for non-protected values)
                read_mode = self._config.get("read_mode")
                if read_mode == "positive_only":
                    # Charge: show positive values only, negative shows 0
                    if numeric_value < 0:
                        numeric_value = 0
                elif read_mode == "negative_only":
                    # Discharge: show absolute value of negative, positive shows 0
                    if numeric_value > 0:
                        numeric_value = 0
                    else:
                        numeric_value = abs(numeric_value)

            # Return integer if gain is 1 (no decimal needed)
            gain = self._config.get("gain", 1)
            if gain == 1:
                return int(numeric_value)
            return numeric_value
        except (ValueError, TypeError):
            return None

    async def async_set_native_value(self, value: float) -> None:
        """Set value."""
        address = self._config.get("address")
        if address is None:
            _LOGGER.error("Number %s has no address configured", self._entity_key)
            return

        try:
            address = int(address)
        except (ValueError, TypeError):
            _LOGGER.error(
                "Invalid address type for number %s: %s", self._entity_key, address
            )
            return

        data_type = self._config.get("data_type", "UINT16")

        direction_entity = self._config.get("direction_entity")
        if direction_entity and (
            self._max_charge_power_entity or self._max_discharge_power_entity
        ):
            direction = self.coordinator.get_user_selection(direction_entity)
            data = self.coordinator.data
            _LOGGER.warning(
                "Power capacity check | entity=%s, value=%s, direction=%s, "
                "has_data=%s, max_charge_entity=%s, max_discharge_entity=%s",
                self._entity_key,
                value,
                direction,
                data is not None and bool(data),
                self._max_charge_power_entity,
                self._max_discharge_power_entity,
            )
            if direction and data:
                if direction == "charge" and self._max_charge_power_entity:
                    raw = data.get(self._max_charge_power_entity)
                    device_max = abs(int(raw)) if raw is not None else None
                    _LOGGER.warning(
                        "Charge capacity check | raw=%s, device_max=%s, min_value=%s, will_reject=%s",
                        raw,
                        device_max,
                        self._attr_native_min_value,
                        device_max is not None
                        and device_max < self._attr_native_min_value,
                    )
                    if (
                        device_max is not None
                        and device_max < self._attr_native_min_value
                    ):
                        raise ServiceValidationError(
                            translation_domain=DOMAIN,
                            translation_key="charge_power_too_low",
                            translation_placeholders={"power": str(device_max)},
                        )
                elif direction == "discharge" and self._max_discharge_power_entity:
                    raw = data.get(self._max_discharge_power_entity)
                    device_max = abs(int(raw)) if raw is not None else None
                    _LOGGER.warning(
                        "Discharge capacity check | raw=%s, device_max=%s, min_value=%s, will_reject=%s",
                        raw,
                        device_max,
                        self._attr_native_min_value,
                        device_max is not None
                        and device_max < self._attr_native_min_value,
                    )
                    if (
                        device_max is not None
                        and device_max < self._attr_native_min_value
                    ):
                        raise ServiceValidationError(
                            translation_domain=DOMAIN,
                            translation_key="discharge_power_too_low",
                            translation_placeholders={"power": str(device_max)},
                        )
        write_value = value
        gain = self._config.get("gain", 1)
        if gain != 1:
            write_value = value * gain

        direction = None
        if direction_entity:
            # STRICT MODE: ONLY use user's explicit selection, NEVER fallback to device
            direction = self.coordinator.get_user_selection(direction_entity)

            if direction is None:
                # User has NOT selected direction - REJECT the operation
                _LOGGER.error(
                    "Battery charge/discharge direction not set! Please select direction first."
                )
                _LOGGER.error(
                    "Battery direction not set! Please select charge/discharge direction first."
                )

                # Log to HA logbook (visible on the frontend)
                device_name = self.coordinator.device_name or "Anker Solix"
                entity_name = self.name or self._entity_key
                await self.hass.services.async_call(
                    "logbook",
                    "log",
                    {
                        "name": device_name,
                        "message": f"Failed to set {entity_name}: please select charge/discharge direction first",
                        "entity_id": self.entity_id,
                        "domain": DOMAIN,
                    },
                    blocking=False,
                )

                # Abort write operation
                return

            # Apply sign based on direction
            if direction == "charge":
                write_value = -abs(write_value)
                _LOGGER.info(
                    "🔋 Direction: charge (user selected), applying NEGATIVE sign: %s -> %s",
                    value,
                    write_value,
                )
            else:
                write_value = abs(write_value)
                _LOGGER.info(
                    "🔋 Direction: discharge (user selected), keeping POSITIVE: %s -> %s",
                    value,
                    write_value,
                )
        else:
            # Apply write_multiplier (e.g., -1 for discharge to convert positive input to negative)
            write_multiplier = self._config.get("write_multiplier", 1)
            if write_multiplier != 1:
                write_value = write_value * write_multiplier
                _LOGGER.debug(
                    "Applied write_multiplier=%s to %s: %s -> %s",
                    write_multiplier,
                    self._entity_key,
                    value,
                    write_value,
                )

        dlog = self.coordinator.device_logger

        dlog.warning(
            "Writing number %s | address=%d (0x%04X), user_value=%s, raw_value=%s, data_type=%s, gain=%s",
            self._entity_key,
            address,
            address,
            value,
            write_value,
            data_type,
            gain,
        )

        try:
            result = await self.coordinator.modbus_manager.write_register(
                address,
                int(write_value),
                data_type,
            )

            if result.success:
                # Store user's input value
                gain = self._config.get("gain", 1)
                user_value = int(value) if gain == 1 else value

                # Check if this is a never-read entity
                never_read = self._config.get("never_read_device", False)
                if never_read:
                    # For never-read entities: store in user_selections (permanent)
                    self.coordinator.set_user_selection(self._entity_key, user_value)
                    _LOGGER.info(
                        "[never_read_device] %s: stored user_selection=%s (will persist until HA restart)",
                        self._entity_key,
                        user_value,
                    )
                else:
                    # For normal entities: use both methods
                    self._last_known_value = user_value
                    # Enable write protection to prevent UI from overwriting user input
                    # Device may take several seconds to process the command
                    self.coordinator.set_write_protection(self._entity_key, value, 10.0)

                # Handle mutual exclusion: set linked entity to 0
                linked_entity = self._config.get("linked_entity")
                if linked_entity:
                    self.coordinator.set_write_protection(linked_entity, 0, 10.0)
                    _LOGGER.debug(
                        "Mutual exclusion: set %s to 0 (linked from %s)",
                        linked_entity,
                        self._entity_key,
                    )
                    # Send signal to update only the linked entity (not all entities)
                    async_dispatcher_send(
                        self.coordinator.hass,
                        SIGNAL_MUTUAL_EXCLUSION_UPDATE,
                        linked_entity,
                    )

                # Log to HA logbook
                unit = self._attr_native_unit_of_measurement or ""
                device_name = self.coordinator.device_name or "Anker Solix"
                # Use self.name for translated entity name
                entity_name = self.name or self._entity_key

                # Build log message with direction if available
                if direction:
                    direction_label = "Charge" if direction == "charge" else "Discharge"
                    log_message = (
                        f"{entity_name} → [{direction_label}] {int(value)} {unit}"
                    )
                    warning_log = f"📝 {device_name} {entity_name} → [{direction_label}] {int(value)} {unit}"
                else:
                    log_message = f"{entity_name} → {int(value)} {unit}"
                    warning_log = (
                        f"📝 {device_name} {entity_name} → {int(value)} {unit}"
                    )

                # Use logbook.log service (confirmed working)
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

                self.async_write_ha_state()

                dlog.warning(warning_log)
            else:
                dlog.error(
                    "Write number FAILED | entity=%s, user_value=%s, raw_value=%s, address=%d (0x%04X), "
                    "reason=%s, raw_response=%s, tx_frame=%s",
                    self._entity_key,
                    value,
                    write_value,
                    address,
                    address,
                    result.error_reason,
                    result.raw_response or "N/A",
                    result.tx_frame or "N/A",
                )
                # Only refresh on failure to restore correct state
                await self.coordinator.async_request_refresh()

        except Exception as e:
            dlog.error(
                "Write number EXCEPTION | %s: user_value=%s, raw_value=%s, address=%d (0x%04X), error=%s",
                self._entity_key,
                value,
                write_value,
                address,
                address,
                e,
            )
            try:
                await self.coordinator.async_request_refresh()
            except Exception:
                pass

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator.

        Control-type Number entities do NOT auto-refresh to prevent
        overwriting user input. Value only updates on:
        1. Initial load (first time)
        2. User manually sets a value (via async_set_native_value)
        3. Mutual exclusion update (via dispatcher signal)

        Note: We always call async_write_ha_state() to update availability
        status even when value is frozen (e.g., visibility_entity changed).
        """
        # For read_once mode, never auto-refresh value after initial read
        if self._read_once:
            if not self._has_initial_read:
                # First time: let native_value do the initial read
                current_value = self.native_value
                if current_value is not None:
                    self.async_write_ha_state()
            else:
                # After initial read, still update state for availability check
                # (e.g., when operating_mode changes, available should be re-evaluated)
                self.async_write_ha_state()
            return

        # For normal mode: only update on first load
        if self._last_known_value is None:
            self._last_known_value = self.native_value
        # Always update state for availability check
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes."""
        attrs = {
            "modbus_address": self._config.get("address"),
            "data_type": self._config.get("data_type"),
            "register_count": self._config.get("count"),
            "last_set_time": self._last_set_time,
        }

        # Add gain information
        gain = self._config.get("gain", 1)
        if gain != 1:
            attrs["gain"] = gain

        # Add dynamic power limits for direction-dependent entities
        if self._max_charge_power_entity or self._max_discharge_power_entity:
            attrs["min_power"] = int(self._attr_native_min_value)
            attrs["max_power"] = int(self.native_max_value)

            data = self.coordinator.data if self.coordinator.data else {}
            if self._max_charge_power_entity:
                raw = data.get(self._max_charge_power_entity)
                attrs["max_charge_power"] = abs(int(raw)) if raw is not None else "N/A"
            if self._max_discharge_power_entity:
                raw = data.get(self._max_discharge_power_entity)
                attrs["max_discharge_power"] = (
                    abs(int(raw)) if raw is not None else "N/A"
                )

            direction_entity = self._config.get("direction_entity")
            if direction_entity:
                direction = self.coordinator.get_user_selection(direction_entity)
                attrs["current_direction"] = direction or "not set"

        return attrs
