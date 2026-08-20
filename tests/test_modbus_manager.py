"""Unit tests for ModbusConnectionManager (modbus_manager.py).

Uses a lightweight fake AnkerSolixModbusClient (mirroring the one in
test_modbus_client.py) so the manager's lock/lifecycle/timeout logic can be
exercised without any real Modbus client or TCP socket.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from custom_components.anker_solix_official.device_logger import WriteResult
from custom_components.anker_solix_official.modbus_manager import (
    ModbusConnectionManager,
)


@dataclass
class _FakeClient:
    """Stand-in for AnkerSolixModbusClient, scripted per test."""

    connected: bool = False
    connect_results: list[bool] = field(default_factory=lambda: [True])
    read_register_result: Any = None
    read_device_pn_result: tuple[str, str, str] = ("", "", "")
    write_register_result: WriteResult = field(
        default_factory=lambda: WriteResult(success=True)
    )
    get_all_data_result: dict = field(default_factory=dict)
    disconnect_called: bool = False
    connect_call_count: int = 0

    def is_connected(self) -> bool:
        return self.connected

    async def connect(self) -> bool:
        self.connect_call_count += 1
        result = (
            self.connect_results.pop(0) if self.connect_results else True
        )
        self.connected = result
        return result

    async def disconnect(self) -> None:
        self.disconnect_called = True
        self.connected = False

    async def read_register(self, address: int, data_type: str, count: int = None) -> Any:
        return self.read_register_result

    async def read_device_pn(self) -> tuple[str, str, str]:
        return self.read_device_pn_result

    async def write_register(self, address: int, value: Any, data_type: str) -> WriteResult:
        return self.write_register_result

    async def get_all_data(self, data_points, batch_ranges, use_batch_optimization) -> dict:
        return self.get_all_data_result

    def get_connection_info(self) -> dict:
        return {"connected": self.connected}


def _initialized_manager(client: _FakeClient | None = None) -> ModbusConnectionManager:
    """Build a manager already initialize()'d, with its client pre-injected."""
    manager = ModbusConnectionManager()
    manager.initialize("127.0.0.1", 502, "TestDevice")
    manager._client = client or _FakeClient(connected=True)
    return manager


class TestInitialize:
    """initialize() basic state setup."""

    def test_sets_initialized_flag_and_lock(self) -> None:
        manager = ModbusConnectionManager()
        manager.initialize("192.168.1.1", 502)
        assert manager._is_initialized is True
        assert manager._io_lock is not None

    def test_default_device_name_derived_from_ip_and_port(self) -> None:
        manager = ModbusConnectionManager()
        manager.initialize("192.168.1.1", 502)
        assert manager._device_name == "192.168.1.1:502"

    def test_explicit_device_name_is_preserved(self) -> None:
        manager = ModbusConnectionManager()
        manager.initialize("192.168.1.1", 502, device_name="My Device")
        assert manager._device_name == "My Device"


class TestGetClient:
    """get_client() gating on initialization and connection state."""

    async def test_uninitialized_manager_returns_none(self) -> None:
        manager = ModbusConnectionManager()
        result = await manager.get_client()
        assert result is None

    async def test_connected_client_is_returned_directly(self) -> None:
        manager = _initialized_manager(_FakeClient(connected=True))
        result = await manager.get_client()
        assert result is manager._client

    async def test_disconnected_client_attempts_reconnect(self) -> None:
        # Arrange
        fake = _FakeClient(connected=False, connect_results=[True])
        manager = _initialized_manager(fake)

        # Act
        result = await manager.get_client()

        # Assert
        assert result is fake
        assert fake.connected is True

    async def test_failed_reconnect_returns_none(self) -> None:
        fake = _FakeClient(connected=False, connect_results=[False])
        manager = _initialized_manager(fake)

        result = await manager.get_client()

        assert result is None


class TestEnsureConnectedLocked:
    """_ensure_connected_locked() client creation and connected_event signaling."""

    async def test_creates_client_lazily_when_none(self) -> None:
        # Arrange
        manager = ModbusConnectionManager()
        manager.initialize("127.0.0.1", 502)
        assert manager._client is None

        # Act: real AnkerSolixModbusClient.connect() will fail against an
        # unreachable address (pytest-socket blocks real sockets anyway),
        # but the client object itself must exist afterwards.
        await manager._ensure_connected_locked()

        # Assert
        assert manager._client is not None

    async def test_connection_exception_is_caught_and_returns_none(self) -> None:
        # Arrange
        class _ExplodingClient(_FakeClient):
            async def connect(self) -> bool:
                raise RuntimeError("boom")

        manager = _initialized_manager(_ExplodingClient(connected=False))

        # Act
        result = await manager._ensure_connected_locked()

        # Assert
        assert result is None

    async def test_already_connected_sets_connected_event(self) -> None:
        # Arrange
        manager = _initialized_manager(_FakeClient(connected=True))

        # Act
        await manager._ensure_connected_locked()

        # Assert
        assert manager._connected_event.is_set() is True


class TestWaitForConnectionReady:
    """_wait_for_connection_ready() grace-period wait for issue #83."""

    async def test_already_connected_returns_true_immediately(self) -> None:
        manager = _initialized_manager(_FakeClient(connected=True))
        result = await manager._wait_for_connection_ready(timeout=0.1)
        assert result is True

    async def test_times_out_when_never_signaled(self) -> None:
        # Arrange: client not connected, and nothing ever sets connected_event.
        manager = _initialized_manager(_FakeClient(connected=False))

        # Act
        result = await manager._wait_for_connection_ready(timeout=0.05)

        # Assert
        assert result is False

    async def test_returns_true_once_connected_event_is_set(self) -> None:
        # Arrange
        manager = _initialized_manager(_FakeClient(connected=False))

        async def _signal_soon() -> None:
            await asyncio.sleep(0.01)
            manager._connected_event.set()

        # Act
        task = asyncio.create_task(_signal_soon())
        result = await manager._wait_for_connection_ready(timeout=1.0)
        await task

        # Assert
        assert result is True


class TestReadRegister:
    """read_register() lock-wrapped delegation to the underlying client."""

    async def test_uninitialized_manager_returns_none(self) -> None:
        manager = ModbusConnectionManager()
        result = await manager.read_register(100, "UINT16")
        assert result is None

    async def test_delegates_to_client_and_updates_activity(self) -> None:
        # Arrange
        fake = _FakeClient(connected=True, read_register_result=42)
        manager = _initialized_manager(fake)

        # Act
        result = await manager.read_register(100, "UINT16")

        # Assert
        assert result == 42
        assert manager._last_activity > 0

    async def test_no_client_available_returns_none(self) -> None:
        # Arrange: connection cannot be established.
        fake = _FakeClient(connected=False, connect_results=[False])
        manager = _initialized_manager(fake)

        # Act
        result = await manager.read_register(100, "UINT16")

        # Assert
        assert result is None

    async def test_client_raising_is_caught_and_returns_none(self) -> None:
        # Arrange
        class _ExplodingClient(_FakeClient):
            async def read_register(self, address, data_type, count=None):
                raise RuntimeError("boom")

        manager = _initialized_manager(_ExplodingClient(connected=True))

        # Act
        result = await manager.read_register(100, "UINT16")

        # Assert
        assert result is None


class TestReadDevicePn:
    """read_device_pn() lock-wrapped delegation."""

    async def test_uninitialized_manager_returns_empty_tuple(self) -> None:
        manager = ModbusConnectionManager()
        result = await manager.read_device_pn()
        assert result == ("", "", "")

    async def test_delegates_to_client(self) -> None:
        fake = _FakeClient(
            connected=True, read_device_pn_result=("hash123", "PN001", "0x1234")
        )
        manager = _initialized_manager(fake)

        result = await manager.read_device_pn()

        assert result == ("hash123", "PN001", "0x1234")

    async def test_no_client_available_returns_empty_tuple(self) -> None:
        fake = _FakeClient(connected=False, connect_results=[False])
        manager = _initialized_manager(fake)

        result = await manager.read_device_pn()

        assert result == ("", "", "")


class TestWriteRegister:
    """write_register() lock-acquisition timeout and delegation."""

    async def test_successful_write_delegates_to_client(self) -> None:
        # Arrange
        expected = WriteResult(success=True)
        fake = _FakeClient(connected=True, write_register_result=expected)
        manager = _initialized_manager(fake)

        # Act
        result = await manager.write_register(100, 1, "UINT16")

        # Assert
        assert result.success is True

    async def test_lock_timeout_returns_transient_failure(self) -> None:
        # Arrange: hold the lock externally so write_register() cannot
        # acquire it within the tiny lock_timeout given.
        fake = _FakeClient(connected=True)
        manager = _initialized_manager(fake)
        await manager._io_lock.acquire()

        try:
            # Act
            result = await manager.write_register(
                100, 1, "UINT16", lock_timeout=0.05
            )

            # Assert
            assert result.success is False
            assert result.is_transient is True
            assert "busy" in result.error_reason.lower()
        finally:
            manager._io_lock.release()

    async def test_no_client_available_returns_failure(self) -> None:
        # Arrange: small lock_timeout keeps the internal connection-ready
        # grace wait (min(5.0, lock_timeout)) short instead of the 5s default.
        fake = _FakeClient(connected=False, connect_results=[False])
        manager = _initialized_manager(fake)

        result = await manager.write_register(100, 1, "UINT16", lock_timeout=0.05)

        assert result.success is False
        assert result.is_transient is True

    async def test_write_timeout_returns_transient_failure(self) -> None:
        # Arrange: client.write_register() never completes within `timeout`.
        class _SlowClient(_FakeClient):
            async def write_register(self, address, value, data_type):
                await asyncio.sleep(10)
                return WriteResult(success=True)

        manager = _initialized_manager(_SlowClient(connected=True))

        # Act
        result = await manager.write_register(100, 1, "UINT16", timeout=0.05)

        # Assert
        assert result.success is False
        assert result.is_transient is True
        assert "timeout" in result.error_reason.lower()

    async def test_client_exception_returns_failure_with_type_name(self) -> None:
        # Arrange
        class _ExplodingClient(_FakeClient):
            async def write_register(self, address, value, data_type):
                raise ValueError("bad value")

        manager = _initialized_manager(_ExplodingClient(connected=True))

        # Act
        result = await manager.write_register(100, 1, "UINT16")

        # Assert
        assert result.success is False
        assert "ValueError" in result.error_reason

    async def test_creates_lock_lazily_if_missing(self) -> None:
        # Arrange: simulate a manager whose lock was cleared (e.g. after a
        # prior disconnect()) but write_register() is still called.
        fake = _FakeClient(connected=True)
        manager = _initialized_manager(fake)
        manager._io_lock = None

        # Act
        result = await manager.write_register(100, 1, "UINT16")

        # Assert
        assert result.success is True
        assert manager._io_lock is not None


class TestGetAllData:
    """get_all_data() lock-wrapped delegation."""

    async def test_uninitialized_manager_returns_empty_dict(self) -> None:
        manager = ModbusConnectionManager()
        result = await manager.get_all_data({})
        assert result == {}

    async def test_delegates_to_client_and_returns_result(self) -> None:
        fake = _FakeClient(connected=True, get_all_data_result={"a": 1})
        manager = _initialized_manager(fake)

        result = await manager.get_all_data({"a": {"address": 1}})

        assert result == {"a": 1}

    async def test_no_client_available_returns_empty_dict(self) -> None:
        fake = _FakeClient(connected=False, connect_results=[False])
        manager = _initialized_manager(fake)

        result = await manager.get_all_data({"a": {"address": 1}})

        assert result == {}

    async def test_client_exception_returns_empty_dict(self) -> None:
        class _ExplodingClient(_FakeClient):
            async def get_all_data(self, data_points, batch_ranges, use_batch_optimization):
                raise RuntimeError("boom")

        manager = _initialized_manager(_ExplodingClient(connected=True))

        result = await manager.get_all_data({"a": {"address": 1}})

        assert result == {}

    async def test_none_result_from_client_normalizes_to_empty_dict(self) -> None:
        class _NoneReturningClient(_FakeClient):
            async def get_all_data(self, data_points, batch_ranges, use_batch_optimization):
                return None

        manager = _initialized_manager(_NoneReturningClient(connected=True))

        result = await manager.get_all_data({"a": {"address": 1}})

        assert result == {}


class TestUpdateIpAddress:
    """update_ip_address() mDNS-driven IP change handling."""

    async def test_disconnects_and_drops_client_reference(self) -> None:
        # Arrange
        fake = _FakeClient(connected=True)
        manager = _initialized_manager(fake)

        # Act
        await manager.update_ip_address("192.168.1.99")

        # Assert
        assert fake.disconnect_called is True
        assert manager._client is None
        assert manager._ip_address == "192.168.1.99"

    async def test_uninitialized_manager_without_lock_is_a_no_op(self) -> None:
        manager = ModbusConnectionManager()
        await manager.update_ip_address("192.168.1.99")  # must not raise
        assert manager._ip_address is None

    async def test_client_disconnect_exception_is_swallowed(self) -> None:
        # Arrange
        class _ExplodingClient(_FakeClient):
            async def disconnect(self) -> None:
                raise RuntimeError("boom")

        manager = _initialized_manager(_ExplodingClient(connected=True))

        # Act & Assert: must not raise even though disconnect() fails.
        await manager.update_ip_address("192.168.1.99")
        assert manager._client is None


class TestForceDisconnect:
    """force_disconnect() error-recovery disconnect."""

    async def test_disconnects_existing_client(self) -> None:
        fake = _FakeClient(connected=True)
        manager = _initialized_manager(fake)

        await manager.force_disconnect()

        assert fake.disconnect_called is True

    async def test_creates_lock_lazily_if_missing(self) -> None:
        manager = ModbusConnectionManager()
        manager._io_lock = None

        await manager.force_disconnect()  # must not raise

        assert manager._io_lock is not None

    async def test_client_disconnect_exception_is_swallowed(self) -> None:
        class _ExplodingClient(_FakeClient):
            async def disconnect(self) -> None:
                raise RuntimeError("boom")

        manager = _initialized_manager(_ExplodingClient(connected=True))

        await manager.force_disconnect()  # must not raise


class TestDisconnect:
    """disconnect() full teardown including cleanup task cancellation."""

    async def test_disconnects_client_and_resets_state(self) -> None:
        # Arrange
        fake = _FakeClient(connected=True)
        manager = _initialized_manager(fake)

        # Act
        await manager.disconnect()

        # Assert
        assert fake.disconnect_called is True
        assert manager._client is None
        assert manager._is_initialized is False
        assert manager._io_lock is None

    async def test_uninitialized_manager_without_lock_is_a_no_op(self) -> None:
        manager = ModbusConnectionManager()
        await manager.disconnect()  # must not raise

    async def test_client_disconnect_exception_is_swallowed(self) -> None:
        class _ExplodingClient(_FakeClient):
            async def disconnect(self) -> None:
                raise RuntimeError("boom")

        manager = _initialized_manager(_ExplodingClient(connected=True))

        await manager.disconnect()  # must not raise
        assert manager._client is None

    async def test_cancels_running_cleanup_task(self) -> None:
        # Arrange
        fake = _FakeClient(connected=True)
        manager = _initialized_manager(fake)
        manager._cleanup_task = asyncio.create_task(asyncio.sleep(10))

        # Act
        await manager.disconnect()

        # Assert
        assert manager._cleanup_task is None


class TestGetConnectionInfo:
    """get_connection_info() summary dict assembly."""

    def test_no_client_reports_disconnected(self) -> None:
        manager = ModbusConnectionManager()
        manager.initialize("127.0.0.1", 502)

        info = manager.get_connection_info()

        assert info["connected"] is False
        assert info["ip_address"] == "127.0.0.1"

    def test_with_client_merges_client_info(self) -> None:
        fake = _FakeClient(connected=True)
        manager = _initialized_manager(fake)

        info = manager.get_connection_info()

        assert info["connected"] is True

    def test_client_get_connection_info_exception_is_caught(self) -> None:
        class _ExplodingClient(_FakeClient):
            def get_connection_info(self) -> dict:
                raise RuntimeError("boom")

        manager = _initialized_manager(_ExplodingClient(connected=True))

        info = manager.get_connection_info()

        assert info["connected"] is False
        assert "error" in info
