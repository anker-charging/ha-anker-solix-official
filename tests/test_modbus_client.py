"""Unit tests for AnkerSolixModbusClient (modbus_client.py).

The underlying pymodbus AsyncModbusTcpClient is replaced with a lightweight
fake (`_FakePymodbusClient`) rather than a real TCP socket: this module is
pure protocol/decoding/error-handling logic layered on top of pymodbus, and
a fake response object lets every branch (success, protocol exception,
transport exception, insufficient data) be driven deterministically without
any real I/O.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
from pymodbus.exceptions import ConnectionException, ModbusException

from custom_components.anker_solix_official.modbus_client import (
    AnkerSolixModbusClient,
    _RegisterDecodeError,
)


@dataclass
class _FakeResponse:
    """Stand-in for a pymodbus response/exception-response object."""

    registers: list[int] | None = None
    error: bool = False

    def isError(self) -> bool:  # noqa: N802 - matches pymodbus's own casing
        return self.error


@dataclass
class _FakePymodbusClient:
    """Stand-in for pymodbus AsyncModbusTcpClient, scripted per test."""

    connected: bool = False
    connect_result: bool = True
    connect_side_effect: BaseException | None = None
    read_input_registers_queue: list[Any] = field(default_factory=list)
    read_holding_registers_queue: list[Any] = field(default_factory=list)
    write_register_result: Any = None
    write_registers_result: Any = None
    write_side_effect: BaseException | None = None
    closed: bool = False

    async def connect(self) -> bool:
        if self.connect_side_effect:
            raise self.connect_side_effect
        self.connected = self.connect_result
        return self.connect_result

    def close(self) -> None:
        self.closed = True
        self.connected = False

    async def read_input_registers(self, address: int, count: int) -> Any:
        return self._pop(self.read_input_registers_queue)

    async def read_holding_registers(self, address: int, count: int) -> Any:
        return self._pop(self.read_holding_registers_queue)

    async def write_register(self, address: int, value: int) -> Any:
        if self.write_side_effect:
            raise self.write_side_effect
        return self.write_register_result

    async def write_registers(self, address: int, values: list[int]) -> Any:
        if self.write_side_effect:
            raise self.write_side_effect
        return self.write_registers_result

    @staticmethod
    def _pop(queue: list[Any]) -> Any:
        if not queue:
            return _FakeResponse(registers=[0])
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _make_client(fake: _FakePymodbusClient | None = None) -> AnkerSolixModbusClient:
    """Build a client with its pymodbus client pre-injected, skipping connect()."""
    client = AnkerSolixModbusClient(ip_address="127.0.0.1", port=502)
    client.client = fake or _FakePymodbusClient(connected=True)
    client._connection_status = "connected"
    return client


class TestIsConnected:
    """is_connected() transport-state passthrough."""

    def test_no_client_returns_false(self) -> None:
        client = AnkerSolixModbusClient()
        assert client.is_connected() is False

    def test_connected_client_returns_true(self) -> None:
        client = _make_client(_FakePymodbusClient(connected=True))
        assert client.is_connected() is True

    def test_disconnected_client_returns_false(self) -> None:
        client = _make_client(_FakePymodbusClient(connected=False))
        assert client.is_connected() is False

    def test_client_raising_on_connected_property_returns_false(self) -> None:
        # Arrange: a client whose `.connected` access itself raises must not
        # propagate — is_connected() is meant to be a safe, exception-free probe.
        class _Explodes:
            @property
            def connected(self) -> bool:
                raise RuntimeError("boom")

        client = AnkerSolixModbusClient()
        client.client = _Explodes()

        # Act & Assert
        assert client.is_connected() is False


class TestConnect:
    """connect() lifecycle and status tracking."""

    async def test_successful_connect_sets_connected_status(self) -> None:
        # Arrange
        client = AnkerSolixModbusClient()
        client.client = _FakePymodbusClient(connected=False, connect_result=True)

        # Act
        result = await client.connect()

        # Assert
        assert result is True
        assert client._connection_status == "connected"

    async def test_already_connected_client_short_circuits(self) -> None:
        # Arrange: client.connected is already True before connect() runs.
        client = AnkerSolixModbusClient()
        fake = _FakePymodbusClient(connected=True)
        client.client = fake

        # Act
        result = await client.connect()

        # Assert: connect() on the underlying transport was never invoked
        # (no side effect to check directly here, but status still updates).
        assert result is True
        assert client._connection_status == "connected"

    async def test_failed_connect_sets_connection_failed_status(self) -> None:
        # Arrange
        client = AnkerSolixModbusClient()
        client.client = _FakePymodbusClient(connected=False, connect_result=False)

        # Act
        result = await client.connect()

        # Assert
        assert result is False
        assert client._connection_status == "connection_failed"

    async def test_connect_creates_client_lazily_when_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: client.client starts as None; connect() must create it via
        # _create_client(). The pymodbus AsyncModbusTcpClient itself is
        # replaced so this test never touches a real socket.
        client = AnkerSolixModbusClient(ip_address="10.0.0.5", port=502)
        assert client.client is None
        fake = _FakePymodbusClient(connected=False, connect_result=True)
        monkeypatch.setattr(client, "_create_client", lambda: fake)

        # Act
        await client.connect()

        # Assert
        assert client.client is fake

    async def test_connection_error_returns_false_and_sets_error_status(self) -> None:
        # Arrange
        client = AnkerSolixModbusClient()
        client.client = _FakePymodbusClient(
            connected=False, connect_side_effect=OSError("network unreachable")
        )

        # Act
        result = await client.connect()

        # Assert
        assert result is False
        assert client._connection_status == "error"


class TestDisconnect:
    """disconnect() cleanup."""

    async def test_disconnect_closes_client_and_sets_status(self) -> None:
        # Arrange
        fake = _FakePymodbusClient(connected=True)
        client = _make_client(fake)

        # Act
        await client.disconnect()

        # Assert
        assert fake.closed is True
        assert client._connection_status == "disconnected"

    async def test_disconnect_with_no_client_does_not_raise(self) -> None:
        client = AnkerSolixModbusClient()
        await client.disconnect()  # must not raise
        assert client._connection_status == "disconnected"


class TestHandleIoSuccess:
    """_handle_io_success() error-episode reset."""

    def test_resets_consecutive_error_counters(self) -> None:
        # Arrange
        client = AnkerSolixModbusClient()
        client._consecutive_errors = 5
        client._error_count_since_last_log = 3

        # Act
        client._handle_io_success()

        # Assert
        assert client._consecutive_errors == 0
        assert client._error_count_since_last_log == 0

    def test_no_op_when_no_errors_pending(self) -> None:
        client = AnkerSolixModbusClient()
        client._handle_io_success()  # must not raise even at baseline zero
        assert client._consecutive_errors == 0


class TestShouldDisconnectFor:
    """_should_disconnect_for() exception-type classification (issue #81)."""

    @pytest.mark.parametrize(
        "exc",
        [
            ConnectionError("reset"),
            OSError("broken pipe"),
            asyncio.TimeoutError(),
            ConnectionException("closed"),
        ],
    )
    def test_connection_level_exceptions_require_disconnect(self, exc: BaseException) -> None:
        assert AnkerSolixModbusClient._should_disconnect_for(exc) is True

    def test_modbus_exception_with_transaction_id_mismatch_requires_disconnect(self) -> None:
        # Arrange: the check matches the literal substring "transaction id"
        # (with a space), not the code's own "transaction_id" variable name.
        exc = ModbusException("request ask for transaction id=5 but got id=6")
        assert AnkerSolixModbusClient._should_disconnect_for(exc) is True

    def test_modbus_exception_with_device_id_mismatch_requires_disconnect(self) -> None:
        exc = ModbusException("device id mismatch")
        assert AnkerSolixModbusClient._should_disconnect_for(exc) is True

    def test_plain_modbus_io_exception_does_not_require_disconnect(self) -> None:
        # Arrange: a bare "no response" ModbusException, no desync wording.
        exc = ModbusException("No response received after 3 retries")

        # Assert: per the documented budget-preserving design, this alone
        # must NOT force a disconnect.
        assert AnkerSolixModbusClient._should_disconnect_for(exc) is False

    def test_unrelated_exception_does_not_require_disconnect(self) -> None:
        assert AnkerSolixModbusClient._should_disconnect_for(ValueError("bad")) is False


class TestIsCoveredByBatchRanges:
    """_is_covered_by_batch_ranges() range-membership check."""

    def test_no_ranges_returns_false(self) -> None:
        assert AnkerSolixModbusClient._is_covered_by_batch_ranges(100, 1, None) is False
        assert AnkerSolixModbusClient._is_covered_by_batch_ranges(100, 1, []) is False

    def test_address_inside_a_configured_range_returns_true(self) -> None:
        ranges = [(100, 200, "input")]
        assert AnkerSolixModbusClient._is_covered_by_batch_ranges(150, 1, ranges) is True

    def test_address_plus_count_exceeding_range_end_returns_false(self) -> None:
        # Arrange: address 195 + count 10 spans to 204, past the range end 200.
        ranges = [(100, 200, "input")]

        # Act & Assert
        assert AnkerSolixModbusClient._is_covered_by_batch_ranges(195, 10, ranges) is False

    def test_address_outside_every_range_returns_false(self) -> None:
        ranges = [(100, 200, "input")]
        assert AnkerSolixModbusClient._is_covered_by_batch_ranges(500, 1, ranges) is False


class TestDefaultValue:
    """_default_value() fallback-by-type."""

    def test_string_type_defaults_to_empty_string(self) -> None:
        client = AnkerSolixModbusClient()
        assert client._default_value("STRING") == ""

    def test_version_type_defaults_to_empty_string(self) -> None:
        client = AnkerSolixModbusClient()
        assert client._default_value("VERSION") == ""

    def test_numeric_types_default_to_zero(self) -> None:
        client = AnkerSolixModbusClient()
        assert client._default_value("UINT16") == 0
        assert client._default_value("INT32") == 0


class TestDecodeRegisterValue:
    """_decode_register_value() covering every DATA_TYPE branch."""

    def test_uint16_decodes_first_register_directly(self) -> None:
        client = AnkerSolixModbusClient()
        assert client._decode_register_value(0, "UINT16", [1234]) == 1234

    def test_int16_positive_value_stays_unchanged(self) -> None:
        client = AnkerSolixModbusClient()
        assert client._decode_register_value(0, "INT16", [100]) == 100

    def test_int16_negative_value_uses_twos_complement(self) -> None:
        # Arrange: 0xFFFF as INT16 must decode to -1.
        client = AnkerSolixModbusClient()
        assert client._decode_register_value(0, "INT16", [0xFFFF]) == -1

    def test_int32_positive_big_endian(self) -> None:
        # Arrange: high=0x0001, low=0x0002 -> 0x00010002 = 65538.
        client = AnkerSolixModbusClient()
        assert client._decode_register_value(0, "INT32", [0x0001, 0x0002]) == 65538

    def test_int32_negative_value_via_twos_complement(self) -> None:
        # Arrange: 0xFFFFFFFF as INT32 must decode to -1.
        client = AnkerSolixModbusClient()
        assert client._decode_register_value(0, "INT32", [0xFFFF, 0xFFFF]) == -1

    def test_int32_with_fewer_than_two_registers_raises_decode_error(self) -> None:
        # Arrange: this specific malformation is explicitly raised rather
        # than silently defaulted (unlike a generic decode Exception, which
        # the outer try/except in this same method DOES default).
        client = AnkerSolixModbusClient()
        with pytest.raises(_RegisterDecodeError):
            client._decode_register_value(0, "INT32", [1])

    def test_uint32_big_endian(self) -> None:
        client = AnkerSolixModbusClient()
        assert client._decode_register_value(0, "UINT32", [0x0001, 0x0002]) == 65538

    def test_uint32_with_fewer_than_two_registers_raises_decode_error(self) -> None:
        client = AnkerSolixModbusClient()
        with pytest.raises(_RegisterDecodeError):
            client._decode_register_value(0, "UINT32", [1])

    def test_version_decodes_four_bytes_from_two_registers(self) -> None:
        # Arrange: registers=[0x0102, 0x0304] -> bytes [1,2,3,4] -> "1.2.3.4".
        client = AnkerSolixModbusClient()
        result = client._decode_register_value(0, "VERSION", [0x0102, 0x0304])
        assert result == "1.2.3.4"

    def test_version_with_insufficient_bytes_returns_empty_string(self) -> None:
        # Arrange: only 1 register -> 2 bytes, need >= 4.
        client = AnkerSolixModbusClient()
        result = client._decode_register_value(0, "VERSION", [0x0102])
        assert result == ""

    def test_string_decodes_utf8_and_strips_trailing_nulls(self) -> None:
        # Arrange: "AB" (0x4142) followed by a null register.
        client = AnkerSolixModbusClient()
        result = client._decode_register_value(0, "STRING", [0x4142, 0x0000])
        assert result == "AB"

    def test_unknown_data_type_falls_back_to_first_register(self) -> None:
        client = AnkerSolixModbusClient()
        assert client._decode_register_value(0, "SOME_UNKNOWN_TYPE", [42]) == 42

    def test_empty_registers_raises_register_decode_error(self) -> None:
        client = AnkerSolixModbusClient()
        with pytest.raises(_RegisterDecodeError):
            client._decode_register_value(0, "UINT16", [])

    def test_none_in_registers_raises_register_decode_error(self) -> None:
        client = AnkerSolixModbusClient()
        with pytest.raises(_RegisterDecodeError):
            client._decode_register_value(0, "UINT16", [None])


class TestReadRegister:
    """read_register() end-to-end: connection gate, count inference, decode."""

    async def test_not_connected_returns_none(self) -> None:
        client = AnkerSolixModbusClient()
        result = await client.read_register(0, "UINT16")
        assert result is None

    async def test_successful_read_decodes_and_resets_error_counter(self) -> None:
        # Arrange
        fake = _FakePymodbusClient(
            connected=True,
            read_input_registers_queue=[_FakeResponse(registers=[1234])],
        )
        client = _make_client(fake)
        client._consecutive_errors = 3

        # Act
        result = await client.read_register(100, "UINT16")

        # Assert
        assert result == 1234
        assert client._consecutive_errors == 0

    async def test_error_response_returns_none(self) -> None:
        # Arrange
        fake = _FakePymodbusClient(
            connected=True,
            read_input_registers_queue=[_FakeResponse(error=True)],
        )
        client = _make_client(fake)

        # Act
        result = await client.read_register(100, "UINT16")

        # Assert
        assert result is None

    async def test_count_inferred_as_two_for_int32(self) -> None:
        # Arrange: default count=None must resolve to 2 registers for INT32
        # so the fake response's 2-element list decodes without a
        # _RegisterDecodeError.
        fake = _FakePymodbusClient(
            connected=True,
            read_input_registers_queue=[_FakeResponse(registers=[0x0001, 0x0002])],
        )
        client = _make_client(fake)

        # Act
        result = await client.read_register(100, "INT32")

        # Assert
        assert result == 65538

    async def test_transport_exception_is_handled_and_returns_none(self) -> None:
        # Arrange
        fake = _FakePymodbusClient(
            connected=True,
            read_input_registers_queue=[ConnectionException("closed")],
        )
        client = _make_client(fake)

        # Act
        result = await client.read_register(100, "UINT16")

        # Assert: caught by the except clause, not propagated.
        assert result is None

    async def test_decode_error_from_malformed_registers_returns_none(self) -> None:
        # Arrange: INT32 needs 2 registers, response provides only 1.
        fake = _FakePymodbusClient(
            connected=True,
            read_input_registers_queue=[_FakeResponse(registers=[1])],
        )
        client = _make_client(fake)

        # Act
        result = await client.read_register(100, "INT32")

        # Assert
        assert result is None


class TestReadDevicePn:
    """read_device_pn() salted-hash PN extraction (privacy-sensitive path)."""

    async def test_successful_read_returns_deterministic_hash(self) -> None:
        # Arrange: registers decode to "ABC100" after cleanup.
        registers = [0x4142, 0x4331, 0x3030, 0x2020, 0x2020]
        fake = _FakePymodbusClient(
            connected=True,
            read_input_registers_queue=[_FakeResponse(registers=registers)],
        )
        client = _make_client(fake)

        # Act
        pn_hash, raw_pn, raw_hex = await client.read_device_pn()

        # Assert
        assert raw_pn == "ABC100"
        assert len(pn_hash) == 64  # SHA-256 hex digest length
        assert raw_hex == "0x4142 0x4331 0x3030 0x2020 0x2020"

    async def test_hash_is_deterministic_across_calls(self) -> None:
        # Arrange
        registers = [0x4142, 0x4331, 0x3030, 0x2020, 0x2020]
        fake = _FakePymodbusClient(
            connected=True,
            read_input_registers_queue=[
                _FakeResponse(registers=registers),
                _FakeResponse(registers=registers),
            ],
        )
        client = _make_client(fake)

        # Act
        first = await client.read_device_pn()
        second = await client.read_device_pn()

        # Assert
        assert first[0] == second[0]

    async def test_all_whitespace_registers_return_empty_hash_but_keep_raw(self) -> None:
        # Arrange: 5 registers of pure spaces -> cleaned PN is empty.
        registers = [0x2020, 0x2020, 0x2020, 0x2020, 0x2020]
        fake = _FakePymodbusClient(
            connected=True,
            read_input_registers_queue=[_FakeResponse(registers=registers)],
        )
        client = _make_client(fake)

        # Act
        pn_hash, _raw_pn, raw_hex = await client.read_device_pn()

        # Assert
        assert pn_hash == ""
        assert raw_hex != ""

    async def test_error_response_returns_all_empty_tuple(self) -> None:
        # Arrange
        fake = _FakePymodbusClient(
            connected=True,
            read_input_registers_queue=[_FakeResponse(error=True)],
        )
        client = _make_client(fake)

        # Act
        result = await client.read_device_pn()

        # Assert
        assert result == ("", "", "")

    async def test_connection_exception_retries_once_then_fails(self) -> None:
        # Arrange: not connected at all, and connect() always fails, so both
        # attempts exhaust without ever reading a register.
        client = AnkerSolixModbusClient()
        client.client = _FakePymodbusClient(connected=False, connect_result=False)

        # Act
        result = await client.read_device_pn()

        # Assert
        assert result == ("", "", "")

    async def test_transport_exception_forces_disconnect_and_retries(self) -> None:
        # Arrange: first read raises ConnectionException (forces disconnect +
        # retry), second attempt succeeds after "reconnecting".
        registers = [0x4142, 0x4331, 0x3030, 0x2020, 0x2020]
        fake = _FakePymodbusClient(
            connected=True,
            read_input_registers_queue=[
                ConnectionException("closed"),
                _FakeResponse(registers=registers),
            ],
        )
        client = _make_client(fake)

        # Act
        pn_hash, raw_pn, _ = await client.read_device_pn()

        # Assert: the retry succeeded on attempt 2.
        assert raw_pn == "ABC100"
        assert pn_hash != ""

    async def test_unexpected_exception_returns_empty_immediately(self) -> None:
        # Arrange: a non-connection exception (e.g. a bug) must short-circuit
        # without retrying, per the bare `except Exception` branch.
        class _WeirdError(Exception):
            pass

        fake = _FakePymodbusClient(
            connected=True, read_input_registers_queue=[_WeirdError("unexpected")]
        )
        client = _make_client(fake)

        # Act
        result = await client.read_device_pn()

        # Assert
        assert result == ("", "", "")


class TestForceDisconnect:
    """_force_disconnect() best-effort synchronous cleanup."""

    def test_closes_client_and_sets_disconnected_status(self) -> None:
        # Arrange
        fake = _FakePymodbusClient(connected=True)
        client = _make_client(fake)

        # Act
        client._force_disconnect()

        # Assert
        assert fake.closed is True
        assert client._connection_status == "disconnected"

    def test_no_client_does_not_raise(self) -> None:
        client = AnkerSolixModbusClient()
        client._force_disconnect()  # must not raise
        assert client._connection_status == "disconnected"


class TestHandleConnectionError:
    """_handle_connection_error() throttling and disconnect-trigger logic."""

    def test_increments_consecutive_error_counter(self) -> None:
        # Arrange
        client = _make_client()

        # Act
        client._handle_connection_error("boom")

        # Assert
        assert client._consecutive_errors == 1

    def test_broken_pipe_string_match_forces_disconnect(self) -> None:
        # Arrange
        fake = _FakePymodbusClient(connected=True)
        client = _make_client(fake)

        # Act
        client._handle_connection_error("Broken pipe detected")

        # Assert
        assert fake.closed is True

    def test_exception_type_match_forces_disconnect(self) -> None:
        # Arrange: no string match, but exc is a ConnectionException, which
        # _should_disconnect_for() classifies as disconnect-worthy.
        fake = _FakePymodbusClient(connected=True)
        client = _make_client(fake)

        # Act
        client._handle_connection_error("generic message", exc=ConnectionException("x"))

        # Assert
        assert fake.closed is True

    def test_plain_modbus_io_error_does_not_force_disconnect(self) -> None:
        # Arrange
        fake = _FakePymodbusClient(connected=True)
        client = _make_client(fake)

        # Act: no string match, and the exception type does not warrant a
        # disconnect per the documented budget-preserving design.
        client._handle_connection_error(
            "timeout", exc=ModbusException("No response received after 3 retries")
        )

        # Assert
        assert fake.closed is False


class TestReadSingleDataPoint:
    """_read_single_data_point() out-of-batch-range single reads."""

    async def test_holding_register_type_reads_via_read_holding_registers(self) -> None:
        # Arrange
        fake = _FakePymodbusClient(
            connected=True,
            read_holding_registers_queue=[_FakeResponse(registers=[42])],
        )
        client = _make_client(fake)

        # Act
        result = await client._read_single_data_point(100, 1, "holding")

        # Assert
        assert result == [42]

    async def test_input_register_type_reads_via_read_input_registers(self) -> None:
        # Arrange
        fake = _FakePymodbusClient(
            connected=True,
            read_input_registers_queue=[_FakeResponse(registers=[7])],
        )
        client = _make_client(fake)

        # Act
        result = await client._read_single_data_point(200, 1, "input")

        # Assert
        assert result == [7]

    async def test_error_response_returns_none(self) -> None:
        # Arrange
        fake = _FakePymodbusClient(
            connected=True,
            read_input_registers_queue=[_FakeResponse(error=True)],
        )
        client = _make_client(fake)

        # Act
        result = await client._read_single_data_point(200, 1, "input")

        # Assert
        assert result is None

    async def test_insufficient_data_returns_none(self) -> None:
        # Arrange: requested 2 registers, response provides only 1.
        fake = _FakePymodbusClient(
            connected=True,
            read_input_registers_queue=[_FakeResponse(registers=[1])],
        )
        client = _make_client(fake)

        # Act
        result = await client._read_single_data_point(200, 2, "input")

        # Assert
        assert result is None

    async def test_transport_exception_is_caught_and_returns_none(self) -> None:
        # Arrange
        fake = _FakePymodbusClient(
            connected=True, read_input_registers_queue=[ModbusException("timeout")]
        )
        client = _make_client(fake)

        # Act
        result = await client._read_single_data_point(200, 1, "input")

        # Assert
        assert result is None


class TestWriteRegister:
    """write_register() covering UINT16/INT32/UINT32 encoding and error paths."""

    async def test_not_connected_returns_transient_failure(self) -> None:
        # Arrange
        client = AnkerSolixModbusClient()

        # Act
        result = await client.write_register(100, 1, "UINT16")

        # Assert
        assert result.success is False
        assert result.is_transient is True
        assert "not connected" in result.error_reason.lower()

    async def test_uint16_write_uses_write_register_single(self) -> None:
        # Arrange
        fake = _FakePymodbusClient(
            connected=True, write_register_result=_FakeResponse()
        )
        client = _make_client(fake)

        # Act
        result = await client.write_register(100, 42, "UINT16")

        # Assert
        assert result.success is True
        assert "FC=0x06" in result.tx_frame

    async def test_int32_positive_write_splits_into_high_low_words(self) -> None:
        # Arrange: capture the values actually passed to write_registers.
        captured: dict = {}

        class _CapturingFake(_FakePymodbusClient):
            async def write_registers(self, address: int, values: list[int]) -> Any:
                captured["address"] = address
                captured["values"] = values
                return _FakeResponse()

        client = _make_client(_CapturingFake(connected=True))

        # Act: 0x00010002 -> high=0x0001, low=0x0002.
        result = await client.write_register(200, 0x00010002, "INT32")

        # Assert
        assert result.success is True
        assert captured["values"] == [0x0001, 0x0002]

    async def test_int32_negative_write_uses_twos_complement_encoding(self) -> None:
        # Arrange
        captured: dict = {}

        class _CapturingFake(_FakePymodbusClient):
            async def write_registers(self, address: int, values: list[int]) -> Any:
                captured["values"] = values
                return _FakeResponse()

        client = _make_client(_CapturingFake(connected=True))

        # Act: -1 as INT32 must encode to [0xFFFF, 0xFFFF] (two's complement).
        result = await client.write_register(200, -1, "INT32")

        # Assert
        assert result.success is True
        assert captured["values"] == [0xFFFF, 0xFFFF]

    async def test_uint32_write_splits_into_high_low_words(self) -> None:
        # Arrange
        captured: dict = {}

        class _CapturingFake(_FakePymodbusClient):
            async def write_registers(self, address: int, values: list[int]) -> Any:
                captured["values"] = values
                return _FakeResponse()

        client = _make_client(_CapturingFake(connected=True))

        # Act
        result = await client.write_register(300, 0x00030004, "UINT32")

        # Assert
        assert result.success is True
        assert captured["values"] == [0x0003, 0x0004]

    async def test_unknown_data_type_falls_back_to_single_register_write(self) -> None:
        # Arrange
        fake = _FakePymodbusClient(connected=True, write_register_result=_FakeResponse())
        client = _make_client(fake)

        # Act
        result = await client.write_register(100, 5, "SOME_UNKNOWN_TYPE")

        # Assert
        assert result.success is True

    async def test_modbus_exception_response_returns_failure_with_exception_name(self) -> None:
        # Arrange: device rejects the value (Illegal Data Value, code=3).
        error_response = _FakeResponse(error=True)
        error_response.exception_code = 3
        fake = _FakePymodbusClient(connected=True, write_register_result=error_response)
        client = _make_client(fake)

        # Act
        result = await client.write_register(100, 99999, "UINT16")

        # Assert
        assert result.success is False
        assert result.is_transient is False
        assert result.exception_name == "Illegal Data Value"
        assert result.exception_code == 3

    async def test_transport_exception_returns_transient_failure(self) -> None:
        # Arrange
        fake = _FakePymodbusClient(
            connected=True, write_side_effect=ConnectionException("closed")
        )
        client = _make_client(fake)

        # Act
        result = await client.write_register(100, 1, "UINT16")

        # Assert
        assert result.success is False
        assert result.is_transient is True
        assert "ConnectionException" in result.error_reason


class TestGetConnectionInfo:
    """get_connection_info() summary dict."""

    def test_reports_expected_keys(self) -> None:
        # Arrange
        client = AnkerSolixModbusClient(ip_address="10.0.0.1", port=502)

        # Act
        info = client.get_connection_info()

        # Assert
        assert info["ip_address"] == "10.0.0.1"
        assert info["port"] == 502
        assert info["connected"] is False
        assert info["status"] == "disconnected"


class TestGetAllData:
    """get_all_data() covering configured ranges, fallback, and single reads."""

    async def test_no_data_points_returns_empty_dict(self) -> None:
        client = _make_client()
        result = await client.get_all_data(None)
        assert result == {}

    async def test_configured_range_populates_matching_data_points(self) -> None:
        # Arrange: one input range [100,101] backing two 1-register points.
        fake = _FakePymodbusClient(
            connected=True,
            read_input_registers_queue=[_FakeResponse(registers=[10, 20])],
        )
        client = _make_client(fake)
        data_points = {
            "a": {"address": 100, "count": 1, "data_type": "UINT16"},
            "b": {"address": 101, "count": 1, "data_type": "UINT16"},
        }
        batch_ranges = [(100, 101, "input")]

        # Act
        result = await client.get_all_data(data_points, batch_ranges)

        # Assert
        assert result == {"a": 10, "b": 20}
        assert client.get_last_failed_registers() == set()

    async def test_configured_holding_range_uses_read_holding_registers(self) -> None:
        # Arrange
        fake = _FakePymodbusClient(
            connected=True,
            read_holding_registers_queue=[_FakeResponse(registers=[42])],
        )
        client = _make_client(fake)
        data_points = {"a": {"address": 200, "count": 1, "data_type": "UINT16"}}
        batch_ranges = [(200, 200, "holding")]

        # Act
        result = await client.get_all_data(data_points, batch_ranges)

        # Assert
        assert result == {"a": 42}

    async def test_range_failing_once_then_succeeding_after_reconnect(self) -> None:
        # Arrange: first attempt raises, forcing a reconnect+retry that then
        # succeeds -- the "retried once" branch inside the range read loop.
        fake = _FakePymodbusClient(
            connected=True,
            connect_result=True,
            read_input_registers_queue=[
                ConnectionException("closed"),
                _FakeResponse(registers=[5]),
            ],
        )
        client = _make_client(fake)
        data_points = {"a": {"address": 100, "count": 1, "data_type": "UINT16"}}
        batch_ranges = [(100, 100, "input")]

        # Act
        result = await client.get_all_data(data_points, batch_ranges)

        # Assert
        assert result == {"a": 5}

    async def test_range_failing_twice_marks_registers_unresponsive(self) -> None:
        # Arrange: both the initial attempt and the post-reconnect retry
        # raise, so the whole range must be marked failed and abandoned.
        fake = _FakePymodbusClient(
            connected=True,
            connect_result=True,
            read_input_registers_queue=[
                ConnectionException("closed"),
                ConnectionException("closed again"),
            ],
        )
        client = _make_client(fake)
        data_points = {"a": {"address": 100, "count": 1, "data_type": "UINT16"}}
        batch_ranges = [(100, 100, "input")]

        # Act
        result = await client.get_all_data(data_points, batch_ranges)

        # Assert
        assert result == {}
        assert 100 in client.get_last_failed_registers()

    async def test_device_unresponsive_skips_remaining_ranges(self) -> None:
        # Arrange: first range fails twice (marks device_unresponsive=True),
        # a second, later range must then be skipped entirely without any
        # further read attempts.
        fake = _FakePymodbusClient(
            connected=True,
            connect_result=True,
            read_input_registers_queue=[
                ConnectionException("closed"),
                ConnectionException("closed again"),
            ],
        )
        client = _make_client(fake)
        data_points = {
            "a": {"address": 100, "count": 1, "data_type": "UINT16"},
            "b": {"address": 300, "count": 1, "data_type": "UINT16"},
        }
        batch_ranges = [(100, 100, "input"), (300, 300, "input")]

        # Act
        result = await client.get_all_data(data_points, batch_ranges)

        # Assert: both ranges' registers end up marked failed, and the
        # second range's read was never attempted (queue only had 2 items).
        assert result == {}
        assert {100, 300}.issubset(client.get_last_failed_registers())

    async def test_error_response_falls_back_to_individual_register_reads(self) -> None:
        # Arrange: the batched [100-101] read errors, so each address is
        # retried individually -- 100 succeeds, 101 fails.
        fake = _FakePymodbusClient(connected=True)
        fake.read_input_registers_queue = [
            _FakeResponse(error=True),  # batched range read fails
            _FakeResponse(registers=[11]),  # individual read for 100
            _FakeResponse(error=True),  # individual read for 101 fails
        ]
        client = _make_client(fake)
        data_points = {
            "a": {"address": 100, "count": 1, "data_type": "UINT16"},
            "b": {"address": 101, "count": 1, "data_type": "UINT16"},
        }
        batch_ranges = [(100, 101, "input")]

        # Act
        result = await client.get_all_data(data_points, batch_ranges)

        # Assert: only "a" (address 100) made it through the fallback.
        assert result == {"a": 11}

    async def test_insufficient_registers_in_range_marks_all_addresses_failed(self) -> None:
        # Arrange: range expects 2 registers, response provides only 1.
        fake = _FakePymodbusClient(
            connected=True,
            read_input_registers_queue=[_FakeResponse(registers=[1])],
        )
        client = _make_client(fake)
        data_points = {"a": {"address": 100, "count": 2, "data_type": "UINT16"}}
        batch_ranges = [(100, 101, "input")]

        # Act
        result = await client.get_all_data(data_points, batch_ranges)

        # Assert
        assert result == {}
        assert {100, 101}.issubset(client.get_last_failed_registers())

    async def test_gain_divides_the_decoded_value(self) -> None:
        # Arrange: raw register 100, gain 10 -> final value 10.0.
        fake = _FakePymodbusClient(
            connected=True, read_input_registers_queue=[_FakeResponse(registers=[100])]
        )
        client = _make_client(fake)
        data_points = {
            "a": {"address": 100, "count": 1, "data_type": "UINT16", "gain": 10}
        }
        batch_ranges = [(100, 100, "input")]

        # Act
        result = await client.get_all_data(data_points, batch_ranges)

        # Assert
        assert result == {"a": 10.0}

    async def test_string_type_ignores_gain(self) -> None:
        # Arrange: STRING type must never be gain-divided even if configured.
        fake = _FakePymodbusClient(
            connected=True,
            read_input_registers_queue=[_FakeResponse(registers=[0x4142])],
        )
        client = _make_client(fake)
        data_points = {
            "a": {"address": 100, "count": 1, "data_type": "STRING", "gain": 10}
        }
        batch_ranges = [(100, 100, "input")]

        # Act
        result = await client.get_all_data(data_points, batch_ranges)

        # Assert
        assert result == {"a": "AB"}

    async def test_data_point_outside_every_range_with_input_type_reads_individually(self) -> None:
        # Arrange: no batch_ranges configured at all, so every point falls
        # to the per-point "outside every range" branch.
        fake = _FakePymodbusClient(
            connected=True, read_input_registers_queue=[_FakeResponse(registers=[77])]
        )
        client = _make_client(fake)
        data_points = {
            "a": {
                "address": 500,
                "count": 1,
                "data_type": "UINT16",
                "register_type": "input",
            }
        }

        # Act
        result = await client.get_all_data(data_points, batch_ranges=None)

        # Assert
        assert result == {"a": 77}

    async def test_data_point_without_register_type_and_no_range_is_marked_failed(self) -> None:
        # Arrange: no batch_ranges, and the point has no register_type ->
        # cannot be read at all.
        client = _make_client(_FakePymodbusClient(connected=True))
        data_points = {"a": {"address": 500, "count": 1, "data_type": "UINT16"}}

        # Act
        result = await client.get_all_data(data_points, batch_ranges=None)

        # Assert
        assert result == {}
        assert 500 in client.get_last_failed_registers()

    async def test_point_inside_a_failed_batch_range_is_not_re_read_individually(self) -> None:
        # Arrange: the configured range [100,100] fails twice (device
        # unresponsive), and a second point at address 100 nominally within
        # that same range must be marked failed via _is_covered_by_batch_ranges
        # rather than attempting yet another individual read.
        fake = _FakePymodbusClient(
            connected=True,
            connect_result=True,
            read_input_registers_queue=[
                ConnectionException("closed"),
                ConnectionException("closed again"),
            ],
        )
        client = _make_client(fake)
        data_points = {
            "a": {
                "address": 100,
                "count": 1,
                "data_type": "UINT16",
                "register_type": "input",
            }
        }
        batch_ranges = [(100, 100, "input")]

        # Act
        result = await client.get_all_data(data_points, batch_ranges)

        # Assert: only the 2 queued exceptions were consumed by the range
        # read; no 3rd individual read was attempted for the same address.
        assert result == {}
        assert fake.read_input_registers_queue == []

    async def test_malformed_configuration_missing_address_marks_failed(self) -> None:
        # Arrange: a data point missing the required "address" key.
        client = _make_client(_FakePymodbusClient(connected=True))
        data_points = {"a": {"data_type": "UINT16"}}

        # Act
        result = await client.get_all_data(data_points, batch_ranges=None)

        # Assert
        assert result == {}

    async def test_batch_optimization_path_groups_and_decodes(self) -> None:
        # Arrange: use_batch_optimization=True with no batch_ranges routes
        # through BatchRegisterReader.group_data_points() and the group-read
        # branch instead of the configured-range branch.
        fake = _FakePymodbusClient(
            connected=True,
            read_input_registers_queue=[_FakeResponse(registers=[9])],
        )
        client = _make_client(fake)
        data_points = {"a": {"address": 100, "count": 1, "data_type": "UINT16"}}

        # Act
        result = await client.get_all_data(
            data_points, batch_ranges=None, use_batch_optimization=True
        )

        # Assert
        assert result == {"a": 9}

    async def test_batch_optimization_group_read_failure_marks_all_points_failed(self) -> None:
        # Arrange
        fake = _FakePymodbusClient(
            connected=True, read_input_registers_queue=[_FakeResponse(error=True)]
        )
        client = _make_client(fake)
        data_points = {"a": {"address": 100, "count": 1, "data_type": "UINT16"}}

        # Act
        result = await client.get_all_data(
            data_points, batch_ranges=None, use_batch_optimization=True
        )

        # Assert
        assert result == {}
        assert 100 in client.get_last_failed_registers()

    async def test_batch_optimization_transport_exception_marks_all_points_failed(self) -> None:
        # Arrange
        fake = _FakePymodbusClient(
            connected=True, read_input_registers_queue=[ConnectionException("closed")]
        )
        client = _make_client(fake)
        data_points = {"a": {"address": 100, "count": 1, "data_type": "UINT16"}}

        # Act
        result = await client.get_all_data(
            data_points, batch_ranges=None, use_batch_optimization=True
        )

        # Assert
        assert result == {}
        assert 100 in client.get_last_failed_registers()

    async def test_batch_optimization_decode_exception_marks_point_failed(self) -> None:
        # Arrange: INT32 needs 2 registers, group read supplies only 1 for
        # this point's slice, tripping the inner decode try/except.
        fake = _FakePymodbusClient(
            connected=True, read_input_registers_queue=[_FakeResponse(registers=[1])]
        )
        client = _make_client(fake)
        data_points = {"a": {"address": 100, "count": 2, "data_type": "INT32"}}

        # Act
        result = await client.get_all_data(
            data_points, batch_ranges=None, use_batch_optimization=True
        )

        # Assert
        assert result == {}

    async def test_all_reads_successful_resets_error_counter(self) -> None:
        # Arrange
        fake = _FakePymodbusClient(
            connected=True, read_input_registers_queue=[_FakeResponse(registers=[1])]
        )
        client = _make_client(fake)
        client._consecutive_errors = 4
        data_points = {"a": {"address": 100, "count": 1, "data_type": "UINT16"}}
        batch_ranges = [(100, 100, "input")]

        # Act
        await client.get_all_data(data_points, batch_ranges)

        # Assert
        assert client._consecutive_errors == 0
