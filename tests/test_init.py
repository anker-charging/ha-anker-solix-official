"""Unit tests for the anker_solix_official integration's __init__.py.

AnkerSolixOfficialCoordinator itself is replaced with a fake so these tests
exercise only async_setup_entry/async_unload_entry's own orchestration logic
(hass.data bookkeeping, platform forward/unload calls) without depending on
real Modbus I/O or the entity platforms' own setup logic.
"""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.anker_solix_official import (
    async_setup_entry,
    async_unload_entry,
    _async_update_listener,
)
from custom_components.anker_solix_official.const import DOMAIN


class _FakeCoordinator:
    """Stand-in for AnkerSolixOfficialCoordinator."""

    instances: list["_FakeCoordinator"] = []

    def __init__(self, hass, entry) -> None:
        self.hass = hass
        self.entry = entry
        self.set_updated_data_calls: list[dict] = []
        self.wait_for_first_data_called = False
        self.shutdown_called = False
        _FakeCoordinator.instances.append(self)

    def async_set_updated_data(self, data: dict) -> None:
        self.set_updated_data_calls.append(data)

    async def async_wait_for_first_data(self) -> None:
        self.wait_for_first_data_called = True

    async def async_shutdown(self) -> None:
        self.shutdown_called = True


@pytest.fixture(autouse=True)
def _reset_fake_coordinator_instances():
    _FakeCoordinator.instances = []
    yield
    _FakeCoordinator.instances = []


@pytest.fixture
def entry(hass) -> MockConfigEntry:
    e = MockConfigEntry(
        domain=DOMAIN,
        data={"device_name": "Test Device", "ip_address": "127.0.0.1", "port": 502},
        unique_id="127.0.0.1",
    )
    e.add_to_hass(hass)
    return e


class TestAsyncSetupEntry:
    """async_setup_entry() coordinator creation and hass.data bookkeeping."""

    async def test_creates_coordinator_and_stores_it_in_hass_data(
        self, hass, entry: MockConfigEntry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        monkeypatch.setattr(
            "custom_components.anker_solix_official.AnkerSolixOfficialCoordinator",
            _FakeCoordinator,
        )

        async def _fake_forward(entry, platforms):
            return True

        monkeypatch.setattr(
            hass.config_entries, "async_forward_entry_setups", _fake_forward
        )

        # Act
        result = await async_setup_entry(hass, entry)

        # Assert
        assert result is True
        assert hass.data[DOMAIN][entry.entry_id] is _FakeCoordinator.instances[0]

    async def test_calls_async_set_updated_data_with_empty_dict_first(
        self, hass, entry: MockConfigEntry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: this seeds entities as "unavailable" rather than "unknown"
        # from the very first frame, before the background loop has data.
        monkeypatch.setattr(
            "custom_components.anker_solix_official.AnkerSolixOfficialCoordinator",
            _FakeCoordinator,
        )

        async def _fake_forward(entry, platforms):
            return True

        monkeypatch.setattr(
            hass.config_entries, "async_forward_entry_setups", _fake_forward
        )

        # Act
        await async_setup_entry(hass, entry)

        # Assert
        coord = _FakeCoordinator.instances[0]
        assert coord.set_updated_data_calls == [{}]
        assert coord.wait_for_first_data_called is True

    async def test_forwards_setup_to_all_four_platforms(
        self, hass, entry: MockConfigEntry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        monkeypatch.setattr(
            "custom_components.anker_solix_official.AnkerSolixOfficialCoordinator",
            _FakeCoordinator,
        )
        forwarded_platforms: list[str] = []

        async def _fake_forward(entry, platforms):
            forwarded_platforms.extend(platforms)
            return True

        monkeypatch.setattr(
            hass.config_entries, "async_forward_entry_setups", _fake_forward
        )

        # Act
        await async_setup_entry(hass, entry)

        # Assert
        assert forwarded_platforms == ["sensor", "select", "number", "switch"]

    async def test_second_entry_reuses_existing_domain_data_dict(
        self, hass, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: two config entries (two physical devices) must coexist
        # under the same hass.data[DOMAIN] dict, keyed by entry_id.
        monkeypatch.setattr(
            "custom_components.anker_solix_official.AnkerSolixOfficialCoordinator",
            _FakeCoordinator,
        )

        async def _fake_forward(entry, platforms):
            return True

        monkeypatch.setattr(
            hass.config_entries, "async_forward_entry_setups", _fake_forward
        )

        entry1 = MockConfigEntry(
            domain=DOMAIN, data={"ip_address": "10.0.0.1"}, unique_id="10.0.0.1"
        )
        entry1.add_to_hass(hass)
        entry2 = MockConfigEntry(
            domain=DOMAIN, data={"ip_address": "10.0.0.2"}, unique_id="10.0.0.2"
        )
        entry2.add_to_hass(hass)

        # Act
        await async_setup_entry(hass, entry1)
        await async_setup_entry(hass, entry2)

        # Assert
        assert len(hass.data[DOMAIN]) == 2
        assert entry1.entry_id in hass.data[DOMAIN]
        assert entry2.entry_id in hass.data[DOMAIN]


class TestAsyncUnloadEntry:
    """async_unload_entry() platform teardown and coordinator shutdown."""

    async def test_successful_unload_shuts_down_coordinator_and_pops_from_data(
        self, hass, entry: MockConfigEntry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        fake_coord = _FakeCoordinator(hass, entry)
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = fake_coord

        async def _fake_unload(entry, platforms):
            return True

        monkeypatch.setattr(
            hass.config_entries, "async_unload_platforms", _fake_unload
        )

        # Act
        result = await async_unload_entry(hass, entry)

        # Assert
        assert result is True
        assert fake_coord.shutdown_called is True
        assert entry.entry_id not in hass.data[DOMAIN]

    async def test_failed_platform_unload_leaves_coordinator_in_data(
        self, hass, entry: MockConfigEntry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: platform unload fails -- coordinator must NOT be torn
        # down or removed, since entities are still using it.
        fake_coord = _FakeCoordinator(hass, entry)
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = fake_coord

        async def _fake_unload(entry, platforms):
            return False

        monkeypatch.setattr(
            hass.config_entries, "async_unload_platforms", _fake_unload
        )

        # Act
        result = await async_unload_entry(hass, entry)

        # Assert
        assert result is False
        assert fake_coord.shutdown_called is False
        assert entry.entry_id in hass.data[DOMAIN]

    async def test_unloads_all_four_platforms(
        self, hass, entry: MockConfigEntry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        fake_coord = _FakeCoordinator(hass, entry)
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = fake_coord
        unloaded_platforms: list[str] = []

        async def _fake_unload(entry, platforms):
            unloaded_platforms.extend(platforms)
            return True

        monkeypatch.setattr(
            hass.config_entries, "async_unload_platforms", _fake_unload
        )

        # Act
        await async_unload_entry(hass, entry)

        # Assert
        assert unloaded_platforms == ["sensor", "select", "number", "switch"]


class TestAsyncUpdateListener:
    """_async_update_listener() config-change reload trigger."""

    async def test_triggers_a_reload_of_the_entry(
        self, hass, entry: MockConfigEntry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        reload_calls: list[str] = []

        async def _fake_reload(entry_id):
            reload_calls.append(entry_id)

        monkeypatch.setattr(hass.config_entries, "async_reload", _fake_reload)

        # Act
        await _async_update_listener(hass, entry)

        # Assert
        assert reload_calls == [entry.entry_id]
