"""Unit tests for the anker_solix_official integration's __init__.py.

AnkerSolixOfficialCoordinator itself is replaced with a fake so these tests
exercise only async_setup_entry/async_unload_entry's own orchestration logic
(hass.data bookkeeping, platform forward/unload calls) without depending on
real Modbus I/O or the entity platforms' own setup logic.
"""

from __future__ import annotations

import pytest
from homeassistant.exceptions import ConfigEntryNotReady
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.anker_solix_official import (
    async_setup_entry,
    async_unload_entry,
)
from custom_components.anker_solix_official.const import DOMAIN


class _FakeCoordinator:
    """Stand-in for AnkerSolixOfficialCoordinator."""

    instances: list["_FakeCoordinator"] = []

    def __init__(self, hass, entry) -> None:
        self.hass = hass
        self.entry = entry
        self.first_refresh_called = False
        self.shutdown_called = False
        _FakeCoordinator.instances.append(self)

    async def async_config_entry_first_refresh(self) -> None:
        self.first_refresh_called = True

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

    async def test_awaits_first_refresh_before_forwarding_platforms(
        self, hass, entry: MockConfigEntry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: the first refresh loads the device config and raises
        # ConfigEntryNotReady when the device is unreachable, so it must
        # complete before any platform is forwarded.
        monkeypatch.setattr(
            "custom_components.anker_solix_official.AnkerSolixOfficialCoordinator",
            _FakeCoordinator,
        )
        call_order: list[str] = []

        async def _fake_forward(entry, platforms):
            call_order.append("forward")
            return True

        monkeypatch.setattr(
            hass.config_entries, "async_forward_entry_setups", _fake_forward
        )

        original_refresh = _FakeCoordinator.async_config_entry_first_refresh

        async def _tracked_refresh(self):
            call_order.append("first_refresh")
            await original_refresh(self)

        monkeypatch.setattr(
            _FakeCoordinator, "async_config_entry_first_refresh", _tracked_refresh
        )

        # Act
        await async_setup_entry(hass, entry)

        # Assert
        coord = _FakeCoordinator.instances[0]
        assert coord.first_refresh_called is True
        assert call_order == ["first_refresh", "forward"]

    async def test_registers_no_update_listener(
        self, hass, entry: MockConfigEntry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A blanket "reload on any entry change" listener would make the
        # coordinator's own title/options writes reload the integration
        # mid-setup, which is what produced the duplicate entities in #117.
        monkeypatch.setattr(
            "custom_components.anker_solix_official.AnkerSolixOfficialCoordinator",
            _FakeCoordinator,
        )

        async def _fake_forward(entry, platforms):
            return True

        monkeypatch.setattr(
            hass.config_entries, "async_forward_entry_setups", _fake_forward
        )

        await async_setup_entry(hass, entry)

        assert entry.update_listeners == []

    async def test_failed_first_refresh_shuts_down_and_does_not_store_coordinator(
        self, hass, entry: MockConfigEntry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A coordinator that opened a Modbus connection while trying to set up
        # must be torn down when the first refresh fails, and must not be left
        # in hass.data where a later unload or a retry would trip over it.
        monkeypatch.setattr(
            "custom_components.anker_solix_official.AnkerSolixOfficialCoordinator",
            _FakeCoordinator,
        )

        async def _failing_refresh(self):
            self.first_refresh_called = True
            raise ConfigEntryNotReady

        monkeypatch.setattr(
            _FakeCoordinator, "async_config_entry_first_refresh", _failing_refresh
        )

        forwarded: list[str] = []

        async def _fake_forward(entry, platforms):
            forwarded.extend(platforms)
            return True

        monkeypatch.setattr(
            hass.config_entries, "async_forward_entry_setups", _fake_forward
        )

        with pytest.raises(ConfigEntryNotReady):
            await async_setup_entry(hass, entry)

        coord = _FakeCoordinator.instances[0]
        assert coord.shutdown_called is True
        assert entry.entry_id not in hass.data.get(DOMAIN, {})
        assert forwarded == []

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

    async def test_repeated_unload_after_coordinator_already_popped(
        self, hass, entry: MockConfigEntry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # HA retries unload after a FAILED_UNLOAD, by which point the
        # coordinator may already be gone; that must not raise KeyError.
        hass.data.setdefault(DOMAIN, {})

        async def _fake_unload(entry, platforms):
            return True

        monkeypatch.setattr(
            hass.config_entries, "async_unload_platforms", _fake_unload
        )

        result = await async_unload_entry(hass, entry)

        assert result is True
