"""Anker Solix TCP communication module."""

import asyncio
import contextlib
import logging
import time
from typing import Any

import pymodbus
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import (
    ConnectionException,
    ModbusException,
)

from .batch_reader import BatchRegisterReader
from .const import MODBUS_RESPONSE_TIMEOUT
from .device_logger import WriteResult

MODBUS_RETRIES = 3

# pymodbus >= 3.11.1 appends its last 20 raw frames to every ERROR it emits
# (pymodbus.logging.Log.error + Log.transport_dump), turning one request
# timeout into ~21 lines of hex. Stripping the dump keeps the level itself
# under Home Assistant's control, so a user who deliberately sets pymodbus to
# DEBUG still gets the per-frame trace they asked for.
_PYMODBUS_FRAME_DUMP_MARKER = "\n>>>>> "

# pymodbus reports a plain "device is offline" as WARNING/ERROR on every single
# retry, with no throttling. For a local_polling integration that reconnects
# forever, an unreachable device is an expected state, not an anomaly -- and we
# already report it ourselves through _handle_connection_error(), which is
# throttled (ERROR once per episode, then WARNING at most every 30s) and names
# the register range that failed. Left alone, one offline device emits ~48
# duplicate pymodbus WARNINGs per 15 minutes and buries the genuine faults.
#
# Demoting to DEBUG (never dropping) keeps the full retry trace available to
# anyone who sets pymodbus to DEBUG, while a normal log shows only our own
# throttled summary. Matched by prefix against the exact emitting call sites in
# pymodbus 3.11.1:
#   - transport/transport.py:250  Log.warning("Failed to connect {}", exc)
#       raised only for TimeoutError/OSError, and returns False rather than
#       raising, so our _ensure_connected_locked() always sees and reports it.
#   - transaction/transaction.py:151,199  "No response received after N retries"
#       surfaces to us as ModbusIOException and is re-logged with the register
#       range by _handle_connection_error().
#   - logging.py:152  "Repeating...."  pymodbus's own dedup placeholder,
#       carries no information at all.
#
# Deliberately NOT demoted: "ERROR: request ask for transaction_id=.. but got
# id=.." (framer/base.py:84) and the device-id variant. Those signal protocol
# desync rather than an offline device, and _should_disconnect_for() relies on
# them to force a reconnect, so they must stay visible at their original level.
_PYMODBUS_EXPECTED_OFFLINE_PREFIXES = (
    "Failed to connect",
    "No response received after",
    "Repeating....",
)


class _StripPymodbusFrameDump(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Log.error() pre-formats the full string into record.msg with no args,
        # so truncating msg is lossless for the human-readable part.
        msg = record.msg
        if isinstance(msg, str) and _PYMODBUS_FRAME_DUMP_MARKER in msg:
            msg = msg.partition(_PYMODBUS_FRAME_DUMP_MARKER)[0]
            record.msg = msg
        if (
            record.levelno > logging.DEBUG
            and isinstance(msg, str)
            and msg.startswith(_PYMODBUS_EXPECTED_OFFLINE_PREFIXES)
        ):
            # levelname must be updated alongside levelno: HA's log formatter
            # and the WARNING-collecting "system log" panel both read the name,
            # so leaving it stale would still surface these as warnings there.
            record.levelno = logging.DEBUG
            record.levelname = "DEBUG"
        return True


def _install_pymodbus_log_filter() -> None:
    # Must be "pymodbus.logging", the logger Log.error() emits on: a filter on
    # the "pymodbus" parent would never see records propagating up from it.
    logger = logging.getLogger("pymodbus.logging")
    if any(isinstance(f, _StripPymodbusFrameDump) for f in logger.filters):
        return
    logger.addFilter(_StripPymodbusFrameDump())


_install_pymodbus_log_filter()


class _RegisterDecodeError(Exception):
    """Malformed register data."""


class AnkerSolixModbusClient:
    """Anker Solix TCP client class (async)."""

    def __init__(
        self,
        ip_address: str = "127.0.0.1",
        port: int = 502,
        device_name: str | None = None,
    ):
        """Initialize Modbus TCP client."""
        self.ip_address = ip_address
        self.port = port
        self.device_name = device_name or f"{ip_address}:{port}"
        self.client: AsyncModbusTcpClient | None = None
        self._logger = logging.getLogger(__name__)
        self._logger.debug(
            "Using pymodbus version: %s for device %s",
            pymodbus.__version__,
            self.device_name,
        )

        self._connection_status = "disconnected"
        self._consecutive_errors = 0
        self._last_error_log_time = 0
        self._error_log_interval = 30
        self._error_count_since_last_log = 0

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

    def _create_client(self) -> AsyncModbusTcpClient:
        """Create the persistent pymodbus async client.

        reconnect_delay=0 disables pymodbus' background auto-reconnect task:
        reconnection is driven exclusively by this integration under the
        manager's I/O lock, so the two can never race on the same socket
        (pymodbus connect() has no reentrancy protection).
        """
        return AsyncModbusTcpClient(
            host=self.ip_address,
            port=self.port,
            timeout=MODBUS_RESPONSE_TIMEOUT,
            retries=MODBUS_RETRIES,
            reconnect_delay=0,
        )

    def is_connected(self) -> bool:
        """Return current transport state without triggering I/O."""
        try:
            return bool(self.client and self.client.connected)
        except Exception:
            return False

    async def connect(self) -> bool:
        """Connect to Modbus device."""
        try:
            if self.client is None:
                self.client = self._create_client()

            if self.client.connected:
                self._connection_status = "connected"
                return True

            if await self.client.connect():
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
        except (ConnectionError, OSError, asyncio.TimeoutError) as e:
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

    async def disconnect(self):
        """Disconnect."""
        try:
            if self.client is not None:
                self.client.close()
            self._connection_status = "disconnected"
            self._logger.debug(
                "Disconnected from Modbus %s:%d", self.ip_address, self.port
            )
        except (OSError, AttributeError) as e:
            self._logger.error("Error during disconnect: %s", e)

    def _handle_io_success(self) -> None:
        """Close the current failure episode after any successful Modbus I/O.

        Without this, _consecutive_errors only ever grows, so the "first error"
        ERROR log below fires once per process and every later outage is
        reported at INFO/DEBUG only -- a device can be unreachable for 20+
        minutes without a single ERROR in the log.
        """
        if self._consecutive_errors:
            self._logger.debug(
                "Modbus I/O recovered after %d consecutive errors",
                self._consecutive_errors,
            )
        self._consecutive_errors = 0
        self._error_count_since_last_log = 0

    @staticmethod
    def _should_disconnect_for(exc: BaseException) -> bool:
        """Whether this error means the socket must be torn down.

        pymodbus already runs its own failure budget: a request that gets no
        answer is retried `retries` times, and only after retries+3 such
        requests in a row does it close the connection itself. Treating the
        first "no response" as fatal fights that budget -- it turns one slow
        range into a full reconnect for every following range, and a momentary
        hiccup into all entities going unavailable.

        Desync is the opposite case and must disconnect: after a transaction or
        device ID mismatch the stream is misaligned, so every later reply is
        read against the wrong request until the socket is replaced (issue #81).
        """
        if isinstance(exc, (ConnectionError, OSError, asyncio.TimeoutError)):
            return True
        if isinstance(exc, ConnectionException):
            return True
        if isinstance(exc, ModbusException):
            return "transaction id" in str(exc) or "device id" in str(exc)
        return False

    @staticmethod
    def _is_covered_by_batch_ranges(
        address: int,
        count: int,
        batch_ranges: list[tuple[int, int, str]] | None,
    ) -> bool:
        """Whether a configured batch range spans this data point.

        Separates "the range was configured but failed this cycle" from "this
        address is deliberately outside every range". The failed-range case has
        already paid for per-register retries inside the batch loop, so reading
        it again here would repeat every failure once more per cycle.
        """
        if not batch_ranges:
            return False
        last = address + count - 1
        return any(
            start <= address and last <= end for start, end, _ in batch_ranges
        )

    async def _read_single_data_point(
        self, address: int, count: int, reg_type: str
    ) -> list[int] | None:
        """Read one data point that sits outside every batch range.

        Returns the registers on success, or None when the device rejects the
        address. Keeping the two outcomes distinguishable is the whole point:
        firmware without 0x8007 answers Illegal Data Address here, whereas a
        batch spanning that address silently appends a zero, which a capability
        gate cannot tell apart from a device reporting "no features".
        """
        try:
            if reg_type == "holding":
                result = await self.client.read_holding_registers(
                    address=address, count=count
                )
            else:
                result = await self.client.read_input_registers(
                    address=address, count=count
                )
        except (
            ConnectionError,
            OSError,
            asyncio.TimeoutError,
            ValueError,
            ConnectionException,
            ModbusException,
        ) as exc:
            self._handle_connection_error(
                f"Exception reading out-of-range address {address} ({reg_type}): {exc}",
                exc=exc,
            )
            return None

        if not result or result.isError():
            self._logger.debug(
                "Out-of-range read failed for address %d (0x%04X, %s): %s",
                address,
                address,
                reg_type,
                result,
            )
            return None

        registers = getattr(result, "registers", None) or getattr(
            result, "data", None
        )
        if not registers or len(registers) < count:
            self._logger.debug(
                "Out-of-range read for address %d (0x%04X) returned insufficient "
                "data: expected %d, got %d",
                address,
                address,
                count,
                len(registers) if registers else 0,
            )
            return None

        self._logger.debug(
            "Out-of-range read successful: address=%d (0x%04X, %s), registers=%s",
            address,
            address,
            reg_type,
            registers[:count],
        )
        return list(registers[:count])

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
                ``error_msg``. Connection-level errors (``ConnectionException``,
                ``ConnectionError``, ``OSError``, ``TimeoutError``) and
                protocol desync (transaction/device ID mismatch) warrant a
                disconnect: the transport may be holding stale TCP state that
                would otherwise be silently reused on the next call (issue #81).
                A plain "no response" ``ModbusIOException`` does not -- see
                ``_should_disconnect_for``.
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

        if not should_disconnect and exc is not None and self._should_disconnect_for(exc):
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

        # First error of each failure episode (the counter is reset by
        # _handle_io_success), so a later outage is reported at ERROR too.
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
                self._logger.warning(log_message)

            self._last_error_log_time = current_time
            self._error_count_since_last_log = 0

    def _force_disconnect(self):
        """Force disconnect and clean up resources."""
        try:
            if self.client is not None:
                self.client.close()
            self._connection_status = "disconnected"
            self._logger.debug(
                "Force disconnected Modbus connection %s:%d", self.ip_address, self.port
            )
        except (OSError, AttributeError) as e:
            self._logger.debug("Exception during disconnect: %s", e)

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

    async def read_register(
        self, address: int, data_type: str, count: int = None
    ) -> Any | None:
        """Read input register (function code 04)."""
        if not self.is_connected():
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
            result = await self.client.read_input_registers(address=address, count=count)

            if not result or result.isError():
                self._logger.error("Failed to read register %d: %s", address, result)
                return None

            registers = getattr(result, "registers", None) or getattr(
                result, "data", None
            )
            value = self._decode_register_value(address, data_type, registers[:count])
            self._handle_io_success()
            return value
        except _RegisterDecodeError as e:
            self._handle_connection_error(
                f"Exception reading register {address}: {e}", exc=e
            )
            return None
        except (
            ConnectionError,
            OSError,
            asyncio.TimeoutError,
            ValueError,
            ModbusException,
        ) as e:
            self._handle_connection_error(
                f"Exception reading register {address}: {e}", exc=e
            )
            return None

    async def read_device_pn(self) -> tuple[str, str, str]:
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
                if not self.is_connected():
                    self._logger.info(
                        "Connection not available, attempting to connect..."
                    )
                    if not await self.connect():
                        self._logger.warning(
                            "Connect failed on attempt %d", attempt + 1
                        )
                        continue

                result = await self.client.read_input_registers(address=0x8000, count=5)
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
                self._handle_io_success()
                # Return salted SHA-256 hash of the PN for privacy protection
                # Salt prevents rainbow table attacks on short PN strings
                salt = "anker_solix_ha_2024"
                pn_hash = hashlib.sha256((salt + device_pn).encode()).hexdigest()
                return (pn_hash, device_pn, raw_hex)

            except (
                ConnectionError,
                OSError,
                asyncio.TimeoutError,
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

    async def write_register(self, address: int, value: Any, data_type: str) -> WriteResult:
        """Write register (function code 06 / 16)."""
        # Check connection status with detailed logging
        is_connected = self.is_connected()

        self._logger.debug(
            "Write register PRE-CHECK | address=%d (0x%04X), value=%s, data_type=%s, is_connected=%s",
            address,
            address,
            value,
            data_type,
            is_connected,
        )

        if not is_connected:
            reason = f"Device not connected (is_connected={is_connected})"
            self._logger.warning(
                "Unable to write register - not connected | "
                "[%s] device=%s:%d, address=%d (0x%04X), value=%s, data_type=%s, "
                "is_connected=%s",
                self.device_name,
                self.ip_address,
                self.port,
                address,
                address,
                value,
                data_type,
                is_connected,
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
                result = await self.client.write_register(address=address, value=int(value))
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
                result = await self.client.write_registers(
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
                result = await self.client.write_registers(
                    address=address, values=[high, low]
                )
            else:
                raw_registers = [int(value) & 0xFFFF]
                func_code = 0x06
                tx_frame = self._format_modbus_frame(func_code, address, raw_registers)
                self._logger.debug("TX | %s", tx_frame)
                result = await self.client.write_register(address=address, value=int(value))

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
            self._handle_io_success()
            return WriteResult(success=True, tx_frame=tx_frame)
        except Exception as e:
            # Catch ALL exceptions; connection-level ones trigger a disconnect
            # so the next operation starts from a clean transport.
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
        return {
            "ip_address": self.ip_address,
            "port": self.port,
            "status": self._connection_status,
            "protocol": "Modbus TCP",
            "connected": self.is_connected(),
            "pymodbus_version": pymodbus.__version__,
            "consecutive_errors": self._consecutive_errors,
        }

    async def get_all_data(
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

        # Bounds how long one get_all_data() can hold the manager's I/O lock.
        # A device that accepts TCP but stops answering Modbus is the expensive
        # case: connect() keeps succeeding, so relying on connect failure alone
        # would let all 7 ranges pay timeout x (1+retries) twice each (~4.7 min
        # of lock hold, with entity writes queued behind it). Failing the same
        # range twice across a fresh connection already proves the device is not
        # answering, so the rest of the sweep is abandoned and the registers are
        # marked unavailable; the next poll re-probes from scratch.
        device_unresponsive = False

        if batch_ranges:
            # Sort by start address, keeping register type
            batch_ranges_sorted = sorted(batch_ranges, key=lambda x: x[0])
            for start, end, reg_type in batch_ranges_sorted:
                register_count = end - start + 1
                if register_count <= 0:
                    continue

                if device_unresponsive:
                    for addr in range(start, end + 1):
                        self._last_failed_registers.add(addr)
                    continue

                retried = False
                while True:
                    try:
                        # Use appropriate function code based on register type
                        if reg_type == "holding":
                            result = await self.client.read_holding_registers(
                                address=start, count=register_count
                            )
                        else:
                            result = await self.client.read_input_registers(
                                address=start, count=register_count
                            )
                        break
                    except (
                        ConnectionError,
                        OSError,
                        asyncio.TimeoutError,
                        ValueError,
                        ConnectionException,
                        ModbusException,
                    ) as exc:
                        self._handle_connection_error(
                            f"Exception reading configured range {start}-{end} ({reg_type}): {exc}",
                            exc=exc,
                        )
                        if retried:
                            device_unresponsive = True
                            self._logger.info(
                                "Range %d-%d (%s) failed twice across a reconnect, "
                                "abandoning remaining ranges this cycle",
                                start,
                                end,
                                reg_type,
                            )
                            for addr in range(start, end + 1):
                                self._last_failed_registers.add(addr)
                            result = None
                            break
                        retried = True
                        # One bounded reconnect + retry before giving up on
                        # this range (issue #81): a fresh TCP connection
                        # discards any transaction-id state the device may
                        # be out of sync with.
                        self._logger.info(
                            "Retrying range %d-%d (%s) once after reconnect",
                            start,
                            end,
                            reg_type,
                        )
                        await self.disconnect()
                        if not await self.connect():
                            device_unresponsive = True
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
                                individual_result = await self.client.read_holding_registers(
                                    address=addr, count=1
                                )
                            else:
                                individual_result = await self.client.read_input_registers(
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
                    result = await self.client.read_input_registers(
                        address=group.start_address,
                        count=group.count,
                    )
                except (
                    ConnectionError,
                    OSError,
                    asyncio.TimeoutError,
                    ValueError,
                    ConnectionException,
                    ModbusException,
                ) as exc:
                    self._handle_connection_error(
                        f"Exception reading register group starting at {group.start_address}: {exc}",
                        exc=exc,
                    )
                    for key, config in group.data_points:
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
                        # No default value: an absent key means "not read", which
                        # capability gates must not confuse with "device reported
                        # 0" (a 0 mask legitimately hides entities).
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
                    if self._is_covered_by_batch_ranges(
                        address, count, batch_ranges
                    ):
                        failed_reads += 1
                        self._last_failed_registers.add(address)
                        self._logger.debug(
                            "Data point %s: address %d (0x%04X) is inside a "
                            "configured batch range that failed this cycle",
                            key,
                            address,
                            address,
                        )
                        continue

                    reg_type = config.get("register_type")
                    if reg_type not in ("input", "holding"):
                        failed_reads += 1
                        self._last_failed_registers.add(address)
                        self._logger.debug(
                            "Data point %s: address %d (0x%04X) is outside every "
                            "configured batch range and has no valid "
                            "register_type (got %r), cannot read it",
                            key,
                            address,
                            address,
                            reg_type,
                        )
                        continue

                    if device_unresponsive:
                        failed_reads += 1
                        self._last_failed_registers.add(address)
                        continue

                    single_registers = await self._read_single_data_point(
                        address, count, reg_type
                    )
                    if single_registers is None:
                        failed_reads += 1
                        self._last_failed_registers.add(address)
                        continue

                    start = address
                    end = address + count - 1
                    registers = single_registers
                    source = f"single read ({reg_type})"
                else:
                    start, end, registers = range_entry
                    source = "configured range"

                offset = address - start
                slice_end = offset + count
                dp_registers = registers[offset:slice_end]
                value = self._decode_register_value(
                    address, config["data_type"], dp_registers
                )
                self._logger.debug(
                    "Data point %s (%s %d-%d): offset=%d, value=%s",
                    key,
                    source,
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
                # No default value: an absent key means "not read", which
                # capability gates must not confuse with "device reported 0".
                # 0x8007 answers Illegal Data Address on some firmware, and
                # writing 0 here made an unreadable mask look like a device
                # that genuinely reports "feature unsupported", hiding
                # Backup Reserve / Charging Limit.
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
        if successful_reads and not self._last_failed_registers:
            self._handle_io_success()
        return data
