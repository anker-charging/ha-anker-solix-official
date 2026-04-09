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
        self._logger.info(
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
                self._logger.info(
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
            self._logger.info(
                "Disconnected from Modbus %s:%d", self.ip_address, self.port
            )
        except (OSError, AttributeError) as e:
            self._logger.error("Error during disconnect: %s", e)

    def _handle_connection_error(self, error_msg: str = ""):
        """Handle connection error and update error tracking with throttled logging."""
        current_time = time.time()
        self._consecutive_errors += 1
        self._connection_status = "error"
        self._error_count_since_last_log += 1

        # Check if it's a Broken pipe error, if so disconnect immediately
        if "Broken pipe" in error_msg or "Connection reset" in error_msg:
            self._logger.debug(
                "Detected connection disconnection error, disconnecting immediately: %s",
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
                self._logger.warning(log_message)

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

    def _default_value(self, data_type: str) -> Any:
        """Return default fallback value for a data type."""
        return "" if data_type in ("STRING", "VERSION") else 0

    def _decode_register_value(
        self, address: int, data_type: str, registers: list[int]
    ) -> Any:
        """Decode register list into the correct Python value."""
        if not registers:
            self._logger.error("Register %d returned no data", address)
            return self._default_value(data_type)

        try:
            if data_type == "UINT16":
                value = registers[0]
            elif data_type == "INT32":
                if len(registers) < 2:
                    self._logger.warning(
                        "Register %d requires 2 values for INT32, got %d",
                        address,
                        len(registers),
                    )
                    return self._default_value(data_type)
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
                    self._logger.warning(
                        "Register %d requires 2 values for UINT32, got %d",
                        address,
                        len(registers),
                    )
                    return self._default_value(data_type)
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
                    self._logger.warning(
                        "String decoding failed for address %d: %s", address, err
                    )
                    value = ""
            else:
                value = registers[0]

            self._logger.debug(
                "Decoded register %d -> %s (%s)", address, value, data_type
            )
            return value
        except Exception as err:
            self._logger.warning(
                "Failed to decode register %d (%s): %s", address, data_type, err
            )
            return self._default_value(data_type)

    def read_register(self, address: int, data_type: str, count: int = None) -> Any:
        """Read input register (function code 04)."""
        if not self._ensure_connection():
            self._logger.warning("Unable to read register %d, not connected", address)
            return 0

        if count is None:
            count = (
                1
                if data_type == "UINT16"
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
                return 0

            registers = getattr(result, "registers", None) or getattr(
                result, "data", None
            )
            value = self._decode_register_value(address, data_type, registers[:count])
            return value
        except (ConnectionError, OSError, TimeoutError, ValueError) as e:
            # Use new error handling method
            self._handle_connection_error(f"Exception reading register {address}: {e}")
            return 0

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
                    self._logger.warning("Device PN registers returned empty")
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
                    self._logger.warning(
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

            except (ConnectionError, OSError, TimeoutError, BrokenPipeError) as e:
                # Handle connection errors - force disconnect and retry
                error_msg = (
                    f"Connection error reading device PN (attempt {attempt + 1}): {e}"
                )
                self._handle_connection_error(error_msg)
                self._logger.warning(error_msg)
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
                self._logger.warning(
                    "RX OK | [FC=0x%02X(WriteSingleReg)] addr=%d(0x%04X) write success",
                    func_code,
                    address,
                    address,
                )
            elif func_code == 0x10:
                self._logger.warning(
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

        self._logger.warning(
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
            return WriteResult(success=False, error_reason=reason)

        try:
            # Prepare raw register values for logging
            raw_registers = []
            func_code = 0x06  # Default: write single register

            if data_type == "UINT16":
                raw_registers = [int(value) & 0xFFFF]
                func_code = 0x06
                tx_frame = self._format_modbus_frame(func_code, address, raw_registers)
                self._logger.warning("TX | %s", tx_frame)
                result = self.client.write_register(address=address, value=int(value))
            elif data_type == "INT32":
                int_value = int(value)
                if int_value < 0:
                    int_value += 0x100000000
                high, low = (int_value >> 16) & 0xFFFF, int_value & 0xFFFF
                raw_registers = [high, low]
                func_code = 0x10
                tx_frame = self._format_modbus_frame(func_code, address, raw_registers)
                self._logger.warning(
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
                self._logger.warning(
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
                self._logger.warning("TX | %s", tx_frame)
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
                )

            self._logger.warning(
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
                self._logger.warning(
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
            self._handle_connection_error(error_str)
            return WriteResult(
                success=False, error_reason=f"{exception_type}: {error_str}"
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
            self._logger.warning("No data points provided, cannot read data")
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

        range_data: dict[tuple[int, int], list[int]] = {}
        processed_keys = set()

        if batch_ranges:
            # Sort by start address, keeping register type
            batch_ranges_sorted = sorted(batch_ranges, key=lambda x: x[0])
            for start, end, reg_type in batch_ranges_sorted:
                try:
                    register_count = end - start + 1
                    if register_count <= 0:
                        continue
                    # Use appropriate function code based on register type
                    if reg_type == "holding":
                        result = self.client.read_holding_registers(
                            address=start, count=register_count
                        )
                    else:
                        result = self.client.read_input_registers(
                            address=start, count=register_count
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
                        f"Exception reading configured range {start}-{end} ({reg_type}): {exc}"
                    )
                    continue

                if not result or result.isError():
                    self._logger.error(
                        "Failed to read configured range %d-%d (%s): %s",
                        start,
                        end,
                        reg_type,
                        result,
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
                    continue

                range_data[(start, end)] = registers

        if use_batch_optimization and not batch_ranges:
            groups = self._batch_reader.group_data_points(data_points)
            for group in groups:
                try:
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
                        f"Exception reading register group starting at {group.start_address}: {exc}"
                    )
                    for key, config in group.data_points:
                        data[key] = self._default_value(
                            config.get("data_type", "UINT16")
                        )
                        failed_reads += 1
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
                    except Exception as exc:
                        self._handle_connection_error(
                            f"Exception decoding batch data point {key}: {exc}"
                        )
                        data[key] = self._default_value(
                            config.get("data_type", "UINT16")
                        )
                        failed_reads += 1
                        self._logger.warning(
                            "Failed to decode batch data point %s: %s", key, exc
                        )

        for key, config in data_points.items():
            if key in processed_keys:
                continue
            try:
                address = int(config["address"])
                count = int(config.get("count", 1))
            except (KeyError, TypeError, ValueError):
                data[key] = self._default_value(config.get("data_type", "UINT16"))
                failed_reads += 1
                self._logger.warning(
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
            except (IndexError, KeyError, ValueError, TypeError) as e:
                data[key] = self._default_value(config.get("data_type", "UINT16"))
                failed_reads += 1
                self._logger.warning(
                    "Failed to decode data point %s from configured range: %s", key, e
                )

        if failed_reads:
            self._logger.warning(
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
