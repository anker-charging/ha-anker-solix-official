"""Sensor platform."""

from __future__ import annotations

import logging
import math
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorStateClass,
    SensorDeviceClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.const import (
    PERCENTAGE,
    UnitOfPower,
    UnitOfEnergy,
    UnitOfTemperature,
    UnitOfElectricPotential,
    UnitOfElectricCurrent,
)

from .const import DOMAIN
from .coordinator import AnkerSolixOfficialCoordinator
from .base_entity import AnkerSolixBaseEntity, async_setup_entities_with_retry

_LOGGER = logging.getLogger(__name__)


def _is_sensor_entity(key: str, config: dict) -> bool:
    """Check if config represents a sensor entity.

    Excludes internal entities (internal: true) which are used for
    capability checks but not exposed to the user.
    """
    # Skip internal entities
    if config.get("internal"):
        return False
    category = config.get("data_type_category")
    return category == "read" or category is None


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensor platform."""
    coordinator: AnkerSolixOfficialCoordinator = hass.data[DOMAIN][
        config_entry.entry_id
    ]

    await async_setup_entities_with_retry(
        hass=hass,
        coordinator=coordinator,
        async_add_entities=async_add_entities,
        entity_filter=_is_sensor_entity,
        entity_factory=lambda c, k, cfg: ModbusLocalDeviceSensor(c, k, cfg),
        platform_name="sensor",
    )


class ModbusLocalDeviceSensor(AnkerSolixBaseEntity, SensorEntity):
    """Modbus local device sensor entity."""

    def __init__(
        self,
        coordinator: AnkerSolixOfficialCoordinator,
        key: str,
        config: dict[str, Any],
    ) -> None:
        """Initialize sensor."""
        super().__init__(coordinator, key, config)

        has_value_mapping = bool(config.get("value_mapping"))

        if has_value_mapping:
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = list(config["value_mapping"].values())
        else:
            self._setup_numeric_sensor(config)

    @property
    def available(self) -> bool:
        """Return if entity is available, respecting version_gate and visibility_entity gates."""
        if not super().available:
            return False

        version_gate = self._config.get("version_gate")
        if version_gate:
            visible_key = f"{self._entity_key}_visible"
            if self.coordinator.data:
                visible = self.coordinator.data.get(visible_key)
                if visible is not None:
                    try:
                        return int(visible) == 1
                    except (ValueError, TypeError):
                        return False
            return False

        # Check visibility_entity (legacy mechanism)
        visibility_entity = self._config.get("visibility_entity")
        if not visibility_entity:
            return True

        if not self.coordinator.data:
            return False

        visibility_value = self._config.get("visibility_value")
        current_value = self.coordinator.data.get(visibility_entity)
        if current_value is None:
            return False
        try:
            return int(current_value) == int(visibility_value)
        except (ValueError, TypeError):
            return False

    def _setup_numeric_sensor(self, config: dict[str, Any]) -> None:
        """Set up attributes for numeric sensor."""
        data_type = config.get("data_type", "")
        unit = config.get("unit", "")

        # Skip unit/device_class setup for non-numeric types (STRING, VERSION)
        if data_type in ("STRING", "VERSION"):
            return

        # Skip unit/device_class for power_direction_format (returns formatted string)
        if config.get("power_direction_format"):
            return

        # Set unit (skip "/" which means no unit)
        if unit and unit != "/":
            self._attr_native_unit_of_measurement = unit

        # Set device class based on unit
        if unit == PERCENTAGE:
            self._attr_device_class = SensorDeviceClass.BATTERY
        elif unit in [UnitOfPower.WATT, UnitOfPower.KILO_WATT, "W", "kW"]:
            self._attr_device_class = SensorDeviceClass.POWER
        elif unit in [UnitOfEnergy.KILO_WATT_HOUR, UnitOfEnergy.WATT_HOUR, "kWh", "Wh"]:
            self._attr_device_class = SensorDeviceClass.ENERGY
        elif unit in [UnitOfTemperature.CELSIUS, "°C"]:
            self._attr_device_class = SensorDeviceClass.TEMPERATURE
        elif unit in [UnitOfElectricPotential.VOLT, "V"]:
            self._attr_device_class = SensorDeviceClass.VOLTAGE
        elif unit in [UnitOfElectricCurrent.AMPERE, "A"]:
            self._attr_device_class = SensorDeviceClass.CURRENT

        # Set state class
        if unit in [UnitOfEnergy.KILO_WATT_HOUR, UnitOfEnergy.WATT_HOUR, "kWh", "Wh"]:
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        elif unit and unit != "/":
            self._attr_state_class = SensorStateClass.MEASUREMENT

        # Derive display precision from gain so UI decimals match register resolution
        gain = config.get("gain")
        if isinstance(gain, (int, float)) and gain > 0:
            precision = round(math.log10(gain))
            if 10**precision == gain:
                self._attr_suggested_display_precision = precision

    def _get_aggregated_value(self, default: Any = None) -> Any:
        """Get aggregated value from primary and additional sources.

        If additional_sources is configured, sums values from all sources.
        Otherwise, returns the primary value.

        Args:
            default: Default value if not found

        Returns:
            Aggregated numeric value or primary value
        """
        primary_value = self._get_raw_value(default)

        # Check for additional sources configuration
        additional_sources = self._config.get("additional_sources", [])
        if not additional_sources:
            return primary_value

        # Only aggregate numeric values
        if not isinstance(primary_value, (int, float)):
            return primary_value

        total = float(primary_value)

        # Sum values from additional sources
        for source_key in additional_sources:
            if self.coordinator.data:
                source_value = self.coordinator.data.get(source_key)
                if isinstance(source_value, (int, float)):
                    total += float(source_value)
                    _LOGGER.debug(
                        "Aggregating %s: adding %s=%s, running total=%s",
                        self._entity_key,
                        source_key,
                        source_value,
                        total,
                    )

        # Return as int if result is whole number
        if total == int(total):
            return int(total)
        return total

    @property
    def native_value(self) -> StateType:
        """Return current value of sensor."""
        if not self.available:
            return None

        # None as "not read this cycle" sentinel, not a fabricated 0/"": a
        # decode failure omits the key from coordinator.data entirely (see
        # _decode_register_value in modbus_client.py), so a missing key here
        # means "no fresh data", never a real device-reported 0. Returning a
        # substituted 0 for a total_increasing energy sensor reproduces the
        # issue #55 false-spike bug at single-register granularity: Recorder
        # would treat that fabricated 0 as a meter reset and produce a false
        # spike once the real value returns. Returning None instead surfaces
        # as state "unknown" (entity stays available), which Recorder's
        # statistics compiler skips entirely rather than folding into sum/
        # reset detection.
        if self._config.get("additional_sources"):
            value = self._get_aggregated_value()
        else:
            value = self._get_raw_value()

        if value is None:
            return None

        # Handle value mapping - return translation key directly for ENUM sensors
        value_mapping = self._config.get("value_mapping")
        if value_mapping and isinstance(value, (int, float)):
            try:
                translation_key = value_mapping.get(int(value))
                if translation_key is not None:
                    return translation_key
            except (ValueError, TypeError):
                pass

        # Handle power split mode - extract positive or negative values only
        # positive_only: return value if > 0, else 0 (e.g., discharging power, grid import)
        # negative_only: return abs(value) if < 0, else 0 (e.g., charging power, grid export)
        power_split_mode = self._config.get("power_split_mode")
        if power_split_mode and isinstance(value, (int, float)):
            try:
                numeric_value = float(value)
                if power_split_mode == "positive_only":
                    # Return positive values as-is, negative values become 0
                    result = numeric_value if numeric_value > 0 else 0
                elif power_split_mode == "negative_only":
                    # Return absolute value of negative values, positive values become 0
                    result = abs(numeric_value) if numeric_value < 0 else 0
                else:
                    result = numeric_value
                
                # Return as int if whole number
                if result == int(result):
                    return int(result)
                return result
            except (ValueError, TypeError):
                pass

        # Handle power direction format (e.g., "Charge 300" instead of "-300")
        power_direction_format = self._config.get("power_direction_format")
        if power_direction_format and isinstance(value, (int, float)):
            try:
                numeric_value = float(value)
                abs_value = abs(int(numeric_value))
                unit = self._config.get("unit", "")
                unit_str = f" {unit}" if unit and unit != "/" else ""

                # When value is 0, show without direction prefix
                if abs_value == 0:
                    return f"0{unit_str}"

                if numeric_value > 0:
                    direction = power_direction_format.get("positive", "")
                else:
                    direction = power_direction_format.get("negative", "")

                return f"{direction} {abs_value}{unit_str}"
            except (ValueError, TypeError):
                pass

        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes."""
        attrs = {
            "modbus_address": self._config.get("address"),
            "data_type": self._config.get("data_type"),
            "register_count": self._config.get("count"),
        }

        # For power_direction_format sensors, add raw numeric value for automation
        power_direction_format = self._config.get("power_direction_format")
        if power_direction_format:
            raw_value = self._get_raw_value()
            if isinstance(raw_value, (int, float)):
                attrs["raw_value"] = raw_value
                attrs["unit"] = self._config.get("unit", "")

        # For aggregated sensors, show component values
        additional_sources = self._config.get("additional_sources", [])
        if additional_sources:
            primary_value = self._get_raw_value()
            attrs["primary_value"] = primary_value
            attrs["additional_sources"] = additional_sources
            # Show individual source values
            for source_key in additional_sources:
                if self.coordinator.data:
                    source_value = self.coordinator.data.get(source_key)
                    if source_value is not None:
                        attrs[f"source_{source_key}"] = source_value

        return attrs
