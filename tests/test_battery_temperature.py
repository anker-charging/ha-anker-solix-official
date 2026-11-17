"""Unit tests for battery temperature sensors and display precision."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from custom_components.anker_solix_official.config_utils import (
    parse_device_configuration,
)
from custom_components.anker_solix_official.device_config import AnkerSolixDeviceConfig
from custom_components.anker_solix_official.modbus_client import AnkerSolixModbusClient
from custom_components.anker_solix_official.sensor import (
    ModbusLocalDeviceSensor,
    _is_sensor_entity,
)

from ._fakes import FakeCoordinator

CONFIG_DIR = (
    Path(__file__).resolve().parents[1]
    / "custom_components/anker_solix_official/config"
)
PROFILES = [
    (
        "3c176e100f3c1d7a36ef64571e6aacd16b539f3657c11745f7bd969ccaf8b213.yaml",
        "Anker SOLIX XE",
    ),
    (
        "482b2e95c3aaf78b871f463a71ba7f4fc87f9181872ac6168a90ad198fa3853a.yaml",
        "Anker SOLIX Solarbank Max",
    ),
    (
        "58f0132b5f7979b2cfa43a0eb1fca770053288032386ff6a4da5ed2d72d4ea35.yaml",
        "Anker SOLIX Solarbank 4 E5000 Pro",
    ),
    (
        "8fcbb87c685781b1d70d784a79eb923098955df2aaf199095ce7767bb70b913d.yaml",
        "Anker SOLIX Solarbank Max AC",
    ),
]
KEY = "battery_temperature"


@pytest.fixture(params=PROFILES, ids=["xe", "max", "solarbank_4", "max_ac"])
async def temperature_profile(request) -> tuple[dict[str, Any], tuple[int, int, str]]:
    """Use the same profile loader and parser as the integration."""
    filename, expected_model = request.param
    config = await AnkerSolixDeviceConfig(hass=None).load_device_config_by_file_async(
        str(CONFIG_DIR / filename)
    )
    assert config is not None
    assert config["product_info"]["default_name"] == expected_model
    points, ranges = parse_device_configuration(config)
    point = points[KEY]
    # The temperature is already in an input batch: exposing it adds no reads.
    batch = next(r for r in ranges if r[0] <= 10156 <= r[1] and r[2] == "input")
    return point, batch


def test_temperature_entity_metadata(temperature_profile) -> None:
    point, _ = temperature_profile
    assert point["address"] == 10156
    assert point["data_type"] == "INT16"
    assert point["count"] == 1
    assert _is_sensor_entity(KEY, point)

    sensor = ModbusLocalDeviceSensor(FakeCoordinator(), KEY, point)
    assert sensor.translation_key == KEY
    assert sensor.unique_id == "test-entry_battery_temperature"
    assert sensor.device_class == "temperature"
    assert sensor.state_class == "measurement"
    assert sensor.native_unit_of_measurement == "°C"
    assert sensor.suggested_display_precision == 0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(250, 25.0), (0, 0.0), (0xFFCE, -5.0), (253, 25.3), (0xFFCB, -5.3)],
)
async def test_temperature_from_input_batch(temperature_profile, raw, expected) -> None:
    """Decode signed tenths at the correct offset in the existing input batch."""
    point, batch = temperature_profile
    start, end, _ = batch
    registers = [0] * (end - start + 1)
    registers[10156 - start] = raw
    transport = MagicMock(connected=True)
    transport.read_input_registers = AsyncMock(
        return_value=SimpleNamespace(registers=registers, isError=lambda: False)
    )
    client = AnkerSolixModbusClient(ip_address="127.0.0.1", port=502)
    client.client = transport
    coordinator = FakeCoordinator()
    sensor = ModbusLocalDeviceSensor(coordinator, KEY, point)

    coordinator.data = await client.get_all_data({KEY: point}, [batch])

    assert sensor.native_value == pytest.approx(expected)
    transport.read_input_registers.assert_awaited_once_with(
        address=start, count=end - start + 1
    )
    transport.read_holding_registers.assert_not_called()


async def test_failed_temperature_read_is_unknown(temperature_profile) -> None:
    """An incomplete batch must not turn a missing temperature into zero."""
    point, batch = temperature_profile
    transport = MagicMock(connected=True)
    transport.read_input_registers = AsyncMock(
        return_value=SimpleNamespace(registers=[], isError=lambda: False)
    )
    client = AnkerSolixModbusClient(ip_address="127.0.0.1", port=502)
    client.client = transport
    coordinator = FakeCoordinator({KEY: 25.3})
    sensor = ModbusLocalDeviceSensor(coordinator, KEY, point)

    coordinator.data = await client.get_all_data({KEY: point}, [batch])

    assert KEY not in coordinator.data
    assert sensor.native_value is None


class TestSuggestedDisplayPrecision:
    """Profile precision overrides must preserve existing sensor behaviour."""

    @pytest.mark.parametrize(
        ("precision", "expected"),
        [(0, 0), (2, 2), (-1, 1), (1.5, 1), ("0", 1), (True, 1), (None, 1)],
    )
    def test_valid_integer_overrides_gain_and_invalid_values_keep_inference(
        self, precision, expected
    ) -> None:
        sensor = ModbusLocalDeviceSensor(
            FakeCoordinator(),
            "temperature",
            {
                "data_type": "INT16",
                "unit": "°C",
                "gain": 10,
                "suggested_display_precision": precision,
            },
        )
        assert sensor.suggested_display_precision == expected

    @pytest.mark.parametrize(
        "config",
        [
            {"data_type": "STRING"},
            {"data_type": "VERSION"},
            {
                "data_type": "INT32",
                "power_direction_format": {"positive": "Discharge", "negative": "Charge"},
            },
        ],
    )
    def test_text_sensors_ignore_precision_override(self, config) -> None:
        sensor = ModbusLocalDeviceSensor(
            FakeCoordinator(),
            "text_sensor",
            {**config, "gain": 10, "suggested_display_precision": 0},
        )
        assert sensor.suggested_display_precision is None
