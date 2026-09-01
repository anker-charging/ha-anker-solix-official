"""Unit tests for AnkerSolixOfficialCoordinator.

Coordinator.__init__ spawns a background connection loop task against the
real modbus_manager/device_config objects it creates internally. To keep
these tests focused on the coordinator's own logic (not full modbus I/O),
the background task is always awaited/cancelled in fixture teardown, and
most tests target the coordinator's synchronous helpers and its async
methods directly (bypassing the background loop) with a fake modbus_manager
substituted in after construction.
"""

from __future__ import annotations

import asyncio

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.anker_solix_official.const import DOMAIN
from custom_components.anker_solix_official.coordinator import (
    AnkerSolixOfficialCoordinator,
)
from custom_components.anker_solix_official.device_logger import WriteResult


@pytest.fixture
async def coordinator(hass):
    """Build a coordinator with its background loop stopped immediately.

    The connection_loop task cannot be prevented from spawning (it's wired
    into __init__ via the HA event bus / call_soon_threadsafe), so instead
    it is allowed to start and then immediately told to stop via _stop_bg,
    with the task itself awaited so no warning about a lingering task leaks
    into other tests.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"device_name": "Test Device", "ip_address": "127.0.0.1", "port": 502},
        unique_id="127.0.0.1",
    )
    entry.add_to_hass(hass)
    coord = AnkerSolixOfficialCoordinator(hass, entry)
    await asyncio.sleep(0)  # let the scheduled _spawn_task callback run
    coord._stop_bg = True
    if coord._bg_task is not None:
        try:
            await asyncio.wait_for(coord._bg_task, timeout=2)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            pass
    yield coord
    await coord.async_shutdown()


class TestVersionComparison:
    """_normalize_version() and _compare_version() static helpers."""

    def test_normalize_strips_leading_v(self) -> None:
        assert AnkerSolixOfficialCoordinator._normalize_version("v0.0.5.5") == "0.0.5.5"

    def test_normalize_strips_leading_uppercase_v(self) -> None:
        assert AnkerSolixOfficialCoordinator._normalize_version("V1.2.3.4") == "1.2.3.4"

    def test_normalize_without_prefix_is_unchanged(self) -> None:
        assert AnkerSolixOfficialCoordinator._normalize_version("1.2.3.4") == "1.2.3.4"

    def test_normalize_strips_surrounding_whitespace(self) -> None:
        assert AnkerSolixOfficialCoordinator._normalize_version("  v1.0.0.0  ") == "1.0.0.0"

    @pytest.mark.parametrize(
        ("version", "threshold", "expected"),
        [
            ("0.0.5.5", "0.0.5.4", 1),
            ("0.0.5.5", "0.0.5.5", 0),
            ("0.0.5.4", "0.0.5.5", -1),
            ("0.0.5.5", "0.0.4.50", 1),
            ("0.0.5.5", "0.0.6.1", -1),
        ],
    )
    def test_compare_version_ordering(
        self, version: str, threshold: str, expected: int
    ) -> None:
        assert AnkerSolixOfficialCoordinator._compare_version(version, threshold) == expected

    def test_compare_version_with_different_segment_counts(self) -> None:
        # Arrange: shorter version string is padded with zeros before compare.
        assert AnkerSolixOfficialCoordinator._compare_version("1.2", "1.2.0.0") == 0
        assert AnkerSolixOfficialCoordinator._compare_version("1.2.1", "1.2.0.0") == 1

    def test_compare_version_with_malformed_input_defaults_to_zero_tuple(self) -> None:
        # Arrange: non-numeric segments must not raise, fall back to (0,).
        result = AnkerSolixOfficialCoordinator._compare_version("not-a-version", "1.0.0.0")
        assert result == -1


class TestValidateIpv4:
    """_validate_ipv4() format check used to distinguish SN from IP unique_id."""

    def test_valid_ipv4_returns_true(self) -> None:
        assert AnkerSolixOfficialCoordinator._validate_ipv4("192.168.1.1") is True

    def test_valid_ipv4_boundary_values(self) -> None:
        assert AnkerSolixOfficialCoordinator._validate_ipv4("0.0.0.0") is True
        assert AnkerSolixOfficialCoordinator._validate_ipv4("255.255.255.255") is True

    def test_out_of_range_octet_returns_false(self) -> None:
        assert AnkerSolixOfficialCoordinator._validate_ipv4("192.168.1.256") is False

    def test_wrong_number_of_parts_returns_false(self) -> None:
        assert AnkerSolixOfficialCoordinator._validate_ipv4("192.168.1") is False

    def test_non_numeric_parts_return_false(self) -> None:
        assert AnkerSolixOfficialCoordinator._validate_ipv4("abc.def.ghi.jkl") is False

    def test_serial_number_is_not_a_valid_ipv4(self) -> None:
        # Arrange: real SNs are alphanumeric strings, never dotted quads.
        assert AnkerSolixOfficialCoordinator._validate_ipv4("A1B2C3D4E5F6G7H8") is False


class TestIsConnected:
    """is_connected() public state reflecting both status and failure flag."""

    def test_connected_and_not_failed_is_true(self, coordinator) -> None:
        coordinator._status = "connected"
        coordinator._connection_failed = False
        assert coordinator.is_connected() is True

    def test_status_not_connected_is_false(self, coordinator) -> None:
        coordinator._status = "disconnected"
        coordinator._connection_failed = False
        assert coordinator.is_connected() is False

    def test_connected_but_failed_flag_set_is_false(self, coordinator) -> None:
        # Arrange: a race where status hasn't been reset yet but a failure
        # was just recorded -- is_connected() must still report False.
        coordinator._status = "connected"
        coordinator._connection_failed = True
        assert coordinator.is_connected() is False


class TestWriteProtection:
    """set_write_protection() / get_protected_value() / clear_write_protection()."""

    def test_protected_value_is_returned_while_active(self, coordinator) -> None:
        coordinator.set_write_protection("operating_mode", "self_use", duration=10)
        is_protected, value = coordinator.get_protected_value("operating_mode")
        assert is_protected is True
        assert value == "self_use"

    def test_unprotected_key_returns_false_none(self, coordinator) -> None:
        is_protected, value = coordinator.get_protected_value("never_set")
        assert is_protected is False
        assert value is None

    def test_expired_protection_is_cleaned_up_and_returns_false(self, coordinator) -> None:
        # Arrange: duration=0 means protected_until is already in the past
        # by the time get_protected_value() checks it.
        coordinator.set_write_protection("mode", "value", duration=-1)

        # Act
        is_protected, value = coordinator.get_protected_value("mode")

        # Assert
        assert is_protected is False
        assert value is None
        assert "mode" not in coordinator._protected_values

    def test_clear_write_protection_removes_entry(self, coordinator) -> None:
        coordinator.set_write_protection("mode", "value", duration=100)
        coordinator.clear_write_protection("mode")
        assert "mode" not in coordinator._protected_values

    def test_clear_write_protection_on_missing_key_is_a_no_op(self, coordinator) -> None:
        coordinator.clear_write_protection("never_set")  # must not raise

    def test_default_duration_is_used_when_none_given(self, coordinator) -> None:
        coordinator.set_write_protection("mode", "value")
        is_protected, _ = coordinator.get_protected_value("mode")
        assert is_protected is True


class TestUserSelection:
    """set_user_selection() / get_user_selection() / clear_user_selection()."""

    def test_stored_selection_is_retrievable(self, coordinator) -> None:
        coordinator.set_user_selection("direction", "charge")
        assert coordinator.get_user_selection("direction") == "charge"

    def test_unset_selection_returns_none(self, coordinator) -> None:
        assert coordinator.get_user_selection("never_set") is None

    def test_clear_removes_selection(self, coordinator) -> None:
        coordinator.set_user_selection("direction", "charge")
        coordinator.clear_user_selection("direction")
        assert coordinator.get_user_selection("direction") is None

    def test_clear_on_missing_key_is_a_no_op(self, coordinator) -> None:
        coordinator.clear_user_selection("never_set")  # must not raise


class TestGetStoredSn:
    """_get_stored_sn() distinguishing a real SN unique_id from an IP fallback."""

    async def test_sn_like_unique_id_is_returned(self, hass) -> None:
        # Arrange: unique_id is a 10+ char alphanumeric string, not an IPv4.
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={"ip_address": "127.0.0.1", "port": 502},
            unique_id="ABC1234567890",
        )
        entry.add_to_hass(hass)
        coord = AnkerSolixOfficialCoordinator(hass, entry)
        await asyncio.sleep(0)
        coord._stop_bg = True

        # Act & Assert
        assert coord._get_stored_sn() == "ABC1234567890"
        await coord.async_shutdown()

    async def test_ip_unique_id_returns_empty_string(self, hass) -> None:
        # Arrange: unique_id fell back to the IP address (SN read failed
        # during config flow) -- must not be treated as a searchable SN.
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={"ip_address": "127.0.0.1", "port": 502},
            unique_id="127.0.0.1",
        )
        entry.add_to_hass(hass)
        coord = AnkerSolixOfficialCoordinator(hass, entry)
        await asyncio.sleep(0)
        coord._stop_bg = True

        # Act & Assert
        assert coord._get_stored_sn() == ""
        await coord.async_shutdown()

    async def test_short_unique_id_returns_empty_string(self, hass) -> None:
        # Arrange: below the 10-char minimum length gate.
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={"ip_address": "127.0.0.1", "port": 502},
            unique_id="SHORT1",
        )
        entry.add_to_hass(hass)
        coord = AnkerSolixOfficialCoordinator(hass, entry)
        await asyncio.sleep(0)
        coord._stop_bg = True

        # Act & Assert
        assert coord._get_stored_sn() == ""
        await coord.async_shutdown()


class TestIsRegisterAvailable:
    """is_register_available() lookup against the unavailable-registers set."""

    def test_register_not_in_set_is_available(self, coordinator) -> None:
        assert coordinator.is_register_available(100) is True

    def test_register_in_set_is_unavailable(self, coordinator) -> None:
        coordinator._unavailable_registers.add(100)
        assert coordinator.is_register_available(100) is False


class TestGetDataPointAddress:
    """get_data_point_address() lookup against the config cache."""

    def test_no_config_cache_returns_none(self, coordinator) -> None:
        coordinator._device_config_cache = None
        assert coordinator.get_data_point_address("power") is None

    def test_missing_key_returns_none(self, coordinator) -> None:
        coordinator._device_config_cache = {"power": {"address": 100}}
        assert coordinator.get_data_point_address("voltage") is None

    def test_present_key_returns_address(self, coordinator) -> None:
        coordinator._device_config_cache = {"power": {"address": 100}}
        assert coordinator.get_data_point_address("power") == 100


class TestIsConfigCacheValid:
    """_is_config_cache_valid() all-three-fields-set gate."""

    def test_all_fields_set_and_flag_true_is_valid(self, coordinator) -> None:
        coordinator._config_cache_valid = True
        coordinator._device_config_cache = {"a": {}}
        coordinator._batch_ranges_cache = []
        assert coordinator._is_config_cache_valid() is True

    def test_flag_false_is_invalid_even_with_data_present(self, coordinator) -> None:
        coordinator._config_cache_valid = False
        coordinator._device_config_cache = {"a": {}}
        coordinator._batch_ranges_cache = []
        assert coordinator._is_config_cache_valid() is False

    def test_missing_device_config_cache_is_invalid(self, coordinator) -> None:
        coordinator._config_cache_valid = True
        coordinator._device_config_cache = None
        coordinator._batch_ranges_cache = []
        assert coordinator._is_config_cache_valid() is False


class TestShouldAttemptReconnection:
    """_should_attempt_reconnection() backoff-interval gate."""

    def test_not_failed_returns_false(self, coordinator) -> None:
        coordinator._connection_failed = False
        assert coordinator._should_attempt_reconnection() is False

    def test_failed_but_within_retry_interval_returns_false(self, coordinator) -> None:
        import time

        coordinator._connection_failed = True
        coordinator._last_connection_attempt = time.time()
        coordinator._connection_retry_interval = 100
        assert coordinator._should_attempt_reconnection() is False

    def test_failed_and_past_retry_interval_returns_true(self, coordinator) -> None:
        coordinator._connection_failed = True
        coordinator._last_connection_attempt = 0
        coordinator._connection_retry_interval = 1
        assert coordinator._should_attempt_reconnection() is True


class TestHandleConnectionSuccess:
    """_handle_connection_success() recovery-from-failure state reset."""

    def test_resets_failure_state_when_previously_failed(self, coordinator) -> None:
        coordinator._connection_failed = True
        coordinator._consecutive_failures = 5
        coordinator._device_unavailable_logged = True
        coordinator._connection_retry_interval = 300

        coordinator._handle_connection_success()

        assert coordinator._connection_failed is False
        assert coordinator._consecutive_failures == 0
        assert coordinator._device_unavailable_logged is False
        assert coordinator._connection_retry_interval == 10

    def test_no_op_when_already_connected(self, coordinator) -> None:
        coordinator._connection_failed = False
        coordinator._consecutive_failures = 3

        coordinator._handle_connection_success()

        # Assert: consecutive_failures is untouched by this branch (it is
        # reset elsewhere in the connection loop, not by this method).
        assert coordinator._consecutive_failures == 3


class _FakeModbusManager:
    """Stand-in for ModbusConnectionManager, scripted per test."""

    def __init__(self) -> None:
        self.client_available: bool = True
        self.read_device_pn_result: tuple[str, str, str] = ("hash1", "PN001", "0xAB")
        self.write_register_result: WriteResult = WriteResult(success=True)
        self.force_disconnect_called = False
        self.disconnect_called = False
        self.update_ip_address_calls: list[str] = []

    async def get_client(self):
        return object() if self.client_available else None

    async def read_device_pn(self) -> tuple[str, str, str]:
        return self.read_device_pn_result

    async def write_register(self, address, value, data_type) -> WriteResult:
        return self.write_register_result

    async def force_disconnect(self) -> None:
        self.force_disconnect_called = True

    async def disconnect(self) -> None:
        self.disconnect_called = True

    async def update_ip_address(self, new_ip: str) -> None:
        self.update_ip_address_calls.append(new_ip)


class TestReadDevicePn:
    """_read_device_pn() logging wrapper around modbus_manager.read_device_pn()."""

    async def test_successful_read_returns_the_manager_result(self, coordinator) -> None:
        # Arrange
        fake_manager = _FakeModbusManager()
        fake_manager.read_device_pn_result = ("hashABC", "PN123", "0x1234")
        coordinator.modbus_manager = fake_manager

        # Act
        result = await coordinator._read_device_pn()

        # Assert
        assert result == ("hashABC", "PN123", "0x1234")

    async def test_empty_hash_result_is_returned_unchanged(self, coordinator) -> None:
        # Arrange
        fake_manager = _FakeModbusManager()
        fake_manager.read_device_pn_result = ("", "", "")
        coordinator.modbus_manager = fake_manager

        # Act
        result = await coordinator._read_device_pn()

        # Assert
        assert result == ("", "", "")

    async def test_manager_exception_is_caught_and_returns_empty_tuple(
        self, coordinator
    ) -> None:
        # Arrange
        class _ExplodingManager(_FakeModbusManager):
            async def read_device_pn(self):
                raise RuntimeError("boom")

        coordinator.modbus_manager = _ExplodingManager()

        # Act
        result = await coordinator._read_device_pn()

        # Assert
        assert result == ("", "", "")


class TestGetConfigFilePath:
    """_get_config_file_path() PN-hash-to-YAML-file resolution."""

    async def test_no_pn_hash_returns_empty_string(self, coordinator) -> None:
        # Arrange
        fake_manager = _FakeModbusManager()
        fake_manager.read_device_pn_result = ("", "", "")
        coordinator.modbus_manager = fake_manager

        # Act
        result = await coordinator._get_config_file_path()

        # Assert
        assert result == ""

    async def test_existing_config_file_returns_its_relative_path(
        self, coordinator
    ) -> None:
        # Arrange: use a real PN hash that ships with the repo's config/ dir.
        import os

        config_dir = os.path.join(
            os.path.dirname(
                __import__(
                    "custom_components.anker_solix_official.coordinator",
                    fromlist=["x"],
                ).__file__
            ),
            "config",
        )
        existing_files = [f for f in os.listdir(config_dir) if f.endswith(".yaml")]
        assert existing_files, "expected at least one shipped device config"
        pn_hash = existing_files[0].removesuffix(".yaml")

        fake_manager = _FakeModbusManager()
        fake_manager.read_device_pn_result = (pn_hash, "PN001", "0xAB")
        coordinator.modbus_manager = fake_manager

        # Act
        result = await coordinator._get_config_file_path()

        # Assert
        assert result == f"config/{pn_hash}.yaml"

    async def test_unsupported_pn_hash_returns_empty_string(self, coordinator) -> None:
        # Arrange: a PN hash with no matching shipped config file.
        fake_manager = _FakeModbusManager()
        fake_manager.read_device_pn_result = (
            "nonexistent-hash-value",
            "PNXXX",
            "0xFF",
        )
        coordinator.modbus_manager = fake_manager

        # Act
        result = await coordinator._get_config_file_path()

        # Assert
        assert result == ""


class TestHandleConnectionFailure:
    """_handle_connection_failure() error-state transition and backoff tiering."""

    async def test_marks_disconnected_and_clears_latest_data(self, coordinator) -> None:
        # Arrange
        coordinator.modbus_manager = _FakeModbusManager()
        coordinator._latest_data = {"a": 1}

        # Act
        await coordinator._handle_connection_failure("test failure")

        # Assert
        assert coordinator._connection_failed is True
        assert coordinator._status == "disconnected"
        assert coordinator._latest_data == {}

    async def test_force_disconnects_the_modbus_connection(self, coordinator) -> None:
        # Arrange
        fake_manager = _FakeModbusManager()
        coordinator.modbus_manager = fake_manager

        # Act
        await coordinator._handle_connection_failure("test failure")

        # Assert
        assert fake_manager.force_disconnect_called is True

    async def test_logs_unavailable_only_once_across_repeated_failures(
        self, coordinator
    ) -> None:
        # Arrange
        coordinator.modbus_manager = _FakeModbusManager()
        assert coordinator._device_unavailable_logged is False

        # Act
        await coordinator._handle_connection_failure("first failure")
        first_logged = coordinator._device_unavailable_logged
        await coordinator._handle_connection_failure("second failure")

        # Assert: flag flips to True after the first call and stays True.
        assert first_logged is True
        assert coordinator._device_unavailable_logged is True
        assert coordinator._consecutive_failures == 2

    @pytest.mark.parametrize(
        ("failure_count", "expected_interval"),
        [(1, 10), (3, 10), (4, 30), (10, 30), (11, 60), (30, 60), (31, 300)],
    )
    async def test_backoff_interval_escalates_with_consecutive_failures(
        self, coordinator, failure_count: int, expected_interval: int
    ) -> None:
        # Arrange
        coordinator.modbus_manager = _FakeModbusManager()
        coordinator._consecutive_failures = failure_count - 1

        # Act
        await coordinator._handle_connection_failure("failure")

        # Assert
        assert coordinator._connection_retry_interval == expected_interval

    async def test_force_disconnect_exception_is_swallowed(self, coordinator) -> None:
        # Arrange
        class _ExplodingManager(_FakeModbusManager):
            async def force_disconnect(self) -> None:
                raise RuntimeError("boom")

        coordinator.modbus_manager = _ExplodingManager()

        # Act & Assert: must not raise despite the manager failing.
        await coordinator._handle_connection_failure("failure")
        assert coordinator._connection_failed is True


class TestMaybeMdnsLookup:
    """_maybe_mdns_lookup() throttled mDNS re-discovery after repeated failures."""

    async def test_below_failure_threshold_does_nothing(self, coordinator) -> None:
        # Arrange
        coordinator._consecutive_failures = 2
        coordinator._last_mdns_lookup = 0

        # Act
        await coordinator._maybe_mdns_lookup()

        # Assert: lookup timestamp untouched -- the gate returned early.
        assert coordinator._last_mdns_lookup == 0

    async def test_rate_limited_within_55_seconds_does_nothing(self, coordinator) -> None:
        # Arrange
        import time

        coordinator._consecutive_failures = 5
        coordinator._last_mdns_lookup = time.time()

        # Act
        await coordinator._maybe_mdns_lookup()

        # Assert: no new lookup attempt (would have updated the timestamp
        # to a later value if it had proceeded past the rate-limit gate).
        assert time.time() - coordinator._last_mdns_lookup < 1

    async def test_no_stored_sn_does_nothing_after_updating_timestamp(
        self, coordinator
    ) -> None:
        # Arrange: fixture's entry already has unique_id="127.0.0.1" (an IP,
        # not a SN), so _get_stored_sn() returns "" without any changes needed.
        coordinator._consecutive_failures = 5
        coordinator._last_mdns_lookup = 0

        # Act
        await coordinator._maybe_mdns_lookup()

        # Assert: the rate-limit timestamp is still updated even though no
        # SN was available (matches the source's actual early-return order).
        assert coordinator._last_mdns_lookup > 0

    async def test_mdns_resolves_new_ip_and_applies_it(
        self, coordinator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: unique_id must go through async_update_entry -- HA's
        # ConfigEntry blocks direct attribute assignment for tracked fields.
        coordinator._consecutive_failures = 5
        coordinator._last_mdns_lookup = 0
        coordinator.hass.config_entries.async_update_entry(
            coordinator.entry, unique_id="ABCDEFGHIJ1234"
        )
        fake_manager = _FakeModbusManager()
        coordinator.modbus_manager = fake_manager

        async def _fake_find(hass, sn, timeout=5):
            return "192.168.9.9"

        monkeypatch.setattr(
            "custom_components.anker_solix_official.coordinator.find_device_ip_by_sn",
            _fake_find,
        )

        # Act
        await coordinator._maybe_mdns_lookup()

        # Assert
        assert coordinator.ip_address == "192.168.9.9"
        assert fake_manager.update_ip_address_calls == ["192.168.9.9"]
        assert coordinator._consecutive_failures == 0

    async def test_mdns_resolving_same_ip_is_a_no_op(
        self, coordinator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        coordinator._consecutive_failures = 5
        coordinator._last_mdns_lookup = 0
        coordinator.hass.config_entries.async_update_entry(
            coordinator.entry, unique_id="ABCDEFGHIJ1234"
        )
        original_ip = coordinator.ip_address

        async def _fake_find(hass, sn, timeout=5):
            return original_ip

        monkeypatch.setattr(
            "custom_components.anker_solix_official.coordinator.find_device_ip_by_sn",
            _fake_find,
        )

        # Act
        await coordinator._maybe_mdns_lookup()

        # Assert: consecutive_failures is untouched since no IP change applied.
        assert coordinator._consecutive_failures == 5


class TestAutoSetModeOnConnect:
    """_auto_set_mode_on_connect() first-connect operating-mode write."""

    async def test_no_full_config_cache_is_a_no_op(self, coordinator) -> None:
        # Arrange
        coordinator._full_config_cache = None

        # Act & Assert: must not raise.
        await coordinator._auto_set_mode_on_connect({})

    async def test_no_auto_mode_configured_is_a_no_op(self, coordinator) -> None:
        # Arrange
        coordinator._full_config_cache = {"product_info": {}}

        # Act & Assert
        await coordinator._auto_set_mode_on_connect({})

    async def test_already_in_target_mode_skips_write_and_persists_flag(
        self, coordinator
    ) -> None:
        # Arrange
        coordinator._full_config_cache = {
            "product_info": {"auto_mode_on_connect": 1},
            "write_quantities": {
                "enumeration_selection": {
                    "operating_mode": {"address": 100, "data_type": "UINT16"}
                }
            },
        }
        fake_manager = _FakeModbusManager()
        coordinator.modbus_manager = fake_manager
        data = {"operating_mode": 1}

        # Act
        await coordinator._auto_set_mode_on_connect(data)

        # Assert
        assert coordinator._initial_mode_sent is True

    async def test_successful_write_updates_data_and_sets_protection(
        self, coordinator
    ) -> None:
        # Arrange
        coordinator._full_config_cache = {
            "product_info": {"auto_mode_on_connect": 2},
            "write_quantities": {
                "enumeration_selection": {
                    "operating_mode": {
                        "address": 100,
                        "data_type": "UINT16",
                        "options": {"2": "self_use"},
                    }
                }
            },
        }
        fake_manager = _FakeModbusManager()
        fake_manager.write_register_result = WriteResult(success=True)
        coordinator.modbus_manager = fake_manager
        data = {"operating_mode": 0}

        # Act
        await coordinator._auto_set_mode_on_connect(data)

        # Assert
        assert data["operating_mode"] == 2
        assert coordinator._initial_mode_sent is True
        is_protected, value = coordinator.get_protected_value("operating_mode")
        assert is_protected is True
        assert value == "self_use"

    async def test_failed_write_does_not_persist_flag(self, coordinator) -> None:
        # Arrange
        coordinator._full_config_cache = {
            "product_info": {"auto_mode_on_connect": 2},
            "write_quantities": {
                "enumeration_selection": {
                    "operating_mode": {"address": 100, "data_type": "UINT16"}
                }
            },
        }
        fake_manager = _FakeModbusManager()
        fake_manager.write_register_result = WriteResult(success=False)
        coordinator.modbus_manager = fake_manager
        data = {"operating_mode": 0}

        # Act
        await coordinator._auto_set_mode_on_connect(data)

        # Assert
        assert coordinator._initial_mode_sent is False

    async def test_no_operating_mode_config_is_a_no_op(self, coordinator) -> None:
        # Arrange: auto_mode configured but no matching enumeration_selection.
        coordinator._full_config_cache = {
            "product_info": {"auto_mode_on_connect": 2},
            "write_quantities": {"enumeration_selection": {}},
        }

        # Act & Assert
        await coordinator._auto_set_mode_on_connect({})
        assert coordinator._initial_mode_sent is False

    async def test_exception_is_caught_and_swallowed(self, coordinator) -> None:
        # Arrange: malformed config (address is not int-convertible).
        coordinator._full_config_cache = {
            "product_info": {"auto_mode_on_connect": 2},
            "write_quantities": {
                "enumeration_selection": {
                    "operating_mode": {"address": "not-a-number", "data_type": "UINT16"}
                }
            },
        }

        # Act & Assert: must not raise.
        await coordinator._auto_set_mode_on_connect({})


class TestOverrideModelWithProductName:
    """_override_model_with_product_name() sensor/device_info name override."""

    def test_no_full_config_cache_is_a_no_op(self, coordinator) -> None:
        coordinator._full_config_cache = None
        data = {"device_model": "raw_pn"}

        coordinator._override_model_with_product_name(data)

        assert data["device_model"] == "raw_pn"

    def test_no_product_info_is_a_no_op(self, coordinator) -> None:
        coordinator._full_config_cache = {}
        data = {"device_model": "raw_pn"}

        coordinator._override_model_with_product_name(data)

        assert data["device_model"] == "raw_pn"

    def test_overrides_model_using_sn_lookup(self, coordinator) -> None:
        # Arrange
        coordinator._full_config_cache = {
            "product_info": {
                "sn_register_key": "device_sn",
                "model_register_key": "device_model",
                "default_name": "Unknown Device",
                "product_code_mapping": {"DMWH": "Solarbank Max"},
            }
        }
        data = {"device_sn": "123DMWH4567890123", "device_model": "raw"}

        # Act
        coordinator._override_model_with_product_name(data)

        # Assert
        assert data["device_model"] == "Solarbank Max"
        assert coordinator.device_info["model"] == "Solarbank Max"

    def test_no_sn_register_key_falls_back_to_default_name(self, coordinator) -> None:
        # Arrange
        coordinator._full_config_cache = {
            "product_info": {
                "model_register_key": "device_model",
                "default_name": "Generic Device",
            }
        }
        data = {"device_model": "raw"}

        # Act
        coordinator._override_model_with_product_name(data)

        # Assert
        assert data["device_model"] == "Generic Device"

    def test_exception_is_caught_and_logged(self, coordinator) -> None:
        # Arrange: data is not a dict, triggering the isinstance guard's
        # "false" branch cleanly (no exception expected, but confirms the
        # guard prevents a crash on malformed input).
        coordinator._full_config_cache = {"product_info": {"default_name": "X"}}

        # Act & Assert: must not raise.
        coordinator._override_model_with_product_name("not-a-dict")


class TestInjectVersionGates:
    """_inject_version_gates() feature-visibility computation from version registers."""

    def test_no_device_config_cache_is_a_no_op(self, coordinator) -> None:
        coordinator._device_config_cache = None
        data = {}
        coordinator._inject_version_gates(data)
        assert data == {}

    def test_single_gate_dict_format_passes_when_version_meets_minimum(
        self, coordinator
    ) -> None:
        # Arrange
        coordinator._device_config_cache = {
            "backup_reserve": {
                "version_gate": {"entity": "firmware_version", "min_version": "0.0.5.0"}
            }
        }
        data = {"firmware_version": "0.0.5.5"}

        # Act
        coordinator._inject_version_gates(data)

        # Assert
        assert data["backup_reserve_visible"] == 1

    def test_single_gate_fails_when_version_below_minimum(self, coordinator) -> None:
        # Arrange
        coordinator._device_config_cache = {
            "backup_reserve": {
                "version_gate": {"entity": "firmware_version", "min_version": "0.0.6.0"}
            }
        }
        data = {"firmware_version": "0.0.5.5"}

        # Act
        coordinator._inject_version_gates(data)

        # Assert
        assert data["backup_reserve_visible"] == 0

    def test_multiple_gates_list_format_requires_all_to_pass(self, coordinator) -> None:
        # Arrange: firmware passes, hardware does not -> AND logic fails overall.
        coordinator._device_config_cache = {
            "feature_x": {
                "version_gate": [
                    {"entity": "firmware_version", "min_version": "0.0.1.0"},
                    {"entity": "hardware_version", "min_version": "0.0.9.0"},
                ]
            }
        }
        data = {"firmware_version": "0.0.5.0", "hardware_version": "0.0.1.0"}

        # Act
        coordinator._inject_version_gates(data)

        # Assert
        assert data["feature_x_visible"] == 0

    def test_missing_version_register_fails_the_gate(self, coordinator) -> None:
        coordinator._device_config_cache = {
            "feature_x": {
                "version_gate": {"entity": "firmware_version", "min_version": "0.0.1.0"}
            }
        }
        data = {}  # firmware_version register never read

        coordinator._inject_version_gates(data)

        assert data["feature_x_visible"] == 0

    def test_no_version_gate_configured_does_not_add_visible_key(
        self, coordinator
    ) -> None:
        coordinator._device_config_cache = {"power": {"address": 100}}
        data = {}

        coordinator._inject_version_gates(data)

        assert "power_visible" not in data

    def test_invalid_version_gate_type_is_skipped(self, coordinator) -> None:
        # Arrange: version_gate is neither dict nor list.
        coordinator._device_config_cache = {"feature_x": {"version_gate": "invalid"}}
        data = {}

        coordinator._inject_version_gates(data)

        assert "feature_x_visible" not in data

    def test_gate_missing_entity_or_min_version_fails(self, coordinator) -> None:
        coordinator._device_config_cache = {
            "feature_x": {"version_gate": {"entity": "firmware_version"}}
        }
        data = {"firmware_version": "0.0.5.0"}

        coordinator._inject_version_gates(data)

        assert data["feature_x_visible"] == 0

    def test_exception_is_caught_and_logged(self, coordinator) -> None:
        # Arrange: data is not a dict, must not crash.
        coordinator._device_config_cache = {"feature_x": {"version_gate": {}}}
        coordinator._inject_version_gates("not-a-dict")  # must not raise


class TestEnsureConfigReady:
    """ensure_config_ready() synchronous-path config load for platform setup."""

    async def test_already_valid_cache_returns_it_directly(self, coordinator) -> None:
        coordinator._config_cache_valid = True
        coordinator._device_config_cache = {"a": {"address": 1}}
        coordinator._batch_ranges_cache = []

        result = await coordinator.ensure_config_ready()

        assert result == {"a": {"address": 1}}

    async def test_no_client_available_returns_empty_dict(self, coordinator) -> None:
        # Arrange
        class _NoClientManager(_FakeModbusManager):
            async def get_client(self):
                return None

        coordinator.modbus_manager = _NoClientManager()
        coordinator._config_cache_valid = False

        # Act
        result = await coordinator.ensure_config_ready()

        # Assert
        assert result == {}

    async def test_manager_exception_returns_empty_dict(self, coordinator) -> None:
        class _ExplodingManager(_FakeModbusManager):
            async def get_client(self):
                raise RuntimeError("boom")

        coordinator.modbus_manager = _ExplodingManager()
        coordinator._config_cache_valid = False

        result = await coordinator.ensure_config_ready()

        assert result == {}


class TestGetDeviceDataPoints:
    """get_device_data_points() public accessor for platform setup."""

    async def test_returns_cached_data_points_when_valid(self, coordinator) -> None:
        coordinator._config_cache_valid = True
        coordinator._device_config_cache = {"a": {"address": 1}}
        coordinator._batch_ranges_cache = []

        result = await coordinator.get_device_data_points()

        assert result == {"a": {"address": 1}}

    async def test_returns_empty_dict_when_cache_invalid(self, coordinator) -> None:
        coordinator._config_cache_valid = False

        result = await coordinator.get_device_data_points()

        assert result == {}


class TestAsyncUpdateData:
    """_async_update_data() DataUpdateCoordinator hook returning cached data."""

    async def test_returns_latest_data_snapshot(self, coordinator) -> None:
        coordinator._latest_data = {"power": 100}

        result = await coordinator._async_update_data()

        assert result == {"power": 100}


class TestAsyncWaitForFirstData:
    """async_wait_for_first_data() bounded wait used during platform setup."""

    async def test_returns_immediately_if_data_already_present(self, coordinator) -> None:
        coordinator._latest_data = {"a": 1}
        await coordinator.async_wait_for_first_data(timeout=5.0)  # must not block

    async def test_times_out_when_data_never_arrives(self, coordinator) -> None:
        coordinator._latest_data = {}
        await coordinator.async_wait_for_first_data(timeout=0.05)
        # Assert: returns without raising once the deadline passes.
        assert coordinator._latest_data == {}


class TestLogDataUpdate:
    """_log_data_update() diagnostic logging, exercised for side-effect safety."""

    def test_initial_phase_with_no_prior_data_does_not_raise(self, coordinator) -> None:
        coordinator._log_data_update("initial", {"a": 1, "b": 2}, None)

    def test_periodic_phase_with_changed_values_does_not_raise(self, coordinator) -> None:
        coordinator._log_data_update("periodic", {"a": 2}, {"a": 1})

    def test_empty_data_does_not_raise(self, coordinator) -> None:
        coordinator._log_data_update("initial", {}, None)

    def test_unchanged_data_does_not_raise(self, coordinator) -> None:
        coordinator._log_data_update("periodic", {"a": 1}, {"a": 1})


class TestPersistInitialModeSent:
    """_persist_initial_mode_sent() idempotent options-write."""

    def test_first_call_sets_flag_and_updates_entry_options(self, coordinator) -> None:
        assert coordinator._initial_mode_sent is False

        coordinator._persist_initial_mode_sent()

        assert coordinator._initial_mode_sent is True
        assert coordinator.entry.options.get("initial_mode_sent") is True

    def test_second_call_is_a_no_op(self, coordinator) -> None:
        coordinator._persist_initial_mode_sent()
        coordinator._persist_initial_mode_sent()  # must not raise or double-update

        assert coordinator._initial_mode_sent is True


class TestApplyMdnsIpUpdate:
    """_apply_mdns_ip_update() in-memory connection-target switch."""

    async def test_updates_ip_address_and_delegates_to_manager(
        self, coordinator
    ) -> None:
        fake_manager = _FakeModbusManager()
        coordinator.modbus_manager = fake_manager

        await coordinator._apply_mdns_ip_update("10.0.0.50")

        assert coordinator.ip_address == "10.0.0.50"
        assert fake_manager.update_ip_address_calls == ["10.0.0.50"]

    async def test_rebuilds_device_logger_with_new_ip(self, coordinator) -> None:
        coordinator.modbus_manager = _FakeModbusManager()

        await coordinator._apply_mdns_ip_update("10.0.0.51")

        assert coordinator.device_logger.device_info["ip"] == "10.0.0.51"


class TestAsyncStartupMdnsCheck:
    """_async_startup_mdns_check() fire-and-forget IP re-discovery at startup."""

    async def test_no_stored_sn_is_a_no_op(self, coordinator) -> None:
        coordinator._initial_mdns_sn = None
        await coordinator._async_startup_mdns_check()  # must not raise

    async def test_already_connected_by_the_time_mdns_resolves_is_ignored(
        self, coordinator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: mDNS resolves a different IP, but the initial connection
        # already succeeded via the configured IP -- must not switch away
        # from a working connection.
        coordinator._initial_mdns_sn = "ABCDEFGHIJ1234"
        coordinator._ever_connected = True
        original_ip = coordinator.ip_address

        async def _fake_find(hass, sn, timeout=5):
            return "10.0.0.99"

        monkeypatch.setattr(
            "custom_components.anker_solix_official.coordinator.find_device_ip_by_sn",
            _fake_find,
        )

        # Act
        await coordinator._async_startup_mdns_check()

        # Assert
        assert coordinator.ip_address == original_ip

    async def test_applies_resolved_ip_when_not_yet_connected(
        self, coordinator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        coordinator._initial_mdns_sn = "ABCDEFGHIJ1234"
        coordinator._ever_connected = False
        coordinator.modbus_manager = _FakeModbusManager()

        async def _fake_find(hass, sn, timeout=5):
            return "10.0.0.77"

        monkeypatch.setattr(
            "custom_components.anker_solix_official.coordinator.find_device_ip_by_sn",
            _fake_find,
        )

        # Act
        await coordinator._async_startup_mdns_check()

        # Assert
        assert coordinator.ip_address == "10.0.0.77"


class TestUpdateUnavailableRegisters:
    """_update_unavailable_registers() syncs failed/successful register sets."""

    async def test_no_client_is_a_no_op(self, coordinator) -> None:
        class _NoClientManager(_FakeModbusManager):
            async def get_client(self):
                return None

        coordinator.modbus_manager = _NoClientManager()

        await coordinator._update_unavailable_registers()  # must not raise

    async def test_new_failures_are_added_to_unavailable_set(self, coordinator) -> None:
        # Arrange
        class _ClientWithFailures:
            def get_last_failed_registers(self):
                return {100, 200}

            def get_last_successful_registers(self):
                return set()

        class _Manager(_FakeModbusManager):
            async def get_client(self):
                return _ClientWithFailures()

        coordinator.modbus_manager = _Manager()

        # Act
        await coordinator._update_unavailable_registers()

        # Assert
        assert {100, 200}.issubset(coordinator._unavailable_registers)

    async def test_recovered_registers_are_removed_from_unavailable_set(
        self, coordinator
    ) -> None:
        # Arrange
        coordinator._unavailable_registers = {100, 200}

        class _ClientRecovered:
            def get_last_failed_registers(self):
                return set()

            def get_last_successful_registers(self):
                return {100}

        class _Manager(_FakeModbusManager):
            async def get_client(self):
                return _ClientRecovered()

        coordinator.modbus_manager = _Manager()

        # Act
        await coordinator._update_unavailable_registers()

        # Assert
        assert 100 not in coordinator._unavailable_registers
        assert 200 in coordinator._unavailable_registers

    async def test_client_without_the_expected_methods_is_skipped(
        self, coordinator
    ) -> None:
        # Arrange: a client object lacking get_last_failed_registers entirely.
        class _BareClient:
            pass

        class _Manager(_FakeModbusManager):
            async def get_client(self):
                return _BareClient()

        coordinator.modbus_manager = _Manager()

        # Act & Assert: must not raise.
        await coordinator._update_unavailable_registers()

    async def test_manager_exception_is_caught(self, coordinator) -> None:
        class _ExplodingManager(_FakeModbusManager):
            async def get_client(self):
                raise RuntimeError("boom")

        coordinator.modbus_manager = _ExplodingManager()

        await coordinator._update_unavailable_registers()  # must not raise


class TestUpdateDeviceRegistryInfo:
    """_update_device_registry_info() version-safe device registry sync.

    Regression coverage for the deprecated `device_registry.async_get_device`
    call (see PR #119 / issue #115): `async_get_device_by_identifier` requires
    two positional args (`identifier`, `config_entry_id`) and only exists on
    HA core >= 2026.8.0, so the coordinator must probe for it via `hasattr`
    rather than calling it unconditionally.
    """

    async def test_uses_new_api_when_available_with_both_required_args(
        self, hass, coordinator
    ) -> None:
        from homeassistant.helpers import device_registry as dr

        dev_reg = dr.async_get(hass)
        device = dev_reg.async_get_or_create(
            config_entry_id=coordinator.entry.entry_id,
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            manufacturer="Anker",
            model="--",
            name="Old Name",
        )
        coordinator.device_info["manufacturer"] = "Anker"
        coordinator.device_info["model"] = "Solarbank Max AC"
        coordinator.device_info["name"] = "New Name"

        calls: list[tuple] = []
        original = dev_reg.async_get_device_by_identifier if hasattr(
            dev_reg, "async_get_device_by_identifier"
        ) else None

        def fake_get_device_by_identifier(identifier, config_entry_id):
            calls.append((identifier, config_entry_id))
            return device

        dev_reg.async_get_device_by_identifier = fake_get_device_by_identifier
        try:
            coordinator._update_device_registry_info()
        finally:
            if original is None:
                del dev_reg.async_get_device_by_identifier
            else:
                dev_reg.async_get_device_by_identifier = original

        # Both required positional args must be passed - this is exactly the
        # bug in PR #119, which only passed `identifier`.
        assert calls == [
            ((DOMAIN, coordinator.entry.entry_id), coordinator.entry.entry_id)
        ]
        updated = dev_reg.async_get_device(
            identifiers={(DOMAIN, coordinator.entry.entry_id)}
        )
        assert updated.model == "Solarbank Max AC"
        assert updated.name == "New Name"

    async def test_falls_back_to_deprecated_api_when_new_api_absent(
        self, hass, coordinator
    ) -> None:
        """Simulates HA core < 2026.8.0, where the new method doesn't exist."""
        from homeassistant.helpers import device_registry as dr

        dev_reg = dr.async_get(hass)
        device = dev_reg.async_get_or_create(
            config_entry_id=coordinator.entry.entry_id,
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            manufacturer="Anker",
            model="--",
            name="Old Name",
        )
        coordinator.device_info["manufacturer"] = "Anker"
        coordinator.device_info["model"] = "Solarbank Max AC"
        coordinator.device_info["name"] = "New Name"

        # Current pytest-homeassistant-custom-component's DeviceRegistry has
        # no async_get_device_by_identifier at all, so this is already the
        # fallback path by default - just assert it works end to end.
        assert not hasattr(dev_reg, "async_get_device_by_identifier")

        coordinator._update_device_registry_info()  # must not raise

        updated = dev_reg.async_get_device(
            identifiers={(DOMAIN, coordinator.entry.entry_id)}
        )
        assert updated.model == "Solarbank Max AC"
        assert updated.name == "New Name"

    async def test_no_op_when_device_not_registered(self, hass, coordinator) -> None:
        # No device_registry entry exists for this entry_id - must not raise.
        coordinator.device_info["model"] = "Solarbank Max AC"

        coordinator._update_device_registry_info()  # must not raise


class TestAsyncShutdown:
    """async_shutdown() full teardown sequence."""

    async def test_disconnects_modbus_and_stops_background_loop(self, coordinator) -> None:
        fake_manager = _FakeModbusManager()
        coordinator.modbus_manager = fake_manager

        await coordinator.async_shutdown()

        assert fake_manager.disconnect_called is True
        assert coordinator._stop_bg is True
