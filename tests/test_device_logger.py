"""Unit tests for DeviceLoggerAdapter, OperationLogger, and WriteResult."""

import logging

from custom_components.anker_solix_official.device_logger import (
    DeviceLoggerAdapter,
    OperationLogger,
    WriteResult,
)


class TestWriteResult:
    """WriteResult dataclass and its __bool__ override."""

    def test_bool_reflects_success_field(self) -> None:
        assert bool(WriteResult(success=True)) is True
        assert bool(WriteResult(success=False)) is False

    def test_default_fields(self) -> None:
        result = WriteResult(success=True)
        assert result.error_reason == ""
        assert result.exception_code is None
        assert result.is_transient is False

    def test_truthiness_in_if_statement(self) -> None:
        # Arrange
        result = WriteResult(success=True)

        # Act & Assert: exercises __bool__ via a plain "if" (not "if x.success").
        if result:
            outcome = "success"
        else:
            outcome = "failure"
        assert outcome == "success"


class TestDeviceLoggerAdapterPrefix:
    """_build_prefix() composition rules."""

    def test_prefix_includes_all_provided_fields_in_order(self) -> None:
        # Arrange
        adapter = DeviceLoggerAdapter(
            logging.getLogger("test.device_logger.full"),
            device_name="Living Room Socket",
            device_ip="192.168.101.31",
            device_port=502,
            device_sn="ABC123456",
            device_model="Smart Plug",
        )

        # Assert
        assert adapter.device_prefix == (
            "[Living Room Socket | 192.168.101.31:502 | SN:ABC123456 | Model:Smart Plug]"
        )

    def test_prefix_with_no_fields_uses_unknown_device_placeholder(self) -> None:
        adapter = DeviceLoggerAdapter(logging.getLogger("test.device_logger.empty"))
        assert adapter.device_prefix == "[Unknown Device]"

    def test_prefix_with_only_name(self) -> None:
        adapter = DeviceLoggerAdapter(
            logging.getLogger("test.device_logger.name_only"),
            device_name="Solarbank",
        )
        assert adapter.device_prefix == "[Solarbank]"

    def test_process_prepends_prefix_to_message(self) -> None:
        # Arrange
        adapter = DeviceLoggerAdapter(
            logging.getLogger("test.device_logger.process"),
            device_name="Solarbank",
        )

        # Act
        msg, kwargs = adapter.process("Connected", {})

        # Assert
        assert msg == "[Solarbank] Connected"
        assert kwargs == {}

    def test_update_device_info_rebuilds_prefix(self) -> None:
        # Arrange: SN discovered only after the first successful connection,
        # so the prefix must be regenerated in place. update_device_info's
        # **kwargs merge directly into device_info, so the key must match
        # device_info's own key ("sn"), not the constructor arg ("device_sn").
        adapter = DeviceLoggerAdapter(
            logging.getLogger("test.device_logger.update"),
            device_name="Solarbank",
        )
        assert "SN:" not in adapter.device_prefix

        # Act
        adapter.update_device_info(sn="XYZ999")

        # Assert
        assert adapter.device_prefix == "[Solarbank | SN:XYZ999]"


class TestOperationLogger:
    """OperationLogger context-manager lifecycle logging."""

    def test_successful_operation_logs_start_and_success(self) -> None:
        # Arrange
        recorded: list[str] = []
        adapter = DeviceLoggerAdapter(logging.getLogger("test.device_logger.op1"))
        adapter.log = lambda level, msg: recorded.append(msg)

        # Act
        with OperationLogger(adapter, "Set operating mode", mode="self_use"):
            pass

        # Assert
        assert any("Operation started: Set operating mode" in m for m in recorded)
        assert any("Operation SUCCESS: Set operating mode" in m for m in recorded)
        assert any("mode=self_use" in m for m in recorded)

    def test_failed_operation_logs_failure_with_exception_details(self) -> None:
        # Arrange
        recorded: list[str] = []
        adapter = DeviceLoggerAdapter(logging.getLogger("test.device_logger.op2"))
        adapter.log = lambda level, msg: recorded.append(msg)
        adapter.error = lambda msg: recorded.append(f"ERROR:{msg}")

        # Act: exception inside the with-block must propagate (return False).
        try:
            with OperationLogger(adapter, "Write register", address=100):
                raise ValueError("device rejected value")
        except ValueError:
            pass

        # Assert
        assert any("Operation FAILED: Write register" in m for m in recorded)
        assert any("ValueError: device rejected value" in m for m in recorded)

    def test_context_manager_reraises_the_original_exception(self) -> None:
        # Arrange
        adapter = DeviceLoggerAdapter(logging.getLogger("test.device_logger.op3"))
        adapter.log = lambda level, msg: None
        adapter.error = lambda msg: None

        # Act & Assert: __exit__ returns False, so the exception must escape.
        raised = False
        try:
            with OperationLogger(adapter, "op"):
                raise RuntimeError("boom")
        except RuntimeError:
            raised = True
        assert raised

    def test_add_context_merges_additional_fields(self) -> None:
        # Arrange
        recorded: list[str] = []
        adapter = DeviceLoggerAdapter(logging.getLogger("test.device_logger.op4"))
        adapter.log = lambda level, msg: recorded.append(msg)

        # Act
        op = OperationLogger(adapter, "Batch read")
        op.add_context(count=5)
        with op:
            pass

        # Assert
        assert any("count=5" in m for m in recorded)

    def test_no_context_produces_message_without_pipe_separator(self) -> None:
        # Arrange
        recorded: list[str] = []
        adapter = DeviceLoggerAdapter(logging.getLogger("test.device_logger.op5"))
        adapter.log = lambda level, msg: recorded.append(msg)

        # Act
        with OperationLogger(adapter, "No-context op"):
            pass

        # Assert: without context, the message must not contain a trailing " | ".
        start_msg = next(m for m in recorded if m.startswith("Operation started"))
        assert start_msg == "Operation started: No-context op"
