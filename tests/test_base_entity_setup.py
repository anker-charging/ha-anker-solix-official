"""Behaviour tests for `async_setup_entities_with_retry`.

These began as characterization tests pinning the deferred entity-add mechanism
(issue #117 defect D). That mechanism has since been deleted: the coordinator
loads the device configuration during its first refresh, before
`async_config_entry_first_refresh` returns and therefore before platforms are
forwarded, so the configuration is always present by the time this helper runs.

The assertions that previously recorded the escape hatch now assert it cannot
exist: no coordinator listener is registered, and nothing can add entities to a
torn-down platform after unload.
"""

from __future__ import annotations

import asyncio
from typing import Any

from custom_components.anker_solix_official.base_entity import (
    async_setup_entities_with_retry,
)


class _StubCoordinator:
    """Coordinator stub exposing only what the setup helper touches."""

    def __init__(self, hass, data_points: dict[str, Any] | None = None) -> None:
        self.hass = hass
        self.ip_address = "127.0.0.1"
        self._data_points: dict[str, Any] = data_points or {}
        self.listeners: list[Any] = []

    async def get_device_data_points(self) -> dict[str, Any]:
        return dict(self._data_points)

    def async_add_listener(self, listener):
        self.listeners.append(listener)

        def _remove() -> None:
            if listener in self.listeners:
                self.listeners.remove(listener)

        return _remove

    def publish(self) -> None:
        for listener in list(self.listeners):
            listener()


def _factory(_coordinator, key, config):
    return {"key": key, "config": config}


def _accept_all(_key, _config) -> bool:
    return True


class TestConfigAvailable:
    async def test_adds_entities_immediately(self, hass) -> None:
        coordinator = _StubCoordinator(hass, {"battery_soc": {"address": 100}})
        added: list[list] = []

        await async_setup_entities_with_retry(
            hass,
            coordinator,
            lambda e: added.append(list(e)),
            _accept_all,
            _factory,
            "sensor",
        )

        assert len(added) == 1
        assert added[0][0]["key"] == "battery_soc"

    async def test_registers_no_coordinator_listener(self, hass) -> None:
        coordinator = _StubCoordinator(hass, {"battery_soc": {"address": 100}})

        await async_setup_entities_with_retry(
            hass, coordinator, lambda e: None, _accept_all, _factory, "sensor"
        )

        assert coordinator.listeners == []


class TestConfigMissing:
    """A missing config now means "no entities", not "retry later".

    The first refresh raises UpdateFailed -> ConfigEntryNotReady when the config
    cannot be loaded, so platforms are never forwarded in that case. Reaching
    this helper without a config therefore means something is genuinely wrong,
    and it must fail loudly and inertly rather than arming a deferred add.
    """

    async def test_adds_nothing(self, hass) -> None:
        coordinator = _StubCoordinator(hass, {})
        added: list[list] = []

        await async_setup_entities_with_retry(
            hass,
            coordinator,
            lambda e: added.append(list(e)),
            _accept_all,
            _factory,
            "sensor",
        )

        assert added == []

    async def test_registers_no_listener_so_nothing_survives_unload(
        self, hass
    ) -> None:
        coordinator = _StubCoordinator(hass, {})

        await async_setup_entities_with_retry(
            hass, coordinator, lambda e: None, _accept_all, _factory, "sensor"
        )

        # The deleted deferred path registered a listener here that outlived
        # unload and could add entities to a dead platform (issue #117).
        assert coordinator.listeners == []

    async def test_later_data_publishes_cannot_add_entities(self, hass) -> None:
        coordinator = _StubCoordinator(hass, {})
        added: list[list] = []
        await async_setup_entities_with_retry(
            hass,
            coordinator,
            lambda e: added.append(list(e)),
            _accept_all,
            _factory,
            "sensor",
        )

        coordinator._data_points = {"battery_soc": {"address": 100}}
        coordinator.publish()
        await asyncio.sleep(0)

        assert added == []

    async def test_stale_callback_is_never_invoked_after_unload(self, hass) -> None:
        coordinator = _StubCoordinator(hass, {})
        calls: list[str] = []

        def _stale_add_entities(_entities) -> None:
            calls.append("called-after-unload")

        await async_setup_entities_with_retry(
            hass, coordinator, _stale_add_entities, _accept_all, _factory, "sensor"
        )

        coordinator._data_points = {"battery_soc": {"address": 100}}
        coordinator.publish()
        await asyncio.sleep(0)

        assert calls == []


class TestEntityFilterIsHonoured:
    async def test_filtered_out_keys_produce_no_entities(self, hass) -> None:
        coordinator = _StubCoordinator(
            hass, {"battery_soc": {"address": 100}, "skip_me": {"address": 200}}
        )
        added: list[list] = []

        await async_setup_entities_with_retry(
            hass,
            coordinator,
            lambda e: added.append(list(e)),
            lambda key, _cfg: key != "skip_me",
            _factory,
            "sensor",
        )

        assert [e["key"] for e in added[0]] == ["battery_soc"]

    async def test_no_matching_keys_adds_nothing(self, hass) -> None:
        coordinator = _StubCoordinator(hass, {"skip_me": {"address": 200}})
        added: list[list] = []

        await async_setup_entities_with_retry(
            hass,
            coordinator,
            lambda e: added.append(list(e)),
            lambda _key, _cfg: False,
            _factory,
            "sensor",
        )

        assert added == []
        assert coordinator.listeners == []
