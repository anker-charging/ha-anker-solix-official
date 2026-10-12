"""Unit tests for ModbusLocalDeviceSensor attribute and value handling.

Regression coverage for issue #108: VERSION-typed data points (e.g. the
Smart Meter Gen 2 firmware version at register 10696) must never receive
numeric sensor attributes. If ``suggested_display_precision`` (or any other
numeric indicator) is set, Home Assistant treats the version string as a
number and entity creation fails with ``float('1.0.0.9')``.

The coordinator dependency is a lightweight fake exposing only what
sensor.py reads, mirroring the pattern used in test_base_entity.py.
"""

from __future__ import annotations

import logging
import math
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from custom_components.anker_solix_official.const import DOMAIN
from custom_components.anker_solix_official.sensor import (
    ModbusLocalDeviceSensor,
    _is_sensor_entity,
    async_setup_entry,
)


class _FakeCoordinator:
    """Stand-in exposing only what ModbusLocalDeviceSensor reads."""

    def __init__(self) -> None:
        self.entry = type("Entry", (), {"entry_id": "test-entry"})()
        self.device_info = {"model": "Smart Meter Gen 2"}
        self.data: dict[str, Any] = {}
        self.last_update_success = True

    def is_connected(self) -> bool:
        return self.last_update_success

    def is_register_available(self, address: int) -> bool:
        return True

    def get_protected_value(self, entity_key: str) -> tuple[bool, Any]:
        return False, None

    def async_add_listener(self, listener):
        return lambda: None


@pytest.fixture
def fake_coordinator() -> _FakeCoordinator:
    return _FakeCoordinator()


def _make_sensor(
    fake_coordinator: _FakeCoordinator,
    entity_key: str,
    config: dict[str, Any],
) -> ModbusLocalDeviceSensor:
    return ModbusLocalDeviceSensor(fake_coordinator, entity_key, config)


def _numeric_indicators(entity: ModbusLocalDeviceSensor) -> dict[str, Any]:
    """The four attributes HA uses to decide a sensor is numeric."""
    return {
        "device_class": entity.device_class,
        "state_class": entity.state_class,
        "unit": entity.native_unit_of_measurement,
        "suggested_precision": entity.suggested_display_precision,
    }


# Exact data point from config/42bcf12f...yaml (Smart Meter Gen 2).
METER_SW_VERSION_CONFIG = {
    "translation_key": "meter_sw_version",
    "address": 10696,
    "data_type": "VERSION",
    "unit": "/",
    "gain": 1,
    "count": 2,
    "icon": "mdi:information-outline",
}


class TestVersionSensorIsText:
    """Issue #108 regression: VERSION sensors must stay non-numeric."""

    def test_no_numeric_indicators(self, fake_coordinator) -> None:
        entity = _make_sensor(
            fake_coordinator, "meter_sw_version", METER_SW_VERSION_CONFIG
        )
        assert _numeric_indicators(entity) == {
            "device_class": None,
            "state_class": None,
            "unit": None,
            "suggested_precision": None,
        }

    def test_native_value_returns_version_string_unchanged(
        self, fake_coordinator
    ) -> None:
        fake_coordinator.data = {"meter_sw_version": "1.0.0.9"}
        entity = _make_sensor(
            fake_coordinator, "meter_sw_version", METER_SW_VERSION_CONFIG
        )
        assert entity.native_value == "1.0.0.9"

    def test_version_value_is_never_float_coerced(self, fake_coordinator) -> None:
        """HA would call float() iff any numeric indicator is set; prove it cannot."""
        fake_coordinator.data = {"meter_sw_version": "1.0.0.9"}
        entity = _make_sensor(
            fake_coordinator, "meter_sw_version", METER_SW_VERSION_CONFIG
        )
        assert all(v is None for v in _numeric_indicators(entity).values())
        # The raw value itself is not float-parseable, which is exactly why
        # the numeric path must stay disabled for this data type.
        with pytest.raises(ValueError):
            float(entity.native_value)


class TestStringSensorIsText:
    """STRING sensors were already protected; keep them non-numeric."""

    def test_no_numeric_indicators(self, fake_coordinator) -> None:
        entity = _make_sensor(
            fake_coordinator,
            "device_sn",
            {"address": 10100, "data_type": "STRING", "unit": "/", "gain": 1},
        )
        assert all(v is None for v in _numeric_indicators(entity).values())

    def test_native_value_returns_string(self, fake_coordinator) -> None:
        fake_coordinator.data = {"device_sn": "ABC123"}
        entity = _make_sensor(
            fake_coordinator,
            "device_sn",
            {"address": 10100, "data_type": "STRING", "unit": "/", "gain": 1},
        )
        assert entity.native_value == "ABC123"


class TestNumericSensorsKeepPrecision:
    """The gain-derived precision must still apply to real numeric sensors."""

    @pytest.mark.parametrize(
        ("gain", "expected_precision"),
        [(1, 0), (10, 1), (100, 2)],
    )
    def test_power_of_ten_gain_sets_precision(
        self, fake_coordinator, gain, expected_precision
    ) -> None:
        entity = _make_sensor(
            fake_coordinator,
            "primary_phase_1_current",
            {"address": 10666, "data_type": "INT16", "unit": "A", "gain": gain},
        )
        assert entity.suggested_display_precision == expected_precision

    def test_non_power_of_ten_gain_sets_no_precision(self, fake_coordinator) -> None:
        entity = _make_sensor(
            fake_coordinator,
            "odd_gain",
            {"address": 10000, "data_type": "UINT16", "unit": "W", "gain": 50},
        )
        assert entity.suggested_display_precision is None

    def test_power_sensor_gets_device_and_state_class(self, fake_coordinator) -> None:
        entity = _make_sensor(
            fake_coordinator,
            "load_power",
            {"address": 10010, "data_type": "INT32", "unit": "W", "gain": 1},
        )
        assert entity.device_class == "power"
        assert entity.state_class == "measurement"
        assert entity.native_unit_of_measurement == "W"

    def test_energy_sensor_is_total_increasing(self, fake_coordinator) -> None:
        entity = _make_sensor(
            fake_coordinator,
            "pv_total_generation",
            {"address": 10018, "data_type": "UINT32", "unit": "kWh", "gain": 10},
        )
        assert entity.device_class == "energy"
        assert entity.state_class == "total_increasing"
        assert entity.suggested_display_precision == 1

    def test_percent_sensor_is_battery_class(self, fake_coordinator) -> None:
        entity = _make_sensor(
            fake_coordinator,
            "battery_soc",
            {"address": 10014, "data_type": "UINT16", "unit": "%", "gain": 1},
        )
        assert entity.device_class == "battery"

    def test_yaml_declared_classes_override_unit_inference(
        self, fake_coordinator
    ) -> None:
        entity = _make_sensor(
            fake_coordinator,
            "any_stored_energy_key",
            {
                "address": 10250,
                "data_type": "UINT32",
                "unit": "kWh",
                "gain": 10,
                "device_class": "energy_storage",
                "state_class": "measurement",
            },
        )
        assert entity.device_class == "energy_storage"
        assert entity.state_class == "measurement"
        assert entity.native_unit_of_measurement == "kWh"
        assert entity.suggested_display_precision == 1

    def test_kwh_without_yaml_classes_stays_total_increasing(
        self, fake_coordinator
    ) -> None:
        entity = _make_sensor(
            fake_coordinator,
            "any_energy_meter_key",
            {"address": 10018, "data_type": "UINT32", "unit": "kWh", "gain": 10},
        )
        assert entity.device_class == "energy"
        assert entity.state_class == "total_increasing"

    def test_yaml_state_class_alone_keeps_unit_inferred_device_class(
        self, fake_coordinator
    ) -> None:
        entity = _make_sensor(
            fake_coordinator,
            "any_energy_key",
            {
                "address": 10018,
                "data_type": "UINT32",
                "unit": "kWh",
                "gain": 10,
                "state_class": "measurement",
            },
        )
        assert entity.device_class == "energy"
        assert entity.state_class == "measurement"

    def test_invalid_declared_device_class_is_ignored_with_warning(
        self, fake_coordinator, caplog
    ) -> None:
        with caplog.at_level(logging.WARNING):
            entity = _make_sensor(
                fake_coordinator,
                "bad_device_class_key",
                {
                    "address": 10018,
                    "data_type": "UINT32",
                    "unit": "kWh",
                    "gain": 10,
                    "device_class": "power-factor",
                },
            )
        assert entity.device_class == "energy"
        assert entity.state_class == "total_increasing"
        assert "Ignoring invalid SensorDeviceClass 'power-factor'" in caplog.text
        assert "bad_device_class_key" in caplog.text

    def test_invalid_declared_state_class_is_ignored_with_warning(
        self, fake_coordinator, caplog
    ) -> None:
        with caplog.at_level(logging.WARNING):
            entity = _make_sensor(
                fake_coordinator,
                "bad_state_class_key",
                {
                    "address": 10010,
                    "data_type": "INT32",
                    "unit": "W",
                    "gain": 1,
                    "state_class": "meassurement",
                },
            )
        assert entity.device_class == "power"
        assert entity.state_class == "measurement"
        assert "Ignoring invalid SensorStateClass 'meassurement'" in caplog.text
        assert "bad_state_class_key" in caplog.text

    def test_declared_class_is_case_insensitive(self, fake_coordinator) -> None:
        entity = _make_sensor(
            fake_coordinator,
            "mixed_case_key",
            {
                "address": 10250,
                "data_type": "UINT32",
                "unit": "kWh",
                "gain": 10,
                "device_class": "Energy_Storage",
                "state_class": "MEASUREMENT",
            },
        )
        assert entity.device_class == "energy_storage"
        assert entity.state_class == "measurement"


class TestValueMappingSensor:
    """ENUM sensors (value_mapping) are unaffected by the numeric setup."""

    def test_enum_sensor_exposes_options(self, fake_coordinator) -> None:
        entity = _make_sensor(
            fake_coordinator,
            "meter_type",
            {
                "address": 10630,
                "data_type": "UINT16",
                "unit": "/",
                "gain": 1,
                "value_mapping": {1: "single_phase", 2: "three_phase"},
            },
        )
        assert entity.device_class == "enum"
        assert entity.options == ["single_phase", "three_phase"]

    def test_enum_native_value_maps_to_translation_key(
        self, fake_coordinator
    ) -> None:
        fake_coordinator.data = {"meter_type": 2}
        entity = _make_sensor(
            fake_coordinator,
            "meter_type",
            {
                "address": 10630,
                "data_type": "UINT16",
                "unit": "/",
                "gain": 1,
                "value_mapping": {1: "single_phase", 2: "three_phase"},
            },
        )
        assert entity.native_value == "three_phase"


class TestMissingKeyIsUnknownNotZero:
    """Issue #55 regression, at single-register granularity.

    A decode failure omits the key from coordinator.data entirely (it is
    never written as a fabricated 0/""). native_value must surface that as
    None (HA state "unknown") rather than substituting 0/"" itself, or the
    same false-energy-spike bug reappears whenever only one register in an
    otherwise-successful refresh fails to decode.
    """

    def test_numeric_sensor_missing_key_returns_none(self, fake_coordinator) -> None:
        fake_coordinator.data = {"other_key": 42}  # this entity's key absent
        entity = _make_sensor(
            fake_coordinator,
            "energy_total",
            {
                "address": 10200,
                "data_type": "UINT32",
                "unit": "kWh",
                "gain": 1,
                "count": 2,
            },
        )
        assert entity.native_value is None

    def test_string_sensor_missing_key_returns_none(self, fake_coordinator) -> None:
        fake_coordinator.data = {"other_key": "x"}
        entity = _make_sensor(
            fake_coordinator,
            "device_sn",
            {"address": 10100, "data_type": "STRING", "unit": "/", "gain": 1},
        )
        assert entity.native_value is None

    def test_aggregated_sensor_missing_primary_key_returns_none(
        self, fake_coordinator
    ) -> None:
        fake_coordinator.data = {"secondary_power": 100}  # primary key absent
        entity = _make_sensor(
            fake_coordinator,
            "primary_power",
            {
                "address": 10300,
                "data_type": "INT32",
                "unit": "W",
                "gain": 1,
                "additional_sources": ["secondary_power"],
            },
        )
        assert entity.native_value is None


class TestIsSensorEntity:
    def test_internal_entity_excluded(self) -> None:
        assert not _is_sensor_entity("k", {"internal": True})

    def test_read_category_is_sensor(self) -> None:
        assert _is_sensor_entity("k", {"data_type_category": "read"})

    def test_missing_category_defaults_to_sensor(self) -> None:
        assert _is_sensor_entity("k", {})

    def test_control_category_excluded(self) -> None:
        assert not _is_sensor_entity("k", {"data_type_category": "control"})


class TestAsyncSetupEntry:
    async def test_creates_only_non_internal_sensors(
        self, fake_coordinator
    ) -> None:
        fake_coordinator.get_device_data_points = AsyncMock(
            return_value={
                "meter_sw_version": METER_SW_VERSION_CONFIG,
                "load_power": {
                    "address": 10010,
                    "data_type": "INT32",
                    "unit": "W",
                    "gain": 1,
                },
                "internal_mask": {"address": 10999, "internal": True},
                "mode_control": {
                    "address": 10100,
                    "data_type_category": "control",
                    "display_type": "select",
                },
            }
        )
        hass = SimpleNamespace(data={DOMAIN: {"e1": fake_coordinator}})
        entry = SimpleNamespace(entry_id="e1")
        added: list = []
        await async_setup_entry(hass, entry, added.extend)
        assert len(added) == 2
        assert all(isinstance(e, ModbusLocalDeviceSensor) for e in added)

    async def test_no_data_points_creates_nothing(self, fake_coordinator) -> None:
        fake_coordinator.get_device_data_points = AsyncMock(return_value={})
        fake_coordinator.ip_address = "192.168.1.50"
        hass = SimpleNamespace(data={DOMAIN: {"e1": fake_coordinator}})
        entry = SimpleNamespace(entry_id="e1")
        added: list = []
        await async_setup_entry(hass, entry, added.extend)
        assert added == []


class TestAvailableVersionGate:
    def _gated(self, fake_coordinator) -> ModbusLocalDeviceSensor:
        return _make_sensor(
            fake_coordinator,
            "feature_x",
            {
                "address": 10000,
                "data_type": "UINT16",
                "unit": "/",
                "gain": 1,
                "version_gate": True,
            },
        )

    def test_visible_flag_1_shows(self, fake_coordinator) -> None:
        fake_coordinator.data = {"feature_x_visible": 1}
        assert self._gated(fake_coordinator).available is True

    def test_visible_flag_0_hides(self, fake_coordinator) -> None:
        fake_coordinator.data = {"feature_x_visible": 0}
        assert self._gated(fake_coordinator).available is False

    def test_invalid_visible_flag_hides(self, fake_coordinator) -> None:
        fake_coordinator.data = {"feature_x_visible": "abc"}
        assert self._gated(fake_coordinator).available is False

    def test_missing_visible_flag_hides(self, fake_coordinator) -> None:
        fake_coordinator.data = {"other": 1}
        assert self._gated(fake_coordinator).available is False

    def test_empty_data_hides(self, fake_coordinator) -> None:
        fake_coordinator.data = {}
        assert self._gated(fake_coordinator).available is False


class TestAvailableVisibilityEntity:
    def _legacy_gated(self, fake_coordinator) -> ModbusLocalDeviceSensor:
        return _make_sensor(
            fake_coordinator,
            "feature_y",
            {
                "address": 10001,
                "data_type": "UINT16",
                "unit": "/",
                "gain": 1,
                "visibility_entity": "mode",
                "visibility_value": 3,
            },
        )

    def test_matching_value_shows(self, fake_coordinator) -> None:
        fake_coordinator.data = {"mode": 3}
        assert self._legacy_gated(fake_coordinator).available is True

    def test_mismatched_value_hides(self, fake_coordinator) -> None:
        fake_coordinator.data = {"mode": 1}
        assert self._legacy_gated(fake_coordinator).available is False

    def test_missing_value_hides(self, fake_coordinator) -> None:
        fake_coordinator.data = {"other": 3}
        assert self._legacy_gated(fake_coordinator).available is False

    def test_invalid_value_hides(self, fake_coordinator) -> None:
        fake_coordinator.data = {"mode": "x"}
        assert self._legacy_gated(fake_coordinator).available is False

    def test_empty_data_hides(self, fake_coordinator) -> None:
        fake_coordinator.data = {}
        assert self._legacy_gated(fake_coordinator).available is False

    def test_unavailable_coordinator_hides(self, fake_coordinator) -> None:
        fake_coordinator.last_update_success = False
        fake_coordinator.data = {"mode": 3}
        assert self._legacy_gated(fake_coordinator).available is False


class TestNumericSetupUnits:
    @pytest.mark.parametrize(
        ("unit", "expected_class"),
        [
            ("°C", "temperature"),
            ("V", "voltage"),
            ("A", "current"),
        ],
    )
    def test_unit_derives_device_class(
        self, fake_coordinator, unit, expected_class
    ) -> None:
        entity = _make_sensor(
            fake_coordinator,
            "probe",
            {"address": 10002, "data_type": "INT16", "unit": unit, "gain": 1},
        )
        assert entity.device_class == expected_class
        assert entity.state_class == "measurement"

    def test_power_direction_format_skips_numeric_setup(
        self, fake_coordinator
    ) -> None:
        entity = _make_sensor(
            fake_coordinator,
            "grid_power",
            {
                "address": 10003,
                "data_type": "INT32",
                "unit": "W",
                "gain": 1,
                "power_direction_format": {"positive": "Import", "negative": "Export"},
            },
        )
        assert entity.device_class is None
        assert entity.native_unit_of_measurement is None


class TestAggregation:
    def test_sums_additional_sources_as_int(self, fake_coordinator) -> None:
        fake_coordinator.data = {"primary": 100, "secondary": 50}
        entity = _make_sensor(
            fake_coordinator,
            "primary",
            {
                "address": 10300,
                "data_type": "INT32",
                "unit": "W",
                "gain": 1,
                "additional_sources": ["secondary"],
            },
        )
        assert entity.native_value == 150

    def test_float_sum_kept_when_fractional(self, fake_coordinator) -> None:
        fake_coordinator.data = {"primary": 1.5, "secondary": 2.25}
        entity = _make_sensor(
            fake_coordinator,
            "primary",
            {
                "address": 10300,
                "data_type": "INT32",
                "unit": "kW",
                "gain": 1,
                "additional_sources": ["secondary"],
            },
        )
        assert entity.native_value == 3.75

    def test_non_numeric_source_skipped(self, fake_coordinator) -> None:
        fake_coordinator.data = {"primary": 100, "secondary": "abc"}
        entity = _make_sensor(
            fake_coordinator,
            "primary",
            {
                "address": 10300,
                "data_type": "INT32",
                "unit": "W",
                "gain": 1,
                "additional_sources": ["secondary"],
            },
        )
        assert entity.native_value == 100

    def test_non_numeric_primary_returned_as_is(self, fake_coordinator) -> None:
        fake_coordinator.data = {"primary": "text"}
        entity = _make_sensor(
            fake_coordinator,
            "primary",
            {
                "address": 10300,
                "data_type": "STRING",
                "unit": "/",
                "gain": 1,
                "additional_sources": ["secondary"],
            },
        )
        assert entity.native_value == "text"


class TestPowerSplitMode:
    def _split(self, fake_coordinator, mode: str) -> ModbusLocalDeviceSensor:
        return _make_sensor(
            fake_coordinator,
            "grid_power",
            {
                "address": 10400,
                "data_type": "INT32",
                "unit": "W",
                "gain": 1,
                "power_split_mode": mode,
            },
        )

    def test_positive_only(self, fake_coordinator) -> None:
        fake_coordinator.data = {"grid_power": 300}
        assert self._split(fake_coordinator, "positive_only").native_value == 300
        fake_coordinator.data = {"grid_power": -300}
        assert self._split(fake_coordinator, "positive_only").native_value == 0

    def test_negative_only(self, fake_coordinator) -> None:
        fake_coordinator.data = {"grid_power": -300}
        assert self._split(fake_coordinator, "negative_only").native_value == 300
        fake_coordinator.data = {"grid_power": 300}
        assert self._split(fake_coordinator, "negative_only").native_value == 0

    def test_unknown_mode_passes_value_through(self, fake_coordinator) -> None:
        fake_coordinator.data = {"grid_power": 300}
        assert self._split(fake_coordinator, "weird").native_value == 300


class TestPowerDirectionFormat:
    def _formatted(self, fake_coordinator, unit="W") -> ModbusLocalDeviceSensor:
        return _make_sensor(
            fake_coordinator,
            "grid_power",
            {
                "address": 10500,
                "data_type": "INT32",
                "unit": unit,
                "gain": 1,
                "power_direction_format": {
                    "positive": "Import",
                    "negative": "Export",
                },
            },
        )

    def test_positive_value_formatted(self, fake_coordinator) -> None:
        fake_coordinator.data = {"grid_power": 300}
        assert self._formatted(fake_coordinator).native_value == "Import 300 W"

    def test_negative_value_formatted(self, fake_coordinator) -> None:
        fake_coordinator.data = {"grid_power": -300}
        assert self._formatted(fake_coordinator).native_value == "Export 300 W"

    def test_zero_without_direction(self, fake_coordinator) -> None:
        fake_coordinator.data = {"grid_power": 0}
        assert self._formatted(fake_coordinator).native_value == "0 W"

    def test_slash_unit_has_no_suffix(self, fake_coordinator) -> None:
        fake_coordinator.data = {"grid_power": 300}
        assert (
            self._formatted(fake_coordinator, unit="/").native_value == "Import 300"
        )

    def test_non_numeric_value_returned_as_is(self, fake_coordinator) -> None:
        fake_coordinator.data = {"grid_power": "abc"}
        assert self._formatted(fake_coordinator).native_value == "abc"


class TestValueMappingEdge:
    def test_nan_falls_through_mapping(self, fake_coordinator) -> None:
        fake_coordinator.data = {"meter_type": math.nan}
        entity = _make_sensor(
            fake_coordinator,
            "meter_type",
            {
                "address": 10630,
                "data_type": "UINT16",
                "unit": "/",
                "gain": 1,
                "value_mapping": {1: "single_phase"},
            },
        )
        assert math.isnan(entity.native_value)


class TestNativeValueUnavailable:
    def test_none_when_coordinator_failed(self, fake_coordinator) -> None:
        fake_coordinator.last_update_success = False
        fake_coordinator.data = {"load_power": 300}
        entity = _make_sensor(
            fake_coordinator,
            "load_power",
            {"address": 10010, "data_type": "INT32", "unit": "W", "gain": 1},
        )
        assert entity.native_value is None


class TestExtraStateAttributes:
    def test_basic_attributes(self, fake_coordinator) -> None:
        entity = _make_sensor(
            fake_coordinator,
            "load_power",
            {"address": 10010, "data_type": "INT32", "unit": "W", "gain": 1, "count": 2},
        )
        attrs = entity.extra_state_attributes
        assert attrs["modbus_address"] == 10010
        assert attrs["data_type"] == "INT32"
        assert attrs["register_count"] == 2

    def test_direction_format_exposes_raw_value(self, fake_coordinator) -> None:
        fake_coordinator.data = {"grid_power": -300}
        entity = _make_sensor(
            fake_coordinator,
            "grid_power",
            {
                "address": 10500,
                "data_type": "INT32",
                "unit": "W",
                "gain": 1,
                "power_direction_format": {"positive": "Import", "negative": "Export"},
            },
        )
        attrs = entity.extra_state_attributes
        assert attrs["raw_value"] == -300
        assert attrs["unit"] == "W"

    def test_direction_format_non_numeric_has_no_raw(self, fake_coordinator) -> None:
        fake_coordinator.data = {"grid_power": "abc"}
        entity = _make_sensor(
            fake_coordinator,
            "grid_power",
            {
                "address": 10500,
                "data_type": "INT32",
                "unit": "W",
                "gain": 1,
                "power_direction_format": {"positive": "Import", "negative": "Export"},
            },
        )
        assert "raw_value" not in entity.extra_state_attributes

    def test_aggregated_exposes_components(self, fake_coordinator) -> None:
        fake_coordinator.data = {"primary": 100, "secondary": 50}
        entity = _make_sensor(
            fake_coordinator,
            "primary",
            {
                "address": 10300,
                "data_type": "INT32",
                "unit": "W",
                "gain": 1,
                "additional_sources": ["secondary", "tertiary"],
            },
        )
        attrs = entity.extra_state_attributes
        assert attrs["primary_value"] == 100
        assert attrs["additional_sources"] == ["secondary", "tertiary"]
        assert attrs["source_secondary"] == 50
        assert "source_tertiary" not in attrs


class TestSmartMeterNewCtDataPointSensors:
    """Coding #3671: sensor behaviour for the newly added CT-group points."""

    def test_power_factor_gain_1000_gives_three_decimals(
        self, fake_coordinator
    ) -> None:
        fake_coordinator.data = {"primary_total_power_factor": 0.995}
        entity = _make_sensor(
            fake_coordinator,
            "primary_total_power_factor",
            {"address": 10648, "data_type": "INT16", "unit": "/", "gain": 1000},
        )
        assert entity.suggested_display_precision == 3
        assert entity.native_value == 0.995

    def test_reactive_power_is_measurement(self, fake_coordinator) -> None:
        entity = _make_sensor(
            fake_coordinator,
            "secondary_total_reactive_power",
            {"address": 10677, "data_type": "INT32", "unit": "W", "gain": 1},
        )
        assert entity.device_class == "power"
        assert entity.state_class == "measurement"

    def test_secondary_energy_is_total_increasing(self, fake_coordinator) -> None:
        fake_coordinator.data = {"secondary_total_forward_active_energy": 123.4}
        entity = _make_sensor(
            fake_coordinator,
            "secondary_total_forward_active_energy",
            {"address": 10686, "data_type": "UINT32", "unit": "kWh", "gain": 10},
        )
        assert entity.device_class == "energy"
        assert entity.state_class == "total_increasing"
        assert entity.native_value == 123.4


class TestSmartPlugCumulativeEnergySensor:
    """Coding #3669: A17X8 plug cumulative energy (register 30033)."""

    def test_plug_cumulative_energy_is_total_increasing(
        self, fake_coordinator
    ) -> None:
        fake_coordinator.data = {"cumulative_energy": 123.456}
        entity = _make_sensor(
            fake_coordinator,
            "cumulative_energy",
            {"address": 30033, "data_type": "UINT32", "unit": "kWh", "gain": 1000},
        )
        assert entity.device_class == "energy"
        assert entity.state_class == "total_increasing"
        assert entity.suggested_display_precision == 3
        assert entity.native_value == 123.456
