"""Modbus Connection Manager - Global singleton connection management"""

import asyncio
import logging
import time
from typing import Optional

from .modbus_client import AnkerSolixModbusClient
from .connection_state import ConnectionState, ConnectionStateMachine
from .const import (
    CONNECTION_CHECK_INTERVAL,
    DEFAULT_TIMEOUT,
    MODBUS_CONNECT_TIMEOUT,
    MODBUS_DISCONNECT_TIMEOUT,
)
from .device_logger import WriteResult


class ModbusConnectionManager:
    """Modbus Connection Manager - Independent instance per device"""

    def __init__(self):
        """Initialize connection manager"""
        self._client: Optional[AnkerSolixModbusClient] = None
        self._ip_address: Optional[str] = None
        self._port: int = 502
        self._device_name: Optional[str] = None
        self._logger = logging.getLogger(__name__)
        self._connection_lock: Optional[asyncio.Lock] = None
        self._operation_lock: Optional[asyncio.Lock] = (
            None  # Lock for read/write operations
        )
        self._last_activity = 0
        self._connection_timeout = 300  # Close connection after 5 minutes of inactivity
        self._cleanup_task: Optional[asyncio.Task] = None
        self._is_initialized = False

        # Use connection state machine for better state management
        self._state_machine = ConnectionStateMachine()

    def initialize(
        self, ip_address: str, port: int = 502, device_name: str | None = None
    ) -> None:
        """Initialize connection parameters"""
        self._ip_address = ip_address
        self._port = port
        self._device_name = device_name or f"{ip_address}:{port}"
        self._connection_lock = asyncio.Lock()
        self._operation_lock = (
            asyncio.Lock()
        )  # Lock for serializing read/write operations
        self._is_initialized = True
        self._logger.info(
            "Modbus connection manager initialized: %s (%s:%d)",
            self._device_name,
            ip_address,
            port,
        )

    async def get_client(self) -> Optional[AnkerSolixModbusClient]:
        """Get Modbus client connection"""
        if not self._is_initialized or not self._connection_lock:
            self._logger.error(
                "Connection manager not initialized, please call initialize() first"
            )
            return None

        async with self._connection_lock:
            # Create new connection if connection doesn't exist or is disconnected
            if not self._client or not await self._is_connected():
                await self._create_connection()

            # Update last activity time
            self._last_activity = time.time()

            # Start cleanup task if not already started
            if not self._cleanup_task or self._cleanup_task.done():
                self._cleanup_task = asyncio.create_task(self._cleanup_connection())

            return self._client

    async def _create_connection(self) -> None:
        """Create new Modbus connection"""
        try:
            # Transition to CONNECTING state
            await self._state_machine.transition_to(ConnectionState.CONNECTING)

            loop = asyncio.get_event_loop()

            if self._client:
                self._logger.debug("Closing old connection")
                await asyncio.wait_for(
                    loop.run_in_executor(None, self._client.disconnect),
                    timeout=MODBUS_DISCONNECT_TIMEOUT,
                )

            self._logger.debug(
                "Creating new Modbus connection: %s (%s:%d)",
                self._device_name,
                self._ip_address,
                self._port,
            )
            self._client = AnkerSolixModbusClient(
                self._ip_address, self._port, self._device_name
            )

            success = await asyncio.wait_for(
                loop.run_in_executor(None, self._client.connect),
                timeout=MODBUS_CONNECT_TIMEOUT,
            )

            if success:
                self._logger.debug(
                    "Modbus connection created successfully: %s:%d",
                    self._ip_address,
                    self._port,
                )
                self._last_activity = time.time()
                # Transition to CONNECTED state
                await self._state_machine.transition_to(ConnectionState.CONNECTED)
            else:
                self._logger.debug(
                    "Failed to create Modbus connection: %s:%d",
                    self._ip_address,
                    self._port,
                )
                self._client = None
                # Transition to ERROR state
                await self._state_machine.transition_to(ConnectionState.ERROR)

        except Exception as e:
            self._logger.debug(
                "Exception occurred while creating Modbus connection: %s", e
            )
            self._client = None
            # Transition to ERROR state
            await self._state_machine.transition_to(ConnectionState.ERROR)

    async def _is_connected(self) -> bool:
        """Check if connection is valid"""
        if not self._client:
            return False

        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self._client._ensure_connection)
        except Exception as e:
            self._logger.debug("Connection check failed: %s", e)
            return False

    async def _wait_for_connection_ready(self, timeout: float = 5.0) -> bool:
        """Give the connection loop a short grace period before failing a write.

        Called OUTSIDE _connection_lock/_operation_lock so the wait does not
        block the coordinator's periodic get_all_data() (issue #83: a write
        arriving during a transient Wi-Fi drop should not be treated as a
        hard failure if the connection self-heals within a few seconds).
        """
        if self._client and await self._is_connected():
            return True
        return await self._state_machine.wait_for_state(
            ConnectionState.CONNECTED, timeout=timeout
        )

    async def _cleanup_connection(self) -> None:
        """Periodically clean up connections"""
        try:
            while True:
                await asyncio.sleep(60)  # Check every minute

                if not self._connection_lock:
                    self._logger.debug(
                        "Connection manager closed, exiting cleanup task"
                    )
                    break

                if (
                    self._client
                    and time.time() - self._last_activity > self._connection_timeout
                ):
                    async with self._connection_lock:
                        if (
                            self._client
                            and time.time() - self._last_activity
                            > self._connection_timeout
                        ):
                            self._logger.info(
                                "Connection timeout, closing Modbus connection"
                            )
                            try:
                                await asyncio.get_event_loop().run_in_executor(
                                    None, self._client.disconnect
                                )
                            except Exception as disconnect_error:
                                self._logger.info(
                                    "Exception occurred while closing connection: %s",
                                    disconnect_error,
                                )
                            finally:
                                self._client = None
                                await self._state_machine.reset()

        except asyncio.CancelledError:
            self._logger.debug("Cleanup task cancelled")
            raise
        except Exception as e:
            self._logger.error(
                "Exception occurred while cleaning up connection: %s", e, exc_info=True
            )

    async def read_register(self, address: int, data_type: str, count: int = None):
        """Read register"""
        client = await self.get_client()
        if not client:
            self._logger.warning(
                "Unable to get client connection, failed to read register %d", address
            )
            return None

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, client.read_register, address, data_type, count
            )
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
        client = await self.get_client()
        if not client:
            self._logger.warning(
                "Unable to get client connection, failed to read device PN"
            )
            return ("", "", "")

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, client.read_device_pn)
            self._last_activity = time.time()
            return result
        except Exception as e:
            self._logger.error("Failed to read device PN: %s", e, exc_info=True)
            return ("", "", "")

    async def write_register(
        self, address: int, value, data_type: str, timeout: float = 15.0
    ) -> WriteResult:
        """Write register with timeout control.

        Uses a fresh connection to avoid TCP state issues.

        Args:
            address: Register address
            value: Value to write
            data_type: Data type (UINT16, INT32, etc.)
            timeout: Timeout in seconds (default: 15.0, device responds quickly)

        Returns:
            WriteResult with success status and error details
        """
        # Use operation lock to prevent concurrent read/write operations
        if not self._operation_lock:
            self._operation_lock = asyncio.Lock()
        if not self._connection_lock:
            self._connection_lock = asyncio.Lock()

        self._logger.debug(
            "Write register request | [%s] device=%s:%d, address=%d (0x%04X), value=%s, data_type=%s, timeout=%.1fs, waiting_for_lock=%s",
            self._device_name,
            self._ip_address,
            self._port,
            address,
            address,
            value,
            data_type,
            timeout,
            self._operation_lock.locked(),
        )

        # Grace period BEFORE acquiring either lock, so a transient drop
        # (issue #83) does not delay get_all_data()'s periodic polling,
        # which also waits on _operation_lock.
        connection_ready = await self._wait_for_connection_ready(timeout=5.0)

        async with self._connection_lock, self._operation_lock:
            self._logger.debug(
                "Write register acquired locks (connection_ready=%s)", connection_ready
            )

            # Re-check now that the locks are held: something could have
            # torn down the connection in the gap between the wait above
            # and acquiring _connection_lock. Only reconnect when actually
            # needed instead of unconditionally forcing a fresh TCP socket.
            if not (self._client and await self._is_connected()):
                try:
                    if self._client:
                        await asyncio.wait_for(
                            asyncio.get_event_loop().run_in_executor(
                                None, self._client.disconnect
                            ),
                            timeout=MODBUS_DISCONNECT_TIMEOUT,
                        )
                        self._client = None
                    await self._state_machine.reset()
                    await self._create_connection()
                except Exception as e:
                    self._logger.error("Failed to reconnect before write: %s", e)
                    return WriteResult(
                        success=False,
                        error_reason=f"Failed to reconnect before write: {e}",
                        is_transient=True,
                    )

            client = self._client
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
                loop = asyncio.get_event_loop()
                result = await asyncio.wait_for(
                    loop.run_in_executor(
                        None, client.write_register, address, value, data_type
                    ),
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
                if "No response received" in error_str:
                    self._logger.debug(
                        "Write register SUCCESS (pymodbus format issue) | [%s] address=%d (0x%04X), value=%s, data_type=%s",
                        self._device_name,
                        address,
                        address,
                        value,
                        data_type,
                    )
                    return WriteResult(
                        success=True,
                        error_reason="No response received but device responded",
                    )
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
        client = await self.get_client()
        if not client:
            self._logger.warning("get_all_data: no client available")
            return {}

        # Use operation lock to prevent concurrent read/write operations
        if not self._operation_lock:
            self._operation_lock = asyncio.Lock()

        async with self._operation_lock:
            try:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None,
                    client.get_all_data,
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

    async def force_disconnect(self) -> None:
        """Force disconnect for error recovery.

        Uses _connection_lock to stay mutually exclusive with
        _create_connection() and write_register() (issue #72).
        """
        if not self._connection_lock:
            self._connection_lock = asyncio.Lock()

        async with self._connection_lock:
            if self._client:
                self._logger.info("Force disconnecting Modbus connection")
                try:
                    await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(
                            None, self._client.disconnect
                        ),
                        timeout=MODBUS_DISCONNECT_TIMEOUT,
                    )
                except Exception as e:
                    self._logger.debug("Exception during force disconnect: %s", e)
                finally:
                    self._client = None
            await self._state_machine.reset()

    async def disconnect(self) -> None:
        """Disconnect and clean up resources"""
        if not self._connection_lock:
            return

        # Transition to CLOSING state
        await self._state_machine.transition_to(ConnectionState.CLOSING)

        async with self._connection_lock:
            # Close client connection
            if self._client:
                self._logger.info("Disconnecting Modbus connection")
                try:
                    await asyncio.get_event_loop().run_in_executor(
                        None, self._client.disconnect
                    )
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

        # Transition to DISCONNECTED state
        await self._state_machine.transition_to(ConnectionState.DISCONNECTED)

        # Clean up state
        self._is_initialized = False
        self._connection_lock = None

    def get_connection_info(self) -> dict:
        """Get connection information"""
        base_info = {
            "ip_address": self._ip_address,
            "port": self._port,
            "last_activity": self._last_activity,
            "connection_timeout": self._connection_timeout,
            "is_initialized": self._is_initialized,
            "connection_state": self._state_machine.current_state.value,
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
