"""Unit tests for mdns_helper.find_device_ip_by_sn.

`_discover_devices_sync` and `async_get_instance` are monkeypatched at the
mdns_helper module level (and the homeassistant.components.zeroconf module
respectively) so no real mDNS traffic or asyncio executor thread is needed.
"""

from __future__ import annotations

import pytest

from custom_components.anker_solix_official import mdns_helper


class TestFindDeviceIpBySn:
    """find_device_ip_by_sn() short-circuits, delegation, and error handling."""

    async def test_empty_sn_returns_none_without_touching_zeroconf(self) -> None:
        result = await mdns_helper.find_device_ip_by_sn(hass=None, sn="")
        assert result is None

    async def test_short_sn_returns_none_without_touching_zeroconf(self) -> None:
        # Arrange: gate is `len(sn) < 8`.
        result = await mdns_helper.find_device_ip_by_sn(hass=None, sn="1234567")
        assert result is None

    async def test_matching_device_returns_its_ip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        monkeypatch.setattr(
            "homeassistant.components.zeroconf.async_get_instance",
            _async_stub(object()),
        )
        monkeypatch.setattr(
            mdns_helper,
            "_discover_devices_sync",
            lambda zc, timeout: [
                {"sn": "OTHERSN01", "ip": "192.168.1.5"},
                {"sn": "TARGETSN1", "ip": "192.168.1.9"},
            ],
        )

        # Act
        result = await mdns_helper.find_device_ip_by_sn(hass=None, sn="TARGETSN1")

        # Assert
        assert result == "192.168.1.9"

    async def test_no_matching_device_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        monkeypatch.setattr(
            "homeassistant.components.zeroconf.async_get_instance",
            _async_stub(object()),
        )
        monkeypatch.setattr(
            mdns_helper,
            "_discover_devices_sync",
            lambda zc, timeout: [{"sn": "OTHERSN01", "ip": "192.168.1.5"}],
        )

        # Act
        result = await mdns_helper.find_device_ip_by_sn(hass=None, sn="NOTFOUND1")

        # Assert
        assert result is None

    async def test_matching_device_with_empty_ip_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: matches by SN but has no resolvable IP -- must not return
        # an empty string as if it were a valid address.
        monkeypatch.setattr(
            "homeassistant.components.zeroconf.async_get_instance",
            _async_stub(object()),
        )
        monkeypatch.setattr(
            mdns_helper,
            "_discover_devices_sync",
            lambda zc, timeout: [{"sn": "TARGETSN1", "ip": ""}],
        )

        # Act
        result = await mdns_helper.find_device_ip_by_sn(hass=None, sn="TARGETSN1")

        # Assert
        assert result is None

    async def test_async_get_instance_failure_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        async def _raise(hass: object) -> None:
            raise RuntimeError("zeroconf unavailable")

        monkeypatch.setattr(
            "homeassistant.components.zeroconf.async_get_instance", _raise
        )

        # Act
        result = await mdns_helper.find_device_ip_by_sn(hass=None, sn="TARGETSN1")

        # Assert
        assert result is None

    async def test_discovery_exception_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        monkeypatch.setattr(
            "homeassistant.components.zeroconf.async_get_instance",
            _async_stub(object()),
        )

        def _raise(zc: object, timeout: int) -> list:
            raise OSError("mDNS socket error")

        monkeypatch.setattr(mdns_helper, "_discover_devices_sync", _raise)

        # Act
        result = await mdns_helper.find_device_ip_by_sn(hass=None, sn="TARGETSN1")

        # Assert
        assert result is None


def _async_stub(return_value: object):
    """Build an async callable returning a fixed value, for monkeypatching
    async_get_instance without depending on unittest.mock."""

    async def _stub(hass: object) -> object:
        return return_value

    return _stub


class TestDiscoverDevicesSyncListener:
    """_discover_devices_sync()'s inner _Listener device-record assembly.

    Exercised indirectly through a real zeroconf.Zeroconf-shaped fake so the
    dedup-by-SN and MAC-extraction logic run against the actual listener
    class defined inside the function, without opening a real mDNS socket.
    """

    def test_mac_extracted_from_anker_device_server_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: bypass real mDNS entirely by replacing zeroconf.ServiceBrowser
        # with a fake that immediately invokes add_service() once, driving the
        # listener class defined inside _discover_devices_sync directly.
        class _FakeInfo:
            server = "Anker-Device_AA11BB22CC33.local."
            port = 502

            def parsed_addresses(self) -> list[str]:
                return ["10.0.0.42"]

            @property
            def decoded_properties(self) -> dict:
                return {"sn": "SN00000001", "pn": "PN01"}

        class _FakeZeroconf:
            def get_service_info(self, type_, name):
                return _FakeInfo()

        import zeroconf as _zc

        def _instant_browser(zc, service_type, listener):
            listener.add_service(zc, service_type, "any-name")

            class _FakeBrowser:
                def cancel(self) -> None:
                    return None

            return _FakeBrowser()

        monkeypatch.setattr(_zc, "ServiceBrowser", _instant_browser)
        monkeypatch.setattr(mdns_helper.time, "sleep", lambda _t: None)

        # Act
        devices = mdns_helper._discover_devices_sync(_FakeZeroconf(), timeout=0)

        # Assert: real zeroconf ServiceInfo.server values are fully-qualified
        # with a trailing dot (e.g. "...local."), so `.replace(".local", "")`
        # leaves that dot behind -- this is the source's actual current
        # behavior (a latent formatting quirk, not something to silently
        # "fix" here), so the test locks down what it really produces today.
        assert len(devices) == 1
        assert devices[0]["mac"] == "AA11BB22CC33."
        assert devices[0]["sn"] == "SN00000001"
        assert devices[0]["ip"] == "10.0.0.42"

    def test_update_service_replaces_prior_entry_for_same_sn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: two add_service calls (simulating a TTL refresh) for the
        # same SN must not produce duplicate entries.
        call_count = {"n": 0}

        class _FakeInfo:
            server = "some-other-server.local."
            port = 502

            def parsed_addresses(self) -> list[str]:
                call_count["n"] += 1
                return [f"10.0.0.{call_count['n']}"]

            @property
            def decoded_properties(self) -> dict:
                return {"sn": "SAMESN001", "pn": "PN01"}

        class _FakeZeroconf:
            def get_service_info(self, type_, name):
                return _FakeInfo()

        import zeroconf as _zc

        def _double_update_browser(zc, service_type, listener):
            listener.add_service(zc, service_type, "any-name")
            listener.update_service(zc, service_type, "any-name")

            class _FakeBrowser:
                def cancel(self) -> None:
                    return None

            return _FakeBrowser()

        monkeypatch.setattr(_zc, "ServiceBrowser", _double_update_browser)
        monkeypatch.setattr(mdns_helper.time, "sleep", lambda _t: None)

        # Act
        devices = mdns_helper._discover_devices_sync(_FakeZeroconf(), timeout=0)

        # Assert: only one entry survives despite two add/update calls.
        assert len(devices) == 1
        assert devices[0]["sn"] == "SAMESN001"

    def test_none_service_info_is_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange: get_service_info() returning None (info expired/unavailable)
        # must not raise or add a device.
        class _FakeZeroconf:
            def get_service_info(self, type_, name):
                return None

        import zeroconf as _zc

        def _browser(zc, service_type, listener):
            listener.add_service(zc, service_type, "any-name")

            class _FakeBrowser:
                def cancel(self) -> None:
                    return None

            return _FakeBrowser()

        monkeypatch.setattr(_zc, "ServiceBrowser", _browser)
        monkeypatch.setattr(mdns_helper.time, "sleep", lambda _t: None)

        # Act
        devices = mdns_helper._discover_devices_sync(_FakeZeroconf(), timeout=0)

        # Assert
        assert devices == []
