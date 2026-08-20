"""Unit tests for diagnostics.async_get_config_entry_diagnostics."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.anker_solix_official.const import DOMAIN
from custom_components.anker_solix_official.diagnostics import (
    async_get_config_entry_diagnostics,
)


class _FakeCoordinator:
    """Minimal stand-in exposing only the attributes diagnostics.py reads."""

    def __init__(self) -> None:
        self._status = "connected"
        self._consecutive_failures = 0
        self._ever_connected = True
        self._initial_mode_sent = True
        self.ip_address = "192.168.1.50"
        self.device_info = {"model": "Solarbank Max"}
        self.data = {"device_sw_version": "1.2.3.4", "device_sn": "SECRETSN01"}


@pytest.fixture
def entry(hass) -> MockConfigEntry:
    e = MockConfigEntry(
        domain=DOMAIN,
        data={"ip_address": "192.168.1.50", "device_name": "Test Device", "port": 502},
        unique_id="192.168.1.50",
    )
    e.add_to_hass(hass)
    return e


class TestAsyncGetConfigEntryDiagnostics:
    """Structure and privacy-redaction of the diagnostics payload."""

    async def test_reports_connection_state_fields(self, hass, entry) -> None:
        # Arrange
        coord = _FakeCoordinator()
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord

        # Act
        result = await async_get_config_entry_diagnostics(hass, entry)

        # Assert
        assert result["connection"]["status"] == "connected"
        assert result["connection"]["consecutive_failures"] == 0
        assert result["connection"]["ever_connected"] is True
        assert result["connection"]["initial_mode_sent"] is True

    async def test_redacts_ip_address_in_config_entry_and_connection(
        self, hass, entry
    ) -> None:
        # Arrange
        coord = _FakeCoordinator()
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord

        # Act
        result = await async_get_config_entry_diagnostics(hass, entry)

        # Assert: the raw IP must never appear in either redacted location.
        assert result["connection"]["ip_address"] != "192.168.1.50"
        assert result["config_entry"]["data"]["ip_address"] != "192.168.1.50"

    async def test_redacts_device_sn_in_register_data(self, hass, entry) -> None:
        # Arrange
        coord = _FakeCoordinator()
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord

        # Act
        result = await async_get_config_entry_diagnostics(hass, entry)

        # Assert
        assert result["register_data"]["device_sn"] != "SECRETSN01"

    async def test_reports_device_model_and_firmware(self, hass, entry) -> None:
        # Arrange
        coord = _FakeCoordinator()
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord

        # Act
        result = await async_get_config_entry_diagnostics(hass, entry)

        # Assert
        assert result["device"]["model"] == "Solarbank Max"
        assert result["device"]["firmware"] == "1.2.3.4"

    async def test_ip_matches_config_entry_flag_true_when_equal(
        self, hass, entry
    ) -> None:
        # Arrange: coordinator.ip_address matches entry.data["ip_address"].
        coord = _FakeCoordinator()
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord

        # Act
        result = await async_get_config_entry_diagnostics(hass, entry)

        # Assert
        assert result["connection"]["ip_matches_config_entry"] is True

    async def test_ip_matches_config_entry_flag_false_after_mdns_switch(
        self, hass, entry
    ) -> None:
        # Arrange: coordinator's in-memory IP has diverged from entry.data
        # (mDNS auto-recovery switched it without persisting to config).
        coord = _FakeCoordinator()
        coord.ip_address = "192.168.1.99"
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord

        # Act
        result = await async_get_config_entry_diagnostics(hass, entry)

        # Assert
        assert result["connection"]["ip_matches_config_entry"] is False

    async def test_no_coordinator_data_defaults_firmware_to_none(
        self, hass, entry
    ) -> None:
        # Arrange: coordinator.data is None (never received data yet).
        coord = _FakeCoordinator()
        coord.data = None
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord

        # Act
        result = await async_get_config_entry_diagnostics(hass, entry)

        # Assert
        assert result["device"]["firmware"] is None
        assert result["register_data"] == {}
