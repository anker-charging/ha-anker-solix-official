"""Device-specific logger adapter for multi-device environments.

Provides DeviceLoggerAdapter (auto-prefixes all logs with device identity)
and OperationLogger (tracks operation start/success/failure lifecycle).

Usage:
    logger = DeviceLoggerAdapter(
        logging.getLogger(__name__),
        device_name="Living Room Socket",
        device_ip="192.168.101.31",
        device_sn="ABC123456",
    )
    logger.info("Connected successfully")
    # Output: [Living Room Socket | 192.168.101.31:502 | SN:ABC123456] Connected successfully
"""

import logging
from dataclasses import dataclass, field
from typing import Any, MutableMapping


@dataclass
class WriteResult:
    """Modbus write operation result with full failure context."""

    success: bool
    error_reason: str = ""
    raw_response: str = ""
    exception_code: int | None = None
    exception_name: str = ""
    tx_frame: str = ""

    def __bool__(self) -> bool:
        return self.success


class DeviceLoggerAdapter(logging.LoggerAdapter):
    """Logger adapter that auto-prefixes all messages with device identity."""

    def __init__(
        self,
        logger: logging.Logger,
        device_name: str | None = None,
        device_ip: str | None = None,
        device_port: int = 502,
        device_sn: str | None = None,
        device_model: str | None = None,
    ):
        self.device_info = {
            "name": device_name,
            "ip": device_ip,
            "port": device_port,
            "sn": device_sn,
            "model": device_model,
        }
        self.device_prefix = self._build_prefix()
        super().__init__(logger, {})

    def _build_prefix(self) -> str:
        parts = []
        if self.device_info.get("name"):
            parts.append(self.device_info["name"])
        if self.device_info.get("ip"):
            parts.append(
                f"{self.device_info['ip']}:{self.device_info.get('port', 502)}"
            )
        if self.device_info.get("sn"):
            parts.append(f"SN:{self.device_info['sn']}")
        if self.device_info.get("model"):
            parts.append(f"Model:{self.device_info['model']}")
        return f"[{' | '.join(parts)}]" if parts else "[Unknown Device]"

    def process(
        self, msg: str, kwargs: MutableMapping[str, Any]
    ) -> tuple[str, MutableMapping[str, Any]]:
        return f"{self.device_prefix} {msg}", kwargs

    def update_device_info(self, **kwargs):
        """Update device info dynamically (e.g., SN discovered after first connection)."""
        self.device_info.update(kwargs)
        self.device_prefix = self._build_prefix()


class OperationLogger:
    """Context manager that logs operation lifecycle: start → success/failure.

    Usage:
        with OperationLogger(logger, "Set operating mode", mode="third_party_control", address=32770):
            result = await write_register(address, value)
            if not result:
                raise ValueError("Write returned False")
    """

    def __init__(
        self,
        logger: DeviceLoggerAdapter,
        operation: str,
        log_level: int = logging.INFO,
        **context,
    ):
        self.logger = logger
        self.operation = operation
        self.log_level = log_level
        self.context = context

    def _format_context(self) -> str:
        return ", ".join(f"{k}={v}" for k, v in self.context.items())

    def __enter__(self):
        ctx = self._format_context()
        msg = f"Operation started: {self.operation}"
        self.logger.log(self.log_level, f"{msg} | {ctx}" if ctx else msg)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        ctx = self._format_context()
        if exc_type is None:
            msg = f"Operation SUCCESS: {self.operation}"
            self.logger.log(self.log_level, f"{msg} | {ctx}" if ctx else msg)
        else:
            error_msg = f"{exc_type.__name__}: {exc_val}"
            msg = f"Operation FAILED: {self.operation}"
            self.logger.error(
                f"{msg} | {ctx} | error={error_msg}"
                if ctx
                else f"{msg} | error={error_msg}"
            )
        return False

    def add_context(self, **kwargs):
        self.context.update(kwargs)
