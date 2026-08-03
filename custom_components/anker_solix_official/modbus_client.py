"""Anker Solix TCP communication module."""

import contextlib
import logging
import time
from pathlib import Path
from typing import Any

from .batch_reader import BatchRegisterReader
from .device_logger import WriteResult

try:
    from pymodbus.client import ModbusTcpClient
    from pymodbus.exceptions import (
        ConnectionException,
        ModbusException,
        ModbusIOException,
    )

    PYMODBUS_VERSION = "3.x"
except ImportError:
    try:
        from pymodbus.client.sync import ModbusTcpClient
        from pymodbus.exceptions import (
            ConnectionException,
            ModbusException,
            ModbusIOException,
        )

        PYMODBUS_VERSION = "2.x"
    except ImportError:
        ModbusTcpClient = None
        ModbusException = None
        ModbusIOException = None
        ConnectionException = None
        PYMODBUS_VERSION = "unknown"


class _RegisterDecodeError(Exception):
    """Malformed register data."""


class AnkerSolixModbusClient:
    """Anker Solix TCP client class."""

    def __init__(
        self,
        ip_address: str = "127.0.0.1",
        port: int = 502,
        device_name: str | None = None,
    ):
        """Initialize Modbus TCP client."""
        if ModbusTcpClient is None:
            raise ImportError(
                "pymodbus not installed, please run: pip install pymodbus>=2.5.0"
            )

        self.ip_address = ip_address
        self.port = port
        self.device_name = device_name or f"{ip_address}:{port}"
        self.client = self._create_client(ip_address, port)
        self._logger = logging.getLogger(__name__)
        self._logger.debug(
            "Using pymodbus version: %s for device %s",
            PYMODBUS_VERSION,
            self.device_name,
        )

        # Configure pymodbus logging to reduce duplicate logs
        pymodbus_logger = logging.getLogger("pymodbus")
        pymodbus_logger.setLevel(
            logging.WARNING
        )  # Only show warnings and errors from pymodbus
        self._connection_status = "disconnected"
        self._last_connection_attempt = 0
        self._connection_retry_interval = 10  # Reduce reconnect interval to 10 seconds
        self._consecutive_errors = 0
        self._last_error_log_time = 0
        self._error_log_interval = 30  # Error log interval (seconds)
        self._error_count_since_last_log = 0
        self._reconnect_delay = 10  # Reconnect delay time (seconds)
        self._last_reconnect_time = 0

        # Initialize batch reader for optimized register reading
        self._batch_reader = BatchRegisterReader()
        
        # Track registers that failed to read (for entity availability)
        self._last_failed_registers: set[int] = set()
        self._last_successful_registers: set[int] = set()

    def get_last_failed_registers(self) -> set[int]:
        """Get set of register addresses that failed in the last read operation."""
        return self._last_failed_registers.copy()

    def get_last_successful_registers(self) -> set[int]:
        """Get set of register addresses that succeeded in the last read operation."""
        return self._last_successful_registers.copy()

    def _create_client(self, host, port):
        """Create Modbus client.

        Note: Device responds quickly but pymodbus may not accept the response format.
        Setting retries=0 to avoid unnecessary retries when device has already responded.
        """
        # pymodbus 3.x: timeout and retries must be passed in constructor
        try:
            return ModbusTcpClient(host=host, port=port, timeout=10, retries=0)
        except TypeError:
            # Fallback for older pymodbus versions
            try:
                client = ModbusTcpClient(host, port)
                if hasattr(client, "timeout"):
                    client.timeout = 10
                if hasattr(client, "retries"):
                    client.retries = 0
                return client
            except TypeError:
                client = ModbusTcpClient()
                if hasattr(client, "host"):
                    client.host = host
                if hasattr(client, "port"):
                    client.port = port
                if hasattr(client, "timeout"):
                    client.timeout = 10
                if hasattr(client, "retries"):
                    client.retries = 0
                return client

    def connect(self) -> bool:
        """Connect to Modbus device."""
        try:
            if hasattr(self.client, "close"):
                with contextlib.suppress(Exception):
                    self.client.close()

            self.client = self._create_client(self.ip_address, self.port)

            if self.client.connect():
                self._connection_status = "connected"
                self._logger.debug(
                    "Successfully connected to Modbus %s:%d", self.ip_address, self.port
                )
                return True

            self._connection_status = "connection_failed"
            # Log connection failure with appropriate level based on error frequency
            if self._consecutive_errors == 0:
                self._logger.info(
                    "Unable to connect to Modbus %s:%d", self.ip_address, self.port
                )
            else:
                self._logger.debug(
                    "Unable to connect to Modbus %s:%d (connection failed)",
                    self.ip_address,
                    self.port,
                )
            return False
        except (ConnectionError, OSError, TimeoutError) as e:
            self._connection_status = "error"
            # Log connection error with appropriate level based on error frequency
            if self._consecutive_errors == 0:
                self._logger.info(
                    "Error connecting to Modbus %s:%d: %s",
                    self.ip_address,
                    self.port,
                    e,
                )
            else:
                self._logger.debug(
                    "Error connecting to Modbus %s:%d: %s",
                    self.ip_address,
                    self.port,
                    e,
                )
            return False

    def disconnect(self):
        """Disconnect."""
        try:
            if hasattr(self.client, "close"):
                self.client.close()
            self._connection_status = "disconnected"
            self._logger.debug(
                "Disconnected from Modbus %s:%d", self.ip_address, self.port
            )
        except (OSError, AttributeError) as e:
            self._logger.error("Error during disconnect: %s", e)

    def _handle_connection_error(
        self, error_msg: str = "", exc: BaseException | None = None
    ):
        """Handle connection error and update error tracking with throttled logging.

        Args:
            error_msg: Human-readable description of the error (used for logging
                and for the legacy string-matching disconnect check).
            exc: The actual exception instance that triggered this call, if any.
                Used to decide whether to force-disconnect based on exception
                *type* rather than relying solely on substring matching in
                ``error_msg``. ``ModbusIOException``/``ModbusException`` (e.g.
                transaction ID mismatch) and connection-level errors
                (``ConnectionException``, ``ConnectionError``, ``OSError``,
                ``TimeoutError``) always warrant a disconnect: the socket may
                be holding a stale/desynced TCP state that would otherwise be
                silently reused on the next call (see issue #81).
        """
        current_time = time.time()
        self._consecutive_errors += 1
        self._connection_status = "error"
        self._error_count_since_last_log += 1

        # Decide whether to force-disconnect. Two independent triggers:
        #   1) Legacy string matching (kept for backward compatibility with
        #      error paths that don't pass an `exc` instance).
        #   2) Exception-type matching (issue #81 fix): covers timeouts and
        #      protocol-level errors such as transaction ID mismatch, which
        #      never contain "Broken pipe"/"Connection reset" in their
        #      message and were previously never triggering a disconnect.
        should_disconnect = "Broken pipe" in error_msg or "Connection reset" in error_msg
        disconnect_reason = "string-match" if should_disconnect else ""

        if not should_disconnect and exc is not None:
            connection_level_excs = (ConnectionError, OSError, TimeoutError)
            if ConnectionException is not None:
                connection_level_excs = connection_level_excs + (ConnectionException,)
            if ModbusException is not None:
                connection_level_excs = connection_level_excs + (ModbusException,)
            if isinstance(exc, connection_level_excs):
                should_disconnect = True
                disconnect_reason = f"exception-type ({type(exc).__name__})"

        if should_disconnect:
            self._logger.debug(
                "Detected connection error requiring disconnect (%s), disconnecting immediately: %s",
                disconnect_reason,
                error_msg,
            )
            self._force_disconnect()

        # Implement log throttling: only log errors under specific conditions
        should_log = False
        log_level = "error"
        log_message = ""

        # Always log first error
        if self._consecutive_errors == 1:
            should_log = True
            log_message = f"Connection error #1: {error_msg}"

        # Periodically log error statistics (at most once every 30 seconds)
        elif current_time - self._last_error_log_time >= self._error_log_interval:
            should_log = True
            log_level = "warning"
            log_message = f"Connection errors continuing: {self._error_count_since_last_log} errors in last {self._error_log_interval}s (total: {self._consecutive_errors})"

        if should_log:
            if log_level == "error":
                self._logger.error(log_message)
            else:
                self._logger.info(log_message)

            self._last_error_log_time = current_time
            self._error_count_since_last_log = 0

    def _force_disconnect(self):
        """Force disconnect and clean up resources."""
        try:
            if hasattr(self.client, "close"):
                self.client.close()
            self._connection_status = "disconnected"
            self._logger.debug(
                "Force disconnected Modbus connection %s:%d", self.ip_address, self.port
            )
        except (OSError, AttributeError) as e:
            self._logger.debug("Exception during disconnect: %s", e)

    def _ensure_connection(self) -> bool:
        """Return current socket state only; reconnection is managed by coordinator."""
        try:
            if hasattr(self.client, "connected"):
                return bool(self.client.connected)
            if hasattr(self.client, "is_socket_open"):
                return bool(self.client.is_socket_open())
            if hasattr(self.client, "is_open"):
                return bool(self.client.is_open())
        except Exception:
            return False
        return False

    def _flush_recv_buffer(self, max_bytes: int = 65536) -> int:
        """Drain any bytes already sitting in the TCP receive buffer.

        Devices under load may fall behind the polling cadence and leave
        stale response packets queued on the socket. If left unread, the
        next request's response gets interleaved with these leftovers,
        producing transaction ID mismatches (issue #81). This performs a
        best-effort, non-blocking drain immediately before issuing a new
        request so pymodbus only has to parse genuinely fresh data.

        Operates directly on the underlying `socket.socket` (bypassing
        pymodbus's `recv()`, which has its own framing/timeout semantics
        not suited for a plain drain) so it must tolerate the socket being
        `None` or already closed.

        Returns the number of bytes discarded (0 if nothing to flush or
        the socket isn't available).
        """
        sock = getattr(self.client, "socket", None)
        if sock is None:
            return 0

        discarded = 0
        try:
            sock.setblocking(False)
            while discarded < max_bytes:
                chunk = sock.recv(4096)
                if not chunk:
                    # Peer closed the connection; nothing more to drain.
                    break
                discarded += len(chunk)
        except (BlockingIOError, InterruptedError):
            # Expected: no more data currently available to read.
            pass
        except (OSError, AttributeError, ValueError) as e:
            # Socket in an unexpected state (closed mid-drain, etc.) - not
            # fatal, the caller will still attempt its own read/write and
            # surface any real problem through its own exception handling.
            self._logger.debug("Exception while flushing recv buffer: %s", e)
        finally:
            with contextlib.suppress(Exception):
                sock.setblocking(True)

        if discarded:
            self._logger.debug(
                "Flushed %d stale byte(s) from recv buffer before next request",
                discarded,
            )
        return discarded

    def _default_value(self, data_type: str) -> Any:
        """Return default fallback value for a data type."""
        return "" if data_type in ("STRING", "VERSION") else 0

    def _decode_register_value(
        self, address: int, data_type: str, registers: list[int]
    ) -> Any:
        """Decode register list into the correct Python value."""
        if not registers:
            self._logger.error("Register %d returned no data", address)
            raise _RegisterDecodeError(f"Register {address} returned no data")

        # Defensive check: ensure no None values in registers list
        if any(r is None for r in registers):
            self._logger.debug(
                "Register %d contains None values: %s, returning default",
                address,
                registers,
            )
            raise _RegisterDecodeError(
                f"Register {address} contains None values: {registers}"
            )

        try:
            if data_type == "UINT16":
                value = registers[0]
            elif data_type == "INT16":
                raw = registers[0] & 0xFFFF
                value = raw if raw < 0x8000 else raw - 0x10000
            elif data_type == "INT32":
                if len(registers) < 2:
                    self._logger.debug(
                        "Register %d requires 2 values for INT32, got %d",
                        address,
                        len(registers),
                    )
                    raise _RegisterDecodeError(
                        f"Register {address} requires 2 values for INT32, "
                        f"got {len(registers)}"
                    )
                # Big-endian: registers[0] is high 16-bit, registers[1] is low 16-bit
                high = registers[0] & 0xFFFF
                low = registers[1] & 0xFFFF
                unsigned = (high << 16) | low
                if unsigned & 0x80000000:
                    value = -((~unsigned & 0xFFFFFFFF) + 1)
                else:
                    value = unsigned
            elif data_type == "UINT32":
                if len(registers) < 2:
                    self._logger.debug(
                        "Register %d requires 2 values for UINT32, got %d",
                        address,
                        len(registers),
                    )
                    raise _RegisterDecodeError(
                        f"Register {address} requires 2 values for UINT32, "
                        f"got {len(registers)}"
                    )
                # Big-endian: registers[0] is high 16-bit, registers[1] is low 16-bit
                high = registers[0] & 0xFFFF
                low = registers[1] & 0xFFFF
                value = (high << 16) | low
            elif data_type == "VERSION":
                # VERSION format: 4 bytes representing version segments
                # e.g., [0x00, 0x00, 0x01, 0x00] -> "0.0.1.0"
                version_bytes = []
                for reg in registers[:2]:  # Only use first 2 registers (4 bytes)
                    version_bytes.append((reg >> 8) & 0xFF)
                    version_bytes.append(reg & 0xFF)

                self._logger.debug(
                    "Version bytes for address %d: %s", address, version_bytes
                )

                # Format as version string: "X.X.X.X"
                if len(version_bytes) >= 4:
                    value = f"{version_bytes[0]}.{version_bytes[1]}.{version_bytes[2]}.{version_bytes[3]}"
                else:
                    value = ""

                self._logger.debug(
                    "Decoded version at address %d: '%s'", address, value
                )
            elif data_type == "STRING":
                string_bytes = []
                for reg in registers:
                    # Big-endian: high byte first, low byte second
                    string_bytes.append((reg >> 8) & 0xFF)
                    string_bytes.append(reg & 0xFF)

                self._logger.debug(
                    "Raw registers for address %d: %s", address, registers
                )
                self._logger.debug("String bytes (big endian): %s", string_bytes)

                try:
                    value = (
                        bytes(string_bytes)
                        .decode("utf-8", errors="ignore")
                        .rstrip("\x00")
                    )
                    self._logger.debug(
                        "Decoded string at address %d: '%s'", address, value
                    )
                except (UnicodeDecodeError, ValueError) as err:
                    self._logger.debug(
                        "String decoding failed for address %d: %s", address, err
                    )
                    value = ""
            else:
                value = registers[0]

            self._logger.debug(
                "Decoded register %d -> %s (%s)", address, value, data_type
            )
            return value
        except _RegisterDecodeError:
            raise
        except Exception as err:
            self._logger.debug(
                "Failed to decode register %d (%s): %s", address, data_type, err
            )
            return self._default_value(data_type)

    def read_register(
        self, address: int, data_type: str, count: int = None
    ) -> Any | None:
        """Read input register (function code 04)."""
        if not self._ensure_connection():
            self._logger.warning("Unable to read register %d, not connected", address)
            return None

        if count is None:
            count = (
                1
                if data_type in ("UINT16", "INT16")
                else 2
                if data_type in ("INT32", "UINT32", "VERSION")
                else 10
                if data_type == "STRING"
                else 1
            )

        try:
            result = self.client.read_input_registers(address=address, count=count)

            if not result or result.isError():
                self._logger.error("Failed to read register %d: %s", address, result)
                return None

            registers = getattr(result, "registers", None) or getattr(
                result, "data", None
            )
            return self._decode_register_value(address, data_type, registers[:count])
        except _RegisterDecodeError as e:
            self._handle_connection_error(
                f"Exception reading register {address}: {e}", exc=e
            )
            return None
        except (ConnectionError, OSError, TimeoutError, ValueError) as e:
            self._handle_connection_error(
                f"Exception reading register {address}: {e}", exc=e
            )
            return None

    def read_device_pn(self) -> tuple[str, str, str]:
        """Read device PN from register 0x8000 (32768) and return salted SHA-256 hash.

        Reads 5 registers as STRING, strips spaces and null characters,
        then returns salted SHA-256 hash of the PN for privacy protection.

        Returns:
            tuple: (pn_hash, raw_pn, raw_registers_hex) or ("", "", "") on failure
        """
        import hashlib

        # Try up to 2 times (initial + 1 retry after reconnect)
        for attempt in range(2):
            try:
                # Check connection and try to reconnect if needed
                if not self._ensure_connection():
                    self._logger.info(
                        "Connection not available, attempting to connect..."
                    )
                    if not self.connect():
                        self._logger.warning(
                            "Connect failed on attempt %d", attempt + 1
                        )
                        continue

                result = self.client.read_input_registers(address=0x8000, count=5)
                if not result or result.isError():
                    self._logger.warning(
                        "Failed to read device PN registers: %s", result
                    )
                    return ("", "", "")

                registers = getattr(result, "registers", None) or getattr(
                    result, "data", None
                )
                if not registers:
                    self._logger.info("Device PN registers returned empty")
                    return ("", "", "")

                # Raw register data (hex format)
                raw_hex = " ".join([f"0x{r:04X}" for r in registers])

                # Decode as string (big endian)
                string_bytes = []
                for reg in registers:
                    string_bytes.append((reg >> 8) & 0xFF)
                    string_bytes.append(reg & 0xFF)

                device_pn_raw = (
                    bytes(string_bytes).decode("utf-8", errors="ignore").rstrip("\x00")
                )

                # Strip all spaces and null characters from the device PN
                device_pn = device_pn_raw.replace(" ", "").replace("\x00", "").strip()
                if not device_pn:
                    self._logger.info(
                        "Device PN is empty after cleaning, raw='%s'", device_pn_raw
                    )
                    return ("", device_pn_raw, raw_hex)

                # Success - reset error counter
                self._consecutive_errors = 0
                # Return salted SHA-256 hash of the PN for privacy protection
                # Salt prevents rainbow table attacks on short PN strings
                salt = "anker_solix_ha_2024"
                pn_hash = hashlib.sha256((salt + device_pn).encode()).hexdigest()
                return (pn_hash, device_pn, raw_hex)

            except (
                ConnectionError,
                OSError,
                TimeoutError,
                BrokenPipeError,
                ConnectionException,
                ModbusException,
            ) as e:
                # Handle connection errors - force disconnect and retry.
                # Not passing exc= to _handle_connection_error(): this
                # method already force-disconnects explicitly below on
                # every branch, so passing exc= would just trigger a
                # redundant second disconnect via the exception-type
                # check added for issue #81.
                error_msg = (
                    f"Connection error reading device PN (attempt {attempt + 1}): {e}"
                )
                self._handle_connection_error(error_msg)
                self._logger.info(error_msg)
                # Force disconnect before retry
                self._force_disconnect()
                if attempt == 0:
                    self._logger.info("Will retry after reconnect...")
                continue

            except Exception as e:
                self._logger.error(
                    "Unexpected exception reading device PN: %s", e, exc_info=True
                )
                return ("", "", "")

        # All attempts failed
        self._logger.error("Failed to read device PN after all attempts")
        return ("", "", "")

    def _format_modbus_frame(
        self, func_code: int, address: int, values: list[int], is_request: bool = True
    ) -> str:
        """Format Modbus frame for logging."""
        # Build Modbus TCP frame description (without MBAP header transaction ID)
        if func_code == 0x06:  # Write single register
            frame = f"[FC=0x{func_code:02X}(WriteSingleReg)] addr={address}(0x{address:04X}), val={values[0]}(0x{values[0]:04X})"
        elif func_code == 0x10:  # Write multiple registers
            hex_vals = " ".join(f"0x{v:04X}" for v in values)
            frame = f"[FC=0x{func_code:02X}(WriteMultiReg)] addr={address}(0x{address:04X}), count={len(values)}, vals=[{hex_vals}]"
        else:
            frame = f"[FC=0x{func_code:02X}] addr={address}(0x{address:04X})"
        return frame

    def _log_write_response(
        self, result, func_code: int, address: int, values: list[int]
    ) -> None:
        """Log write response details."""
        if hasattr(result, "isError") and result.isError():
            # Exception response
            exc_code = getattr(result, "exception_code", "N/A")
            exc_names = {
                1: "Illegal Function",
                2: "Illegal Data Address",
                3: "Illegal Data Value",
                4: "Slave Device Failure",
            }
            exc_name = exc_names.get(exc_code, "Unknown")
            raw_bytes = ""
            if hasattr(result, "encode"):
                try:
                    encoded = result.encode()
                    raw_bytes = " ".join(f"{b:02X}" for b in encoded)
                except Exception:
                    raw_bytes = str(result)
            self._logger.error(
                "RX Exception | FC=0x%02X, exc_code=%s(%s), raw_response=[%s], result=%s",
                func_code | 0x80,
                exc_code,
                exc_name,
                raw_bytes,
                result,
            )
        else:
            # Normal response
            if func_code == 0x06:
                self._logger.debug(
                    "RX OK | [FC=0x%02X(WriteSingleReg)] addr=%d(0x%04X) write success",
                    func_code,
                    address,
                    address,
                )
            elif func_code == 0x10:
                self._logger.debug(
                    "RX OK | [FC=0x%02X(WriteMultiReg)] addr=%d(0x%04X), count=%d write success",
                    func_code,
                    address,
                    address,
                    len(values),
                )

    def write_register(self, address: int, value: Any, data_type: str) -> WriteResult:
        """Write register (function code 06 / 16)."""
        # Check connection status with detailed logging
        is_connected = self._ensure_connection()
        socket_open = False
        try:
            if hasattr(self.client, "is_socket_open"):
                socket_open = self.client.is_socket_open()
            elif hasattr(self.client, "connected"):
                socket_open = self.client.connected
        except Exception:
            pass

        self._logger.debug(
            "Write register PRE-CHECK | address=%d (0x%04X), value=%s, data_type=%s, is_connected=%s, socket_open=%s",
            address,
            address,
            value,
            data_type,
            is_connected,
            socket_open,
        )

        if not is_connected:
            reason = f"Device not connected (is_connected={is_connected}, socket_open={socket_open})"
            self._logger.warning(
                "Unable to write register - not connected | "
                "[%s] device=%s:%d, address=%d (0x%04X), value=%s, data_type=%s, "
                "is_connected=%s, socket_open=%s",
                self.device_name,
                self.ip_address,
                self.port,
                address,
                address,
                value,
                data_type,
                is_connected,
                socket_open,
            )
            return WriteResult(success=False, error_reason=reason, is_transient=True)

        try:
            # Prepare raw register values for logging
            raw_registers = []
            func_code = 0x06  # Default: write single register

            if data_type == "UINT16":
                raw_registers = [int(value) & 0xFFFF]
                func_code = 0x06
                tx_frame = self._format_modbus_frame(func_code, address, raw_registers)
                self._logger.debug("TX | %s", tx_frame)
                result = self.client.write_register(address=address, value=int(value))
            elif data_type == "INT32":
                int_value = int(value)
                if int_value < 0:
                    int_value += 0x100000000
                high, low = (int_value >> 16) & 0xFFFF, int_value & 0xFFFF
                raw_registers = [high, low]
                func_code = 0x10
                tx_frame = self._format_modbus_frame(func_code, address, raw_registers)
                self._logger.debug(
                    "TX | %s (raw=%s, big-endian: high=0x%04X, low=0x%04X)",
                    tx_frame,
                    value,
                    high,
                    low,
                )
                result = self.client.write_registers(
                    address=address, values=[high, low]
                )
            elif data_type == "UINT32":
                int_value = int(value)
                high, low = (int_value >> 16) & 0xFFFF, int_value & 0xFFFF
                raw_registers = [high, low]
                func_code = 0x10
                tx_frame = self._format_modbus_frame(func_code, address, raw_registers)
                self._logger.debug(
                    "TX | %s (raw=%s, big-endian: high=0x%04X, low=0x%04X)",
                    tx_frame,
                    value,
                    high,
                    low,
                )
                result = self.client.write_registers(
                    address=address, values=[high, low]
                )
            else:
                raw_registers = [int(value) & 0xFFFF]
                func_code = 0x06
                tx_frame = self._format_modbus_frame(func_code, address, raw_registers)
                self._logger.debug("TX | %s", tx_frame)
                result = self.client.write_register(address=address, value=int(value))

            # Format raw registers for error logging
            raw_hex = " ".join([f"0x{r:04X}" for r in raw_registers])

            # Log response
            self._log_write_response(result, func_code, address, raw_registers)

            if result.isError():
                exc_code = getattr(result, "exception_code", None)
                exc_names = {
                    1: "Illegal Function",
                    2: "Illegal Data Address",
                    3: "Illegal Data Value",
                    4: "Slave Device Failure",
                }
                exc_name = exc_names.get(exc_code, "Unknown") if exc_code else ""
                raw_bytes = ""
                if hasattr(result, "encode"):
                    try:
                        encoded = result.encode()
                        raw_bytes = " ".join(f"{b:02X}" for b in encoded)
                    except Exception:
                        raw_bytes = str(result)
                if exc_code:
                    reason = f"Modbus exception: {exc_name} (code={exc_code})"
                else:
                    reason = f"Modbus error: {result}"
                self._logger.error(
                    "Write register FAILED | [%s] device=%s:%d, address=%d (0x%04X), value=%s, data_type=%s, raw_registers=[%s], error=%s",
                    self.device_name,
                    self.ip_address,
                    self.port,
                    address,
                    address,
                    value,
                    data_type,
                    raw_hex,
                    result,
                )
                return WriteResult(
                    success=False,
                    error_reason=reason,
                    raw_response=raw_bytes,
                    exception_code=exc_code,
                    exception_name=exc_name,
                    tx_frame=tx_frame,
                    is_transient=False,
                )

            self._logger.debug(
                "Write register SUCCESS | address=%d (0x%04X), value=%s, data_type=%s, raw_registers=[%s]",
                address,
                address,
                value,
                data_type,
                raw_hex,
            )
            return WriteResult(success=True, tx_frame=tx_frame)
        except Exception as e:
            # Catch ALL exceptions and check for "No response received"
            error_str = str(e)
            exception_type = type(e).__name__
            self._logger.warning(
                "Write register caught exception | [%s] device=%s:%d, type=%s, address=%d, value=%s, error=%s",
                self.device_name,
                self.ip_address,
                self.port,
                exception_type,
                address,
                value,
                error_str,
            )
            if "No response received" in error_str:
                self._logger.debug(
                    "📝 Write SUCCESS (device responded) | address=%d (0x%04X), value=%s, data_type=%s",
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
                "Write register EXCEPTION | address=%d (0x%04X), value=%s, data_type=%s, error=%s",
                address,
                address,
                value,
                data_type,
                e,
            )
            self._handle_connection_error(error_str, exc=e)
            return WriteResult(
                success=False, error_reason=f"{exception_type}: {error_str}", is_transient=True
            )

    def get_connection_info(self) -> dict[str, Any]:
        """Get connection information."""
        is_connected = False
        if hasattr(self.client, "connected"):
            is_connected = self.client.connected
        elif hasattr(self.client, "is_socket_open"):
            is_connected = self.client.is_socket_open()
        elif hasattr(self.client, "is_open"):
            is_connected = self.client.is_open()

        return {
            "ip_address": self.ip_address,
            "port": self.port,
            "status": self._connection_status,
            "protocol": "Modbus TCP",
            "connected": is_connected,
            "pymodbus_version": PYMODBUS_VERSION,
            "consecutive_errors": self._consecutive_errors,
        }

    def get_all_data(
        self,
        data_points: dict[str, Any] | None = None,
        batch_ranges: list[tuple[int, int, str]] | None = None,
        use_batch_optimization: bool = False,
    ) -> dict[str, Any]:
        """Batch read all data points.

        Args:
            data_points: Dictionary of data point configurations
            batch_ranges: Optional list of (start, end, register_type) tuples.
                register_type is "holding" (function code 03) or "input" (function code 04).
            use_batch_optimization: If True, use BatchRegisterReader for optimized
                reading (experimental)

        Returns:
            Dictionary of data point values
        """
        if data_points is None:
            self._logger.info("No data points provided, cannot read data")
            return {}

        self._logger.debug(
            "Starting batch read of %d data points (ranges=%d, optimization=%s)",
            len(data_points),
            len(batch_ranges) if batch_ranges else 0,
            use_batch_optimization,
        )

        # Log batch optimization efficiency if enabled
        if use_batch_optimization and not batch_ranges:
            efficiency = self._batch_reader.calculate_efficiency(data_points)
            self._logger.debug(
                "Batch read optimization: %d groups, %.1f%% efficiency (savings: %d registers)",
                efficiency["num_groups"],
                efficiency["efficiency_percent"],
                efficiency["savings"],
            )

        data: dict[str, Any] = {}
        successful_reads = 0
        failed_reads = 0

        self._last_failed_registers.clear()
        self._last_successful_registers.clear()

        range_data: dict[tuple[int, int], list[int]] = {}
        processed_keys = set()

        # Tracks whether a reconnect attempt already failed once during
        # this get_all_data() call. If the device is fully unreachable
        # (rather than just having a transient stale-buffer issue), every
        # subsequent range would otherwise also pay for a full reconnect
        # attempt, potentially stretching a single get_all_data() call
        # (and the operation lock it's called under) far beyond its
        # normal duration. Once tripped, remaining ranges skip the
        # retry-once path and fail immediately, same as before this fix.
        reconnect_exhausted = False

        if batch_ranges:
            # Sort by start address, keeping register type
            batch_ranges_sorted = sorted(batch_ranges, key=lambda x: x[0])
            for start, end, reg_type in batch_ranges_sorted:
                register_count = end - start + 1
                if register_count <= 0:
                    continue

                retried = False
                while True:
                    try:
                        # Drain any stale bytes left over from a previous
                        # cycle before issuing this request (issue #81).
                        self._flush_recv_buffer()
                        # Use appropriate function code based on register type
                        if reg_type == "holding":
                            result = self.client.read_holding_registers(
                                address=start, count=register_count
                            )
                        else:
                            result = self.client.read_input_registers(
                                address=start, count=register_count
                            )
                        break
                    except (
                        ConnectionError,
                        OSError,
                        TimeoutError,
                        ValueError,
                        ConnectionException,
                        ModbusException,
                    ) as exc:
                        self._handle_connection_error(
                            f"Exception reading configured range {start}-{end} ({reg_type}): {exc}",
                            exc=exc,
                        )
                        if retried or reconnect_exhausted:
                            for addr in range(start, end + 1):
                                self._last_failed_registers.add(addr)
                            result = None
                            break
                        retried = True
                        # One bounded reconnect + retry before giving up on
                        # this range (issue #81): a fresh TCP connection
                        # discards any transaction-id state the device may
                        # be out of sync with. `connect()` is a single,
                        # synchronous, non-recursive attempt bounded by its
                        # own connect timeout - it cannot loop or block
                        # indefinitely.
                        self._logger.info(
                            "Retrying range %d-%d (%s) once after reconnect",
                            start,
                            end,
                            reg_type,
                        )
                        self.disconnect()
                        if not self.connect():
                            reconnect_exhausted = True
                            for addr in range(start, end + 1):
                                self._last_failed_registers.add(addr)
                            result = None
                            break
                        # Loop back and retry the read exactly once.

                if result is None:
                    continue

                if not result or result.isError():
                    self._logger.info(
                        "Failed to read configured range %d-%d (%s): %s, trying individual reads",
                        start,
                        end,
                        reg_type,
                        result,
                    )
                    # Fallback: try reading each register individually
                    individual_reads = [None] * register_count
                    successful_individual = 0
                    
                    for addr in range(start, end + 1):
                        try:
                            if reg_type == "holding":
                                individual_result = self.client.read_holding_registers(
                                    address=addr, count=1
                                )
                            else:
                                individual_result = self.client.read_input_registers(
                                    address=addr, count=1
                                )
                            
                            if individual_result and not individual_result.isError():
                                individual_registers = getattr(individual_result, "registers", None) or getattr(
                                    individual_result, "data", None
                                )
                                if individual_registers:
                                    offset = addr - start
                                    individual_reads[offset] = individual_registers[0]
                                    successful_individual += 1
                                    self._last_successful_registers.add(addr)
                                    self._logger.debug(
                                        "Individual read successful: address=%d, value=%s",
                                        addr,
                                        individual_registers[0],
                                    )
                            else:
                                # Individual read failed - mark as unavailable
                                self._last_failed_registers.add(addr)
                                self._logger.debug(
                                    "Individual read failed for address %d: %s",
                                    addr,
                                    individual_result,
                                )
                        except Exception as individual_exc:
                            # Individual read failed - mark as unavailable
                            self._last_failed_registers.add(addr)
                            self._logger.debug(
                                "Individual read failed for address %d: %s",
                                addr,
                                individual_exc,
                            )
                    
                    # Only add to range_data if we got at least one successful read
                    if successful_individual > 0:
                        range_data[(start, end)] = individual_reads
                        self._logger.debug(
                            "Fallback individual reads: %d/%d successful for range %d-%d",
                            successful_individual,
                            register_count,
                            start,
                            end,
                        )
                    continue

                registers = getattr(result, "registers", None) or getattr(
                    result, "data", None
                )
                if not registers or len(registers) < register_count:
                    self._logger.error(
                        "Configured range %d-%d (%s) returned insufficient data: expected %d, got %d",
                        start,
                        end,
                        reg_type,
                        register_count,
                        len(registers) if registers else 0,
                    )
                    for addr in range(start, end + 1):
                        self._last_failed_registers.add(addr)
                    continue

                range_data[(start, end)] = registers
                for addr in range(start, end + 1):
                    self._last_successful_registers.add(addr)

        if use_batch_optimization and not batch_ranges:
            groups = self._batch_reader.group_data_points(data_points)
            for group in groups:
                try:
                    self._flush_recv_buffer()
                    result = self.client.read_input_registers(
                        address=group.start_address,
                        count=group.count,
                    )
                except (
                    ConnectionError,
                    OSError,
                    TimeoutError,
                    ValueError,
                    ConnectionException,
                    ModbusException,
                ) as exc:
                    self._handle_connection_error(
                        f"Exception reading register group starting at {group.start_address}: {exc}",
                        exc=exc,
                    )
                    for key, config in group.data_points:
                        data[key] = self._default_value(
                            config.get("data_type", "UINT16")
                        )
                        failed_reads += 1
                        self._last_failed_registers.add(int(config["address"]))
                    continue

                if not result or result.isError():
                    self._logger.error(
                        "Failed to read register group starting at %d: %s",
                        group.start_address,
                        result,
                    )
                    for key, config in group.data_points:
                        data[key] = self._default_value(
                            config.get("data_type", "UINT16")
                        )
                        failed_reads += 1
                        address = int(config["address"])
                        self._last_failed_registers.add(address)
                    continue

                registers = getattr(result, "registers", None) or getattr(
                    result, "data", None
                )
                if not registers or len(registers) < group.count:
                    self._logger.error(
                        "Register group %d-%d returned insufficient data: expected %d, got %d",
                        group.start_address,
                        group.end_address,
                        group.count,
                        len(registers) if registers else 0,
                    )
                    for key, config in group.data_points:
                        data[key] = self._default_value(
                            config.get("data_type", "UINT16")
                        )
                        failed_reads += 1
                        self._last_failed_registers.add(int(config["address"]))
                    continue

                for key, config in group.data_points:
                    processed_keys.add(key)
                    try:
                        address = int(config["address"])
                        dp_count = int(config.get("count", 1))
                        offset = address - group.start_address
                        slice_end = offset + dp_count

                        if offset < 0 or slice_end > len(registers):
                            raise IndexError(
                                "Data point %s exceeds group bounds: offset=%s, end=%s, len=%s"
                                % (key, offset, slice_end, len(registers))
                            )

                        dp_registers = registers[offset:slice_end]
                        value = self._decode_register_value(
                            address,
                            config["data_type"],
                            dp_registers,
                        )

                        if config.get("data_type") != "STRING" and config.get(
                            "gain"
                        ) not in (None, 1):
                            original_value = value
                            value = value / config["gain"]
                            self._logger.debug(
                                "Data point %s (batch): address=%d, raw_value=%s, "
                                "gain=%s, final_value=%s",
                                key,
                                address,
                                original_value,
                                config["gain"],
                                value,
                            )
                        else:
                            self._logger.debug(
                                "Data point %s (batch): address=%d, value=%s",
                                key,
                                address,
                                value,
                            )

                        data[key] = value
                        successful_reads += 1
                        self._last_successful_registers.add(address)
                    except Exception as exc:
                        # Not passing exc= here: this branch decodes an already
                        # fetched register value (unit conversion, gain, etc.),
                        # not a socket-level read. A decode error is a data
                        # problem, not a connection problem — forcing a
                        # reconnect here would be incorrect and would not fix
                        # the underlying malformed data.
                        self._handle_connection_error(
                            f"Exception decoding batch data point {key}: {exc}"
                        )
                        data[key] = self._default_value(
                            config.get("data_type", "UINT16")
                        )
                        failed_reads += 1
                        with contextlib.suppress(KeyError, ValueError, TypeError):
                            self._last_failed_registers.add(int(config["address"]))
                        self._logger.debug(
                            "Failed to decode batch data point %s: %s", key, exc
                        )

        for key, config in data_points.items():
            if key in processed_keys:
                continue
            try:
                address = int(config["address"])
                count = int(config.get("count", 1))
            except (KeyError, TypeError, ValueError):
                # Do not write a default value here: leaving the key absent
                # lets downstream consumers (e.g. capability_entity checks)
                # distinguish "never read" (key missing) from "read but
                # decoded to 0".
                failed_reads += 1
                self._logger.debug(
                    "Invalid configuration for data point %s: %s", key, config
                )
                continue

            range_entry = None

            for (start, end), registers in range_data.items():
                if start <= address and address + count - 1 <= end:
                    range_entry = (start, end, registers)
                    break

            try:
                if not range_entry:
                    self._logger.debug(
                        "Skipping data point %s: address %d (count %d) outside configured batch ranges",
                        key,
                        address,
                        count,
                    )
                    continue

                start, end, registers = range_entry
                offset = address - start
                slice_end = offset + count
                dp_registers = registers[offset:slice_end]
                value = self._decode_register_value(
                    address, config["data_type"], dp_registers
                )
                self._logger.debug(
                    "Data point %s (configured range %d-%d): offset=%d, value=%s",
                    key,
                    start,
                    end,
                    offset,
                    value,
                )
                if config.get("data_type") != "STRING" and config.get("gain") not in (
                    None,
                    1,
                ):
                    original_value = value
                    value = value / config["gain"]
                    self._logger.debug(
                        "Data point %s: address=%d, raw_value=%s, gain=%s, final_value=%s",
                        key,
                        config["address"],
                        original_value,
                        config["gain"],
                        value,
                    )
                else:
                    self._logger.debug(
                        "Data point %s: address=%d, value=%s",
                        key,
                        config["address"],
                        value,
                    )
                data[key] = value
                successful_reads += 1
                self._last_successful_registers.add(address)
            except (
                IndexError,
                KeyError,
                ValueError,
                TypeError,
                _RegisterDecodeError,
            ) as e:
                data[key] = self._default_value(config.get("data_type", "UINT16"))
                failed_reads += 1
                self._last_failed_registers.add(address)
                self._logger.debug(
                    "Failed to decode data point %s from configured range: %s", key, e
                )

        self._last_successful_registers -= self._last_failed_registers

        if failed_reads:
            self._logger.info(
                "Batch read completed with partial failures: %d successful, %d failed",
                successful_reads,
                failed_reads,
            )
        else:
            self._logger.debug(
                "Batch read completed successfully (%d points)",
                successful_reads,
            )
        return data

    def _has_garbled_text(self, text: str) -> bool:
        """Detect if text contains garbled characters."""
        if not text:
            return False

        # Check if contains non-ASCII characters or control characters (except common whitespace)
        for char in text:
            if ord(char) < 32 and char not in "\t\n\r":
                return True
            if ord(char) > 126:
                return True

        # Check if contains too many special characters
        special_chars = sum(1 for c in text if not c.isalnum() and c not in " -_.")
        if special_chars > len(text) * 0.3:  # If special characters exceed 30%
            return True

        return False

    def __del__(self):
        """Destructor, ensure connection is properly closed."""
        try:
            if hasattr(self, "client") and hasattr(self.client, "close"):
                self.client.close()
        except (OSError, AttributeError):
            pass


# Compatible with old name
# Backward compatibility alias
VirtualModbusDevice = AnkerSolixModbusClient
