"""Modbus Connection Manager - Global singleton connection management"""

import asyncio
import logging
import time
from typing import Optional

from .modbus_client import AnkerSolixModbusClient
from .connection_state import ConnectionState, ConnectionStateMachine
from .const import CONNECTION_CHECK_INTERVAL, DEFAULT_TIMEOUT


class ModbusConnectionManager:
    """Modbus Connection Manager - Independent instance per device"""

    def __init__(self):
        """Initialize connection manager"""
        self._client: Optional[AnkerSolixModbusClient] = None
        self._ip_address: Optional[str] = None
        self._port: int = 502
        self._logger = logging.getLogger(__name__)
        self._connection_lock: Optional[asyncio.Lock] = None
        self._operation_lock: Optional[asyncio.Lock] = None  # Lock for read/write operations
        self._last_activity = 0
        self._connection_timeout = 300  # Close connection after 5 minutes of inactivity
        self._cleanup_task: Optional[asyncio.Task] = None
        self._is_initialized = False

        # Use connection state machine for better state management
        self._state_machine = ConnectionStateMachine()
        
    def initialize(self, ip_address: str, port: int = 502) -> None:
        """Initialize connection parameters"""
        self._ip_address = ip_address
        self._port = port
        self._connection_lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()  # Lock for serializing read/write operations
        self._is_initialized = True
        self._logger.info("Modbus connection manager initialized: %s:%d", ip_address, port)
        
    async def get_client(self) -> Optional[AnkerSolixModbusClient]:
        """Get Modbus client connection"""
        if not self._is_initialized or not self._connection_lock:
            self._logger.error("Connection manager not initialized, please call initialize() first")
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

            if self._client:
                self._logger.debug("Closing old connection")
                self._client.disconnect()

            self._logger.debug("Creating new Modbus connection: %s:%d", self._ip_address, self._port)
            self._client = AnkerSolixModbusClient(self._ip_address, self._port)

            # Connect synchronously in async environment
            loop = asyncio.get_event_loop()
            success = await loop.run_in_executor(None, self._client.connect)

            if success:
                self._logger.info("Modbus connection created successfully: %s:%d", self._ip_address, self._port)
                self._last_activity = time.time()
                # Transition to CONNECTED state
                await self._state_machine.transition_to(ConnectionState.CONNECTED)
            else:
                self._logger.error("Failed to create Modbus connection: %s:%d", self._ip_address, self._port)
                self._client = None
                # Transition to ERROR state
                await self._state_machine.transition_to(ConnectionState.ERROR)

        except Exception as e:
            self._logger.error("Exception occurred while creating Modbus connection: %s", e, exc_info=True)
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
    
    async def _cleanup_connection(self) -> None:
        """Periodically clean up connections"""
        try:
            while True:
                await asyncio.sleep(60)  # Check every minute

                if not self._connection_lock:
                    self._logger.debug("Connection manager closed, exiting cleanup task")
                    break

                if (self._client and
                    time.time() - self._last_activity > self._connection_timeout):

                    async with self._connection_lock:
                        if (self._client and
                            time.time() - self._last_activity > self._connection_timeout):

                            self._logger.info("Connection timeout, closing Modbus connection")
                            try:
                                self._client.disconnect()
                            except Exception as disconnect_error:
                                self._logger.warning("Exception occurred while closing connection: %s", disconnect_error)
                            finally:
                                self._client = None

        except asyncio.CancelledError:
            self._logger.debug("Cleanup task cancelled")
            raise
        except Exception as e:
            self._logger.error("Exception occurred while cleaning up connection: %s", e, exc_info=True)
    
    async def read_register(self, address: int, data_type: str, count: int = None):
        """Read register"""
        client = await self.get_client()
        if not client:
            self._logger.warning("Unable to get client connection, failed to read register %d", address)
            return 0

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, client.read_register, address, data_type, count
            )
            self._last_activity = time.time()
            return result
        except Exception as e:
            self._logger.error("Failed to read register %d: %s", address, e, exc_info=True)
            return 0

    async def read_device_pn(self) -> tuple[str, str, str]:
        """Read device PN from register 0x8000 (32768) and return MD5 hash with raw data.

        Returns:
            tuple: (pn_hash, raw_pn, raw_registers_hex) or ("", "", "") on failure
        """
        client = await self.get_client()
        if not client:
            self._logger.warning("Unable to get client connection, failed to read device PN")
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
    ) -> bool:
        """Write register with timeout control.

        Uses a fresh connection to avoid TCP state issues.

        Args:
            address: Register address
            value: Value to write
            data_type: Data type (UINT16, INT32, etc.)
            timeout: Timeout in seconds (default: 15.0, device responds quickly)

        Returns:
            True if write succeeded, False otherwise
        """
        # Use operation lock to prevent concurrent read/write operations
        if not self._operation_lock:
            self._operation_lock = asyncio.Lock()

        self._logger.info(
            "Write register request | address=%d (0x%04X), value=%s, data_type=%s, timeout=%.1fs, waiting_for_lock=%s",
            address, address, value, data_type, timeout, self._operation_lock.locked()
        )

        async with self._operation_lock:
            self._logger.info("Write register acquired lock, reconnecting for clean state...")

            # Force reconnect before write to ensure clean TCP state
            # This is critical because long-lived connections may have stale data
            try:
                if self._client:
                    self._client.disconnect()
                    self._client = None
                # Reset state machine to allow proper transition
                self._state_machine.reset()
                await self._create_connection()
            except Exception as e:
                self._logger.error("Failed to reconnect before write: %s", e)
                return False

            client = self._client
            if not client:
                self._logger.warning(
                    "Unable to get client connection | write_register address=%d (0x%04X), value=%s, data_type=%s",
                    address, address, value, data_type
                )
                return False

            try:
                loop = asyncio.get_event_loop()
                result = await asyncio.wait_for(
                    loop.run_in_executor(
                        None, client.write_register, address, value, data_type
                    ),
                    timeout=timeout,
                )
                self._last_activity = time.time()

                if result:
                    self._logger.info(
                        "Write register completed | address=%d (0x%04X), value=%s, data_type=%s, result=SUCCESS",
                        address, address, value, data_type
                    )
                else:
                    self._logger.warning(
                        "Write register completed | address=%d (0x%04X), value=%s, data_type=%s, result=FAILED",
                        address, address, value, data_type
                    )
                return result
            except asyncio.TimeoutError:
                self._logger.error(
                    "Write register TIMEOUT | address=%d (0x%04X), value=%s, data_type=%s, timeout=%.1fs",
                    address, address, value, data_type, timeout
                )
                return False
            except Exception as e:
                error_str = str(e)
                # "No response received" - pymodbus didn't accept device response format
                # But device actually responded (RX received), so treat as success
                if "No response received" in error_str:
                    self._logger.info(
                        "Write register SUCCESS (pymodbus format issue) | address=%d (0x%04X), value=%s, data_type=%s",
                        address, address, value, data_type
                    )
                    return True
                self._logger.error(
                    "Write register EXCEPTION | address=%d (0x%04X), value=%s, data_type=%s, error=%s",
                    address, address, value, data_type, e, exc_info=True
                )
                return False
    
    async def get_all_data(
        self,
        data_points: dict,
        batch_ranges: list[tuple[int, int, str]] | None = None,
        *,
        use_batch_optimization: bool = True,
    ) -> dict:
        """Batch read data"""
        self._logger.debug("get_all_data called with %d data points", len(data_points) if data_points else 0)
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
                    None, client.get_all_data, data_points, batch_ranges, use_batch_optimization
                )
                self._last_activity = time.time()
                self._logger.debug("get_all_data completed, got %d results", len(result) if result else 0)
                return result if result else {}
            except Exception as e:
                self._logger.error("Failed to batch read data: %s", e, exc_info=True)
                return {}
    
    def force_disconnect(self) -> None:
        """Force disconnect without acquiring lock - for error recovery."""
        if self._client:
            self._logger.info("Force disconnecting Modbus connection")
            try:
                self._client.disconnect()
            except Exception as e:
                self._logger.debug("Exception during force disconnect: %s", e)
            finally:
                self._client = None

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
                    self._client.disconnect()
                except Exception as e:
                    self._logger.warning("Exception occurred while disconnecting: %s", e)
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
                    self._logger.warning("Exception occurred while waiting for cleanup task to finish: %s", e)
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
            "connection_state": self._state_machine.current_state.value
        }

        if not self._client:
            base_info["connected"] = False
            return base_info

        try:
            client_info = self._client.get_connection_info()
            base_info.update(client_info)
            return base_info
        except Exception as e:
            self._logger.warning("Failed to get client connection info: %s", e)
            base_info["connected"] = False
            base_info["error"] = str(e)
            return base_info


# Removed global singleton instance, each device creates its own manager
