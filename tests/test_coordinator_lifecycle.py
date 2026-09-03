"""Lifecycle invariants for AnkerSolixOfficialCoordinator (issue #117 regressions).

These began as characterization tests that pinned the old hand-rolled background
loop's behaviour, including four defects. The loop is gone; the coordinator now
does its I/O in `_async_update_data` and lets
DataUpdateCoordinator own retry timing and availability.

Each defect that was previously pinned as a KNOWN BUG is now asserted to be
impossible:

* a shut-down coordinator cannot mutate Home Assistant state
* availability cannot claim "connected" once polling has stopped
* a blocking disconnect cannot leave the coordinator still polling
* no custom retry ladder shadows Home Assistant's own backoff
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from custom_components.anker_solix_official.const import (
    DOMAIN,
    EMPTY_FRAME_TOLERANCE,
)
from custom_components.anker_solix_official.coordinator import (
    AnkerSolixOfficialCoordinator,
)
from custom_components.anker_solix_official.device_logger import WriteResult
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

MINIMAL_CFG: dict[str, Any] = {
    "product_info": {"default_name": "Test Product"},
    "read_quantities": {"battery_soc": {"address": 100, "data_type": "UINT16"}},
}


class _ScriptedModbusManager:
    """Modbus manager stand-in with per-test scripted results and a call log."""

    def __init__(self) -> None:
        self.client_available = True
        self.all_data: dict[str, Any] = {"battery_soc": 55}
        self.read_device_pn_result = ("testpn", "PN001", "0xAB")
        self.calls: list[str] = []
        self.disconnect_calls = 0
        self.force_disconnect_calls = 0
        self.update_ip_calls: list[str] = []
        self.disconnect_blocks_forever = False

    async def get_client(self):
        self.calls.append("get_client")
        return object() if self.client_available else None

    async def get_all_data(self, data_points, batch_ranges=None, **_kw) -> dict:
        self.calls.append("get_all_data")
        return dict(self.all_data)

    async def read_device_pn(self):
        return self.read_device_pn_result

    async def write_register(self, address, value, data_type) -> WriteResult:
        self.calls.append(f"write:{address}={value}")
        return WriteResult(success=True)

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.calls.append("disconnect")
        if self.disconnect_blocks_forever:
            await asyncio.Event().wait()

    async def force_disconnect(self) -> None:
        self.force_disconnect_calls += 1
        self.calls.append("force_disconnect")

    async def update_ip_address(self, new_ip: str) -> None:
        self.update_ip_calls.append(new_ip)

    def initialize(self, *_a, **_kw) -> None:
        pass


@pytest.fixture
async def coord(hass):
    """Coordinator with a scripted modbus manager and no real device I/O."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"device_name": "Test Device", "ip_address": "127.0.0.1", "port": 502},
        unique_id="TESTSN123",
    )
    entry.add_to_hass(hass)
    c = AnkerSolixOfficialCoordinator(hass, entry)
    c.modbus_manager = _ScriptedModbusManager()
    yield c
    c.modbus_manager.disconnect_blocks_forever = False
    try:
        await asyncio.wait_for(c.async_shutdown(), timeout=2)
    except (TimeoutError, asyncio.CancelledError, Exception):
        pass


def _prime_config_cache(c) -> None:
    """Mark the device-config cache valid so refreshes skip PN/config loading."""
    c._device_config_cache = {"battery_soc": {"address": 100, "data_type": "UINT16"}}
    c._batch_ranges_cache = []
    c._config_cache_valid = True
    c._full_config_cache = MINIMAL_CFG


class TestNoBackgroundLoopIsSpawned:
    """Construction must not start any polling of its own."""

    async def test_construction_performs_no_device_io(self, coord) -> None:
        await asyncio.sleep(0)

        assert coord.modbus_manager.calls == []

    async def test_no_background_loop_attributes_remain(self, coord) -> None:
        for attr in ("_bg_task", "_stop_bg", "_status", "_resource_manager"):
            assert not hasattr(coord, attr), f"{attr} should have been removed"


class TestShutdownGate:
    """A shut-down coordinator must not be able to mutate HA state.

    DataUpdateCoordinator.async_shutdown sets `_shutdown_requested`, and
    `_async_refresh` returns immediately when it is set. This is the invariant the
    old hand-rolled loop bypassed, which let a torn-down coordinator keep
    publishing and wake up stale listeners (issue #117).
    """

    async def test_refresh_after_shutdown_performs_no_io(self, coord) -> None:
        _prime_config_cache(coord)

        await coord.async_shutdown()
        coord.modbus_manager.calls.clear()
        await coord.async_refresh()

        assert coord.modbus_manager.calls == []
        assert coord._latest_data == {}

    async def test_refresh_after_shutdown_notifies_no_listeners(self, coord) -> None:
        _prime_config_cache(coord)
        notified: list[int] = []
        coord.async_add_listener(lambda: notified.append(1))

        await coord.async_shutdown()
        notified.clear()
        await coord.async_refresh()

        assert notified == []

    async def test_shutdown_requested_is_set(self, coord) -> None:
        await coord.async_shutdown()

        assert coord._shutdown_requested is True


class TestShutdownOrdering:
    """Polling must be stopped before the socket is closed.

    The old order disconnected first, and disconnect blocks on the manager's I/O
    lock, so a long in-flight read could stall unload for minutes while the loop
    stayed alive (issue #117 defect C).
    """

    async def test_polling_is_stopped_even_when_disconnect_hangs(self, coord) -> None:
        coord.modbus_manager.disconnect_blocks_forever = True

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(coord.async_shutdown(), timeout=0.3)

        # Fixed ordering: the refresh gate is already closed despite the hang.
        assert coord._shutdown_requested is True
        assert coord.modbus_manager.disconnect_calls == 1

    async def test_refresh_is_refused_while_disconnect_hangs(self, coord) -> None:
        _prime_config_cache(coord)
        coord.modbus_manager.disconnect_blocks_forever = True

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(coord.async_shutdown(), timeout=0.3)

        coord.modbus_manager.calls.clear()
        await coord.async_refresh()

        assert coord.modbus_manager.calls == []


class TestAvailabilityCannotLie:
    """Availability is derived from the refresh outcome, never from a flag.

    Previously `_status` was set to "connected" and then frozen when the loop was
    cancelled, so entities reported themselves available for hours while serving
    stale values (issue #117, second reporter).
    """

    async def test_successful_refresh_reports_connected(self, coord) -> None:
        _prime_config_cache(coord)

        await coord.async_refresh()

        assert coord.last_update_success is True
        assert coord.is_connected() is True

    async def test_failed_refresh_reports_disconnected(self, coord) -> None:
        _prime_config_cache(coord)
        coord.modbus_manager.client_available = False

        await coord.async_refresh()

        assert coord.last_update_success is False
        assert coord.is_connected() is False

    async def test_cannot_stay_connected_after_a_failure(self, coord) -> None:
        _prime_config_cache(coord)
        await coord.async_refresh()
        assert coord.is_connected() is True

        coord.modbus_manager.client_available = False
        await coord.async_refresh()

        assert coord.is_connected() is False


class TestNoCustomBackoffLadder:
    """HA owns retry scheduling; the 10/30/60/300 ladder was removed."""

    async def test_no_retry_interval_state_is_kept(self, coord) -> None:
        for attr in (
            "_connection_retry_interval",
            "_last_connection_attempt",
            "_device_unavailable_logged",
            "_connection_failed",
        ):
            assert not hasattr(coord, attr), f"{attr} should have been removed"

    async def test_update_interval_is_left_at_the_scan_interval(self, coord) -> None:
        _prime_config_cache(coord)
        original = coord.update_interval
        coord.modbus_manager.client_available = False

        await coord.async_refresh()
        await coord.async_refresh()

        assert coord.update_interval == original


class TestFailureBookkeeping:
    async def test_failures_are_counted_for_the_mdns_trigger(self, coord) -> None:
        _prime_config_cache(coord)
        coord.modbus_manager.client_available = False

        await coord.async_refresh()
        await coord.async_refresh()

        assert coord._consecutive_failures == 2

    async def test_recovery_resets_the_counter(self, coord) -> None:
        _prime_config_cache(coord)
        coord.modbus_manager.client_available = False
        await coord.async_refresh()

        coord.modbus_manager.client_available = True
        await coord.async_refresh()

        assert coord._consecutive_failures == 0

    async def test_failure_drops_the_socket(self, coord) -> None:
        _prime_config_cache(coord)
        coord.modbus_manager.client_available = False

        await coord.async_refresh()

        assert coord.modbus_manager.force_disconnect_calls == 1

    async def test_failure_clears_coordinator_data_not_just_latest_data(
        self, coord
    ) -> None:
        # DataUpdateCoordinator leaves `coordinator.data` at its last successful
        # value when `_async_update_data` raises. Code that reads
        # `coordinator.data` directly instead of checking `last_update_success`
        # first -- e.g. base_entity's write-condition gate -- would otherwise see
        # stale pre-outage values during an outage even though `available` is
        # already False.
        _prime_config_cache(coord)
        await coord.async_refresh()
        assert coord.data == {"battery_soc": 55}

        coord.modbus_manager.client_available = False
        await coord.async_refresh()

        assert coord.data == {}
        assert coord.last_update_success is False


class TestMdnsTriggerConditions:
    async def test_no_lookup_below_three_failures(self, coord, monkeypatch) -> None:
        looked_up: list[str] = []

        async def _fake(hass, sn, **_kw):
            looked_up.append(sn)
            return None

        monkeypatch.setattr(
            "custom_components.anker_solix_official.coordinator.find_device_ip_by_sn",
            _fake,
        )
        coord._consecutive_failures = 2

        await coord._maybe_mdns_lookup()

        assert looked_up == []

    async def test_rate_limited_within_55s(self, coord, monkeypatch) -> None:
        looked_up: list[str] = []

        async def _fake(hass, sn, **_kw):
            looked_up.append(sn)
            return None

        monkeypatch.setattr(
            "custom_components.anker_solix_official.coordinator.find_device_ip_by_sn",
            _fake,
        )
        coord._consecutive_failures = 5
        coord._last_mdns_lookup = time.time()

        await coord._maybe_mdns_lookup()

        assert looked_up == []

    async def test_ip_update_is_not_persisted_to_entry_data(self, coord) -> None:
        original_data = dict(coord.entry.data)

        await coord._apply_mdns_ip_update("192.168.1.99")

        assert coord.ip_address == "192.168.1.99"
        assert coord.modbus_manager.update_ip_calls == ["192.168.1.99"]
        assert dict(coord.entry.data) == original_data


class TestInitialModePersistence:
    """Writes to entry.options must not reload the integration.

    No update listener is registered any more, so the coordinator persisting its
    own state cannot restart the setup it is running inside (issue #117 defect A).
    """

    async def test_persist_writes_entry_options_once(self, coord) -> None:
        assert coord._initial_mode_sent is False

        coord._persist_initial_mode_sent()
        await asyncio.sleep(0)

        assert coord._initial_mode_sent is True
        assert coord.entry.options.get("initial_mode_sent") is True

    async def test_persist_is_idempotent(self, coord) -> None:
        coord._persist_initial_mode_sent()
        await asyncio.sleep(0)
        first = dict(coord.entry.options)

        coord._persist_initial_mode_sent()
        await asyncio.sleep(0)

        assert dict(coord.entry.options) == first

    async def test_options_write_registers_no_reload(self, coord) -> None:
        coord._persist_initial_mode_sent()
        await asyncio.sleep(0)

        assert coord.entry.update_listeners == []


class TestEmptyFrameTolerance:
    """A dropped Modbus frame must not flap every entity to unavailable.

    availability is now driven by last_update_success, so failing on the very
    first empty frame would take ~110 entities unavailable and back within one
    5s scan interval. The pre-refactor loop tolerated a couple of misses on the
    periodic path and that behaviour is preserved here.
    """

    async def test_single_empty_frame_keeps_the_last_good_values(
        self, coord
    ) -> None:
        _prime_config_cache(coord)
        await coord.async_refresh()
        assert coord.last_update_success is True

        coord.modbus_manager.all_data = {}
        await coord.async_refresh()

        assert coord.last_update_success is True
        assert coord.data["battery_soc"] == 55

    async def test_persistent_empty_frames_eventually_fail(self, coord) -> None:
        _prime_config_cache(coord)
        await coord.async_refresh()
        coord.modbus_manager.all_data = {}

        for _ in range(EMPTY_FRAME_TOLERANCE + 1):
            await coord.async_refresh()

        assert coord.last_update_success is False
        assert coord.is_connected() is False

    async def test_tolerance_counter_resets_after_a_good_frame(self, coord) -> None:
        _prime_config_cache(coord)
        await coord.async_refresh()

        coord.modbus_manager.all_data = {}
        await coord.async_refresh()
        assert coord._empty_frames == 1

        coord.modbus_manager.all_data = {"battery_soc": 61}
        await coord.async_refresh()

        assert coord._empty_frames == 0
        assert coord.data["battery_soc"] == 61

    async def test_tolerated_empty_frames_still_count_toward_mdns_trigger(
        self, coord
    ) -> None:
        # _consecutive_failures gates mDNS re-discovery (>= 3). If tolerated
        # empty frames did not advance it, a device that starts returning empty
        # frames while still reachable would delay mDNS re-discovery by the
        # whole tolerance window on top of the normal 3-failure gate.
        _prime_config_cache(coord)
        await coord.async_refresh()

        coord.modbus_manager.all_data = {}
        for _ in range(EMPTY_FRAME_TOLERANCE):
            await coord.async_refresh()

        assert coord._consecutive_failures == EMPTY_FRAME_TOLERANCE

    async def test_recovery_resets_consecutive_failures_too(self, coord) -> None:
        _prime_config_cache(coord)
        await coord.async_refresh()

        coord.modbus_manager.all_data = {}
        await coord.async_refresh()
        assert coord._consecutive_failures == 1

        coord.modbus_manager.all_data = {"battery_soc": 61}
        await coord.async_refresh()

        assert coord._consecutive_failures == 0

    async def test_empty_first_ever_frame_fails_immediately(self, coord) -> None:
        # Nothing to fall back on before a first successful read, so the entry
        # must fail setup rather than come up with no data.
        _prime_config_cache(coord)
        coord.modbus_manager.all_data = {}

        with pytest.raises(UpdateFailed):
            await coord._async_update_data()


class TestFirstRefreshContract:
    """Connect/config failures must surface as UpdateFailed for HA to translate."""

    async def test_unreachable_device_raises_update_failed(self, coord) -> None:
        coord.modbus_manager.client_available = False

        with pytest.raises(UpdateFailed):
            await coord._async_connect_and_ensure_config()

    async def test_successful_setup_populates_the_config_cache(
        self, coord, monkeypatch
    ) -> None:
        async def _path():
            return "config/testpn.yaml"

        async def _load(_file):
            return MINIMAL_CFG

        monkeypatch.setattr(coord, "_get_config_file_path", _path)
        monkeypatch.setattr(
            coord.device_config, "load_device_config_by_file_async", _load
        )

        await coord._async_connect_and_ensure_config()

        assert coord._is_config_cache_valid() is True

    async def test_working_ip_is_not_delayed_by_an_mdns_scan(
        self, coord, monkeypatch
    ) -> None:
        # A reachable configured IP must never pay for an mDNS lookup; the scan is
        # a fallback for a moved device, not a precondition for connecting.
        scans: list[str] = []

        async def _fake(hass, sn, **_kw):
            scans.append(sn)
            return None

        monkeypatch.setattr(
            "custom_components.anker_solix_official.coordinator.find_device_ip_by_sn",
            _fake,
        )

        async def _path():
            return "config/testpn.yaml"

        async def _load(_file):
            return MINIMAL_CFG

        monkeypatch.setattr(coord, "_get_config_file_path", _path)
        monkeypatch.setattr(
            coord.device_config, "load_device_config_by_file_async", _load
        )

        await coord._async_connect_and_ensure_config()

        assert scans == []

    async def test_unreachable_ip_falls_back_to_an_mdns_scan(
        self, coord, monkeypatch
    ) -> None:
        scans: list[str] = []

        async def _fake(hass, sn, **_kw):
            scans.append(sn)
            return None

        monkeypatch.setattr(
            "custom_components.anker_solix_official.coordinator.find_device_ip_by_sn",
            _fake,
        )
        coord._initial_mdns_sn = "TESTSN123"
        coord.modbus_manager.client_available = False

        with pytest.raises(UpdateFailed):
            await coord._async_connect_and_ensure_config()

        assert scans == ["TESTSN123"]
