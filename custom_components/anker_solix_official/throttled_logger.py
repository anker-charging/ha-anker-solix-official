"""Throttled logger to reduce log spam."""
import logging
import time
from typing import Any, Dict


class LogThrottleState:
    """State for a single throttled log message."""

    def __init__(self):
        """Initialize throttle state."""
        self.first_occurrence: float = 0
        self.last_logged: float = 0
        self.count_since_last_log: int = 0
        self.total_count: int = 0


class ThrottledLogger:
    """Logger wrapper that throttles repeated log messages.

    Reduces log spam by aggregating repeated messages within a time window.
    Example: Instead of logging "Connection failed" 100 times per minute,
    log once with a count of occurrences.
    """

    def __init__(self, logger: logging.Logger, default_interval: float = 60):
        """Initialize throttled logger.

        Args:
            logger: The underlying Python logger instance
            default_interval: Default throttle interval in seconds (default: 60)
        """
        self._logger = logger
        self._default_interval = default_interval
        self._throttle_states: Dict[str, LogThrottleState] = {}

    def throttled_log(
        self,
        level: int,
        message: str,
        *args: Any,
        throttle_key: str | None = None,
        interval: float | None = None,
        **kwargs: Any
    ) -> None:
        """Log a message with throttling.

        Args:
            level: Logging level (e.g., logging.INFO, logging.WARNING)
            message: Log message format string
            *args: Format arguments for the message
            throttle_key: Unique key for this message type (defaults to message itself)
            interval: Throttle interval in seconds (defaults to default_interval)
            **kwargs: Additional keyword arguments passed to logger
        """
        current_time = time.time()
        throttle_key = throttle_key or message
        throttle_interval = interval or self._default_interval

        # Get or create throttle state
        if throttle_key not in self._throttle_states:
            self._throttle_states[throttle_key] = LogThrottleState()

        state = self._throttle_states[throttle_key]

        # Update counters
        state.total_count += 1
        state.count_since_last_log += 1

        # Track first occurrence
        if state.first_occurrence == 0:
            state.first_occurrence = current_time

        # Check if we should log
        time_since_last_log = current_time - state.last_logged

        if time_since_last_log >= throttle_interval:
            # Log with occurrence count if this is a repeated message
            if state.count_since_last_log > 1:
                augmented_message = f"{message} (occurred {state.count_since_last_log} times in {time_since_last_log:.1f}s)"
                self._logger.log(level, augmented_message, *args, **kwargs)
            else:
                self._logger.log(level, message, *args, **kwargs)

            # Reset counters
            state.last_logged = current_time
            state.count_since_last_log = 0

    def info(self, message: str, *args: Any, **kwargs: Any) -> None:
        """Log INFO level message with throttling."""
        self.throttled_log(logging.INFO, message, *args, **kwargs)

    def warning(self, message: str, *args: Any, **kwargs: Any) -> None:
        """Log WARNING level message with throttling."""
        self.throttled_log(logging.WARNING, message, *args, **kwargs)

    def error(self, message: str, *args: Any, **kwargs: Any) -> None:
        """Log ERROR level message with throttling."""
        self.throttled_log(logging.ERROR, message, *args, **kwargs)

    def debug(self, message: str, *args: Any, **kwargs: Any) -> None:
        """Log DEBUG level message with throttling."""
        self.throttled_log(logging.DEBUG, message, *args, **kwargs)

    def reset_throttle(self, throttle_key: str | None = None) -> None:
        """Reset throttle state for a specific key or all keys.

        Args:
            throttle_key: Key to reset, or None to reset all
        """
        if throttle_key:
            self._throttle_states.pop(throttle_key, None)
        else:
            self._throttle_states.clear()

    def get_stats(self, throttle_key: str) -> Dict[str, Any] | None:
        """Get statistics for a throttled message.

        Args:
            throttle_key: Key to get stats for

        Returns:
            Dictionary with statistics, or None if key not found
        """
        state = self._throttle_states.get(throttle_key)
        if not state:
            return None

        return {
            "total_count": state.total_count,
            "count_since_last_log": state.count_since_last_log,
            "first_occurrence": state.first_occurrence,
            "last_logged": state.last_logged,
        }
