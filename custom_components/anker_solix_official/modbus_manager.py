"""Modbus Connection Manager - Global singleton connection management"""

import asyncio
import logging
import time
from typing import Optional

from .const import CONNECTION_CHECK_INTERVAL
from .device_logger import WriteResult
from .modbus_client import AnkerSolixModbusClient


class ModbusConnectionManager:
    """Modbus Connection Manager - Independent instance per device"""

    def __init__(self):
        """Initialize connection manager"""
        self._client: Optional[AnkerSolixModbusClient] = None
        self._ip_address: Optional[str] = None
        self._port: int = 502
        self._device_name: Optional[str] = None
        self._logger = logging.getLogger(__name__)
        # Single lock serializing all connection lifecycle + read/write I/O.
        self._io_lock: Optional[asyncio.Lock] = None
        self._last_activity = 0
        self._connection_timeout = 300  # Close connection after 5 minutes of inactivity
        self._cleanup_task: Optional[asyncio.Task] = None
        self._is_initialized = False
        self._connected_event = asyncio.Event()

    def initialize(
        self, ip_address: str, port: int = 502, device_name: str | None = None
    ) -> None:
        """Initialize connection parameters"""
        self._ip_address = ip_address
        self._port = port
        self._device_name = device_name or f"{ip_address}:{port}"
        self._io_lock = asyncio.Lock()
        self._is_initialized = True
        self._logger.info(
            "Modbus connection manager initialized: %s (%s:%d)",
            self._device_name,
            ip_address,
            port,
        )

    async def _ensure_connected_locked(self) -> Optional[AnkerSolixModbusClient]:
        """Create the client if needed and ensure it is connected.

        Must be called with _io_lock held. The client object is persistent and
        reused; reconnection closes and re-opens the transport on the same
        object rather than instantiating a replacement.
        """
        if self._client is None:
            self._client = AnkerSolixModbusClient(
                self._ip_address, self._port, self._device_name
            )

        if self._client.is_connected():
            self._connected_event.set()
            return self._client

        self._connected_event.clear()
        try:
            connected = await self._client.connect()
        except Exception as e:
            self._logger.debug("Exception while connecting: %s", e)
            connected = False

        if connected:
            self._last_activity = time.time()
            self._connected_event.set()
            return self._client

        return None

    def _ensure_cleanup_task(self) -> None:
        """Start the idle-cleanup task if it is not running."""
        if not self._cleanup_task or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_connection())

    async def get_client(self) -> Optional[AnkerSolixModbusClient]:
        """Get Modbus client connection"""
        if not self._is_initialized or not self._io_lock:
            self._logger.error(
                "Connection manager not initialized, please call initialize() first"
            )
            return None

        async with self._io_lock:
            client = await self._ensure_connected_locked()
            if client is not None:
                self._last_activity = time.time()
                self._ensure_cleanup_task()
            return client

    async def _wait_for_connection_ready(self, timeout: float = 5.0) -> bool:
        """Give the connection loop a short grace period before failing a write.

        Called OUTSIDE _io_lock so the wait does not block the coordinator's
        periodic get_all_data() (issue #83: a write arriving during a transient
        Wi-Fi drop should not be treated as a hard failure if the connection
        self-heals within a few seconds).
        """
        if self._client and self._client.is_connected():
            return True
        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def _cleanup_connection(self) -> None:
        """Periodically clean up connections"""
        try:
            while True:
                await asyncio.sleep(CONNECTION_CHECK_INTERVAL)

                if not self._io_lock:
                    self._logger.debug(
                        "Connection manager closed, exiting cleanup task"
                    )
                    break

                if (
                    self._client
                    and self._client.is_connected()
                    and time.time() - self._last_activity > self._connection_timeout
                ):
                    async with self._io_lock:
                        if (
                            self._client
                            and self._client.is_connected()
                            and time.time() - self._last_activity
                            > self._connection_timeout
                        ):
                            self._logger.info(
                                "Connection timeout, closing Modbus connection"
                            )
                            try:
                                await self._client.disconnect()
                            except Exception as disconnect_error:
                                self._logger.info(
                                    "Exception occurred while closing connection: %s",
                                    disconnect_error,
                                )
                            finally:
                                self._connected_event.clear()

        except asyncio.CancelledError:
            self._logger.debug("Cleanup task cancelled")
            raise
        except Exception as e:
            self._logger.error(
                "Exception occurred while cleaning up connection: %s", e, exc_info=True
            )

    async def read_register(self, address: int, data_type: str, count: int = None):
        """Read register"""
        if not self._io_lock:
            return None
        async with self._io_lock:
            client = await self._ensure_connected_locked()
            if not client:
                self._logger.warning(
                    "Unable to get client connection, failed to read register %d",
                    address,
                )
                return None
            try:
                result = await client.read_register(address, data_type, count)
                self._last_activity = time.time()
                return result
            except Exception as e:
                self._logger.error(
                    "Failed to read register %d: %s", address, e, exc_info=True
                )
                return None

    async def read_device_pn(self) -> tuple[str, str, str]:
        """Read device PN from register 0x8000 (32768) and return MD5 hash with raw data.

        Returns:
            tuple: (pn_hash, raw_pn, raw_registers_hex) or ("", "", "") on failure
        """
        if not self._io_lock:
            return ("", "", "")
        async with self._io_lock:
            client = await self._ensure_connected_locked()
            if not client:
                self._logger.warning(
                    "Unable to get client connection, failed to read device PN"
                )
                return ("", "", "")
            try:
                result = await client.read_device_pn()
                self._last_activity = time.time()
                return result
            except Exception as e:
                self._logger.error("Failed to read device PN: %s", e, exc_info=True)
                return ("", "", "")

    async def write_register(
        self,
        address: int,
        value,
        data_type: str,
        timeout: float = 30.0,
        lock_timeout: float = 15.0,
    ) -> WriteResult:
        """Write register with timeout control.

        Args:
            address: Register address
            value: Value to write
            data_type: Data type (UINT16, INT32, etc.)
            timeout: Outer backstop in seconds (default 30.0). pymodbus bounds
                each request internally and normally completes far sooner.
            lock_timeout: Max seconds to wait for the I/O lock (default 15.0).

        Returns:
            WriteResult with success status and error details
        """
        if not self._io_lock:
            self._io_lock = asyncio.Lock()

        self._logger.debug(
            "Write register request | [%s] device=%s:%d, address=%d (0x%04X), value=%s, data_type=%s, waiting_for_lock=%s",
            self._device_name,
            self._ip_address,
            self._port,
            address,
            address,
            value,
            data_type,
            self._io_lock.locked(),
        )

        # Grace period BEFORE acquiring the lock, so a transient drop
        # (issue #83) does not delay get_all_data()'s periodic polling,
        # which also waits on _io_lock.
        #
        # `timeout` only bounds the request once the lock is held, so the wait
        # is bounded too: a user action must fail fast rather than hang the
        # service call behind a degraded poll. Both waits share one deadline so
        # the total is `lock_timeout`, not grace + lock_timeout.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + lock_timeout
        connection_ready = await self._wait_for_connection_ready(
            timeout=min(5.0, lock_timeout)
        )

        remaining = deadline - loop.time()
        try:
            if remaining <= 0:
                raise asyncio.TimeoutError
            await asyncio.wait_for(self._io_lock.acquire(), timeout=remaining)
        except asyncio.TimeoutError:
            self._logger.warning(
                "Write register LOCK TIMEOUT | [%s] device=%s:%d, address=%d (0x%04X), "
                "value=%s, data_type=%s, waited=%.1fs",
                self._device_name,
                self._ip_address,
                self._port,
                address,
                address,
                value,
                data_type,
                lock_timeout,
            )
            return WriteResult(
                success=False,
                error_reason=f"Device busy, write not sent ({lock_timeout}s lock timeout)",
                is_transient=True,
            )

        try:
            self._logger.debug(
                "Write register acquired lock (connection_ready=%s)", connection_ready
            )

            client = await self._ensure_connected_locked()
            if not client:
                self._logger.warning(
                    "Unable to get client connection | write_register address=%d (0x%04X), value=%s, data_type=%s",
                    address,
                    address,
                    value,
                    data_type,
                )
                return WriteResult(
                    success=False,
                    error_reason="Unable to get client connection",
                    is_transient=True,
                )

            try:
                result = await asyncio.wait_for(
                    client.write_register(address, value, data_type),
                    timeout=timeout,
                )
                self._last_activity = time.time()

                if result.success:
                    self._logger.debug(
                        "Write register completed | [%s] device=%s:%d, address=%d (0x%04X), value=%s, data_type=%s, result=SUCCESS",
                        self._device_name,
                        self._ip_address,
                        self._port,
                        address,
                        address,
                        value,
                        data_type,
                    )
                elif result.is_transient:
                    self._logger.warning(
                        "Write register deferred (connection not ready) | [%s] device=%s:%d, address=%d (0x%04X), value=%s, data_type=%s, reason=%s",
                        self._device_name,
                        self._ip_address,
                        self._port,
                        address,
                        address,
                        value,
                        data_type,
                        result.error_reason,
                    )
                else:
                    self._logger.error(
                        "Write register completed | [%s] device=%s:%d, address=%d (0x%04X), value=%s, data_type=%s, result=FAILED, reason=%s",
                        self._device_name,
                        self._ip_address,
                        self._port,
                        address,
                        address,
                        value,
                        data_type,
                        result.error_reason,
                    )
                return result
            except asyncio.TimeoutError:
                self._logger.warning(
                    "Write register TIMEOUT | [%s] device=%s:%d, address=%d (0x%04X), value=%s, data_type=%s, timeout=%.1fs",
                    self._device_name,
                    self._ip_address,
                    self._port,
                    address,
                    address,
                    value,
                    data_type,
                    timeout,
                )
                return WriteResult(
                    success=False,
                    error_reason=f"Connection timeout ({timeout}s)",
                    is_transient=True,
                )
            except Exception as e:
                error_str = str(e)
                self._logger.error(
                    "Write register EXCEPTION | [%s] address=%d (0x%04X), value=%s, data_type=%s, error=%s",
                    self._device_name,
                    address,
                    address,
                    value,
                    data_type,
                    e,
                    exc_info=True,
                )
                return WriteResult(
                    success=False, error_reason=f"{type(e).__name__}: {error_str}"
                )
        finally:
            self._io_lock.release()

    async def get_all_data(
        self,
        data_points: dict,
        batch_ranges: list[tuple[int, int, str]] | None = None,
        *,
        use_batch_optimization: bool = True,
    ) -> dict:
        """Batch read data"""
        self._logger.debug(
            "get_all_data called with %d data points",
            len(data_points) if data_points else 0,
        )
        if not self._io_lock:
            return {}

        async with self._io_lock:
            client = await self._ensure_connected_locked()
            if not client:
                # DEBUG, not WARNING: connect() already reported this failure
                # via _handle_connection_error() (throttled), so at a 5s poll
                # this would only repeat it once per cycle for a whole outage.
                self._logger.debug("get_all_data: no client available")
                return {}
            try:
                result = await client.get_all_data(
                    data_points,
                    batch_ranges,
                    use_batch_optimization,
                )
                self._last_activity = time.time()
                self._logger.debug(
                    "get_all_data completed, got %d results",
                    len(result) if result else 0,
                )
                return result if result else {}
            except Exception as e:
                self._logger.error("Failed to batch read data: %s", e, exc_info=True)
                return {}

    async def update_ip_address(self, new_ip: str) -> None:
        """Switch connection target after mDNS discovers a new device IP.

        Closes the current client (cancelling any background reconnect) and
        drops it so the next operation creates a client for the new IP.
        """
        if not self._io_lock:
            return
        async with self._io_lock:
            if self._client:
                try:
                    await self._client.disconnect()
                except Exception as e:
                    self._logger.debug("Exception closing client on IP change: %s", e)
                self._client = None
            self._ip_address = new_ip
            self._connected_event.clear()

    async def force_disconnect(self) -> None:
        """Force disconnect for error recovery."""
        if not self._io_lock:
            self._io_lock = asyncio.Lock()

        async with self._io_lock:
            if self._client:
                self._logger.info("Force disconnecting Modbus connection")
                try:
                    await self._client.disconnect()
                except Exception as e:
                    self._logger.debug("Exception during force disconnect: %s", e)
            self._connected_event.clear()

    async def disconnect(self) -> None:
        """Disconnect and clean up resources"""
        if not self._io_lock:
            return

        async with self._io_lock:
            # Close client connection
            if self._client:
                self._logger.info("Disconnecting Modbus connection")
                try:
                    await self._client.disconnect()
                except Exception as e:
                    self._logger.warning(
                        "Exception occurred while disconnecting: %s", e
                    )
                finally:
                    self._client = None

            # Cancel cleanup task
            if self._cleanup_task and not self._cleanup_task.done():
                self._cleanup_task.cancel()
                try:
                    await self._cleanup_task
                except asyncio.CancelledError:
                    self._logger.debug("Cleanup task cancelled")
                except Exception as e:
                    self._logger.warning(
                        "Exception occurred while waiting for cleanup task to finish: %s",
                        e,
                    )
                finally:
                    self._cleanup_task = None

        self._connected_event.clear()

        # Clean up state
        self._is_initialized = False
        self._io_lock = None

    def get_connection_info(self) -> dict:
        """Get connection information"""
        base_info = {
            "ip_address": self._ip_address,
            "port": self._port,
            "last_activity": self._last_activity,
            "connection_timeout": self._connection_timeout,
            "is_initialized": self._is_initialized,
        }

        if not self._client:
            base_info["connected"] = False
            return base_info

        try:
            client_info = self._client.get_connection_info()
            base_info.update(client_info)
            return base_info
        except Exception as e:
            self._logger.info("Failed to get client connection info: %s", e)
            base_info["connected"] = False
            base_info["error"] = str(e)
            return base_info


# Removed global singleton instance, each device creates its own manager
