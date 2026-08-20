"""Unit tests for AnkerSolixOfficialConfigFlow.

AnkerSolixModbusClient is monkeypatched at the config_flow module level so
these tests exercise only the flow's own validation/orchestration logic
(IP format check, step transitions, unique_id dedup) without any real
Modbus I/O.
"""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.anker_solix_official import config_flow as config_flow_module
from custom_components.anker_solix_official.config_flow import (
    AnkerSolixOfficialConfigFlow,
)
from custom_components.anker_solix_official.const import DOMAIN


class _FakeModbusClient:
    """Stand-in for AnkerSolixModbusClient used by the config flow."""

    connect_result: bool = True
    read_device_pn_result: tuple[str, str, str] = ("hash1", "PN001", "0xAB")
    connect_side_effect: BaseException | None = None

    def __init__(self, ip_address: str, port: int = 502) -> None:
        self.ip_address = ip_address
        self.port = port
        self.client = None
        self.disconnect_called = False

    async def connect(self) -> bool:
        if self.__class__.connect_side_effect:
            raise self.__class__.connect_side_effect
        return self.__class__.connect_result

    async def disconnect(self) -> None:
        self.disconnect_called = True

    async def read_device_pn(self) -> tuple[str, str, str]:
        return self.__class__.read_device_pn_result


@pytest.fixture(autouse=True)
def _reset_fake_client_class_state():
    _FakeModbusClient.connect_result = True
    _FakeModbusClient.read_device_pn_result = ("hash1", "PN001", "0xAB")
    _FakeModbusClient.connect_side_effect = None
    yield


@pytest.fixture(autouse=True)
def _patch_modbus_client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(config_flow_module, "AnkerSolixModbusClient", _FakeModbusClient)


@pytest.fixture(autouse=True)
def _patch_config_file_lookup(monkeypatch: pytest.MonkeyPatch):
    """By default, pretend the device's config file exists (device supported)."""

    async def _fake_check_device_support(self, ip_address, port=502):
        return True, ""

    monkeypatch.setattr(
        AnkerSolixOfficialConfigFlow,
        "_check_device_support",
        _fake_check_device_support,
    )


class TestValidateIpv4:
    """_validate_ipv4() regex-based format check."""

    def test_valid_ip_returns_true(self) -> None:
        flow = AnkerSolixOfficialConfigFlow()
        assert flow._validate_ipv4("192.168.1.1") is True

    def test_boundary_values_are_valid(self) -> None:
        flow = AnkerSolixOfficialConfigFlow()
        assert flow._validate_ipv4("0.0.0.0") is True
        assert flow._validate_ipv4("255.255.255.255") is True

    def test_out_of_range_octet_is_invalid(self) -> None:
        flow = AnkerSolixOfficialConfigFlow()
        assert flow._validate_ipv4("192.168.1.256") is False

    def test_non_numeric_is_invalid(self) -> None:
        flow = AnkerSolixOfficialConfigFlow()
        assert flow._validate_ipv4("not.an.ip.address") is False

    def test_whitespace_is_stripped_before_validation(self) -> None:
        flow = AnkerSolixOfficialConfigFlow()
        assert flow._validate_ipv4("  192.168.1.1  ") is True


class TestTestModbusConnection:
    """_test_modbus_connection() connect-then-disconnect lifecycle."""

    async def test_successful_connection_returns_true(self) -> None:
        _FakeModbusClient.connect_result = True
        flow = AnkerSolixOfficialConfigFlow()

        result = await flow._test_modbus_connection("192.168.1.1")

        assert result is True

    async def test_failed_connection_returns_false(self) -> None:
        _FakeModbusClient.connect_result = False
        flow = AnkerSolixOfficialConfigFlow()

        result = await flow._test_modbus_connection("192.168.1.1")

        assert result is False

    async def test_connect_exception_is_caught_and_returns_false(self) -> None:
        _FakeModbusClient.connect_side_effect = OSError("network unreachable")
        flow = AnkerSolixOfficialConfigFlow()

        result = await flow._test_modbus_connection("192.168.1.1")

        assert result is False


class TestAsyncStepUser:
    """async_step_user() the primary IP-entry config flow step."""

    async def test_empty_ip_shows_invalid_ip_error(self, hass) -> None:
        flow = AnkerSolixOfficialConfigFlow()
        flow.hass = hass

        result = await flow.async_step_user({"ip_address": ""})

        assert result["type"] == "form"
        assert result["errors"]["base"] == "invalid_ip"

    async def test_malformed_ip_shows_invalid_ip_error(self, hass) -> None:
        flow = AnkerSolixOfficialConfigFlow()
        flow.hass = hass

        result = await flow.async_step_user({"ip_address": "not-an-ip"})

        assert result["errors"]["base"] == "invalid_ip"

    async def test_connection_failure_shows_cannot_connect_error(self, hass) -> None:
        _FakeModbusClient.connect_result = False
        flow = AnkerSolixOfficialConfigFlow()
        flow.hass = hass

        result = await flow.async_step_user({"ip_address": "192.168.1.1"})

        assert result["errors"]["base"] == "cannot_connect"

    async def test_no_user_input_shows_the_form(self, hass) -> None:
        flow = AnkerSolixOfficialConfigFlow()
        flow.hass = hass

        result = await flow.async_step_user(None)

        assert result["type"] == "form"
        assert result["step_id"] == "user"

    async def test_successful_flow_creates_entry_with_expected_data(self, hass) -> None:
        flow = AnkerSolixOfficialConfigFlow()
        flow.hass = hass
        flow.handler = DOMAIN
        flow.context = {}

        result = await flow.async_step_user({"ip_address": "192.168.1.1"})

        assert result["type"] == "create_entry"
        assert result["data"]["ip_address"] == "192.168.1.1"
        assert result["data"]["port"] == 502

    async def test_unsupported_device_shows_device_not_supported_error(
        self, hass, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _fake_check_device_support(self, ip_address, port=502):
            return False, ""

        monkeypatch.setattr(
            AnkerSolixOfficialConfigFlow,
            "_check_device_support",
            _fake_check_device_support,
        )
        flow = AnkerSolixOfficialConfigFlow()
        flow.hass = hass

        result = await flow.async_step_user({"ip_address": "192.168.1.1"})

        assert result["errors"]["base"] == "device_not_supported"

    async def test_already_configured_ip_aborts(self, hass) -> None:
        # Arrange: an existing enabled entry with the same unique_id.
        existing = MockConfigEntry(
            domain=DOMAIN,
            data={"ip_address": "192.168.1.1"},
            unique_id="192.168.1.1",
        )
        existing.add_to_hass(hass)

        flow = AnkerSolixOfficialConfigFlow()
        flow.hass = hass
        flow.handler = DOMAIN
        flow.context = {}

        # Act
        result = await flow.async_step_user({"ip_address": "192.168.1.1"})

        # Assert
        assert result["type"] == "abort"
        assert result["reason"] == "already_configured"


class TestAsyncStepImport:
    """async_step_import() YAML-import config flow path."""

    async def test_invalid_ip_aborts_with_invalid_ip_reason(self, hass) -> None:
        flow = AnkerSolixOfficialConfigFlow()
        flow.hass = hass

        result = await flow.async_step_import({"ip_address": "bad-ip"})

        assert result["type"] == "abort"
        assert result["reason"] == "invalid_ip"

    async def test_connection_failure_aborts_with_cannot_connect(self, hass) -> None:
        _FakeModbusClient.connect_result = False
        flow = AnkerSolixOfficialConfigFlow()
        flow.hass = hass

        result = await flow.async_step_import({"ip_address": "192.168.1.1"})

        assert result["reason"] == "cannot_connect"

    async def test_successful_import_creates_entry(self, hass) -> None:
        flow = AnkerSolixOfficialConfigFlow()
        flow.hass = hass
        flow.handler = DOMAIN
        flow.context = {}

        result = await flow.async_step_import(
            {"ip_address": "192.168.1.1", "port": 502, "device_name": "My Device"}
        )

        assert result["type"] == "create_entry"
        assert result["data"]["device_name"] == "My Device"


class TestAsyncGetOptionsFlow:
    """async_get_options_flow() static factory method."""

    def test_returns_an_options_flow_handler(self) -> None:
        entry = MockConfigEntry(domain=DOMAIN, data={}, entry_id="abc123")
        handler = AnkerSolixOfficialConfigFlow.async_get_options_flow(entry)
        assert handler._entry_id == "abc123"
