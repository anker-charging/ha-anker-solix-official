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

from typing import Any

import pytest
from custom_components.anker_solix_official.sensor import ModbusLocalDeviceSensor


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
