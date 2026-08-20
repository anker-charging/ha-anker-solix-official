"""Unit tests for ThrottledLogger / LogThrottleState."""

import logging

from custom_components.anker_solix_official.throttled_logger import ThrottledLogger


class TestThrottledLog:
    """throttled_log() and its level-specific wrappers."""

    def test_first_call_logs_immediately(self) -> None:
        # Arrange
        base_logger = logging.getLogger("test.throttle.first")
        recording = _RecordingLog()
        base_logger.log = recording
        logger = ThrottledLogger(base_logger, default_interval=60)

        # Act
        logger.info("device offline")

        # Assert: first occurrence of a key is never throttled.
        assert recording.calls == [(logging.INFO, "device offline", (), {})]

    def test_second_call_within_interval_is_suppressed(self) -> None:
        # Arrange
        base_logger = logging.getLogger("test.throttle.suppressed")
        recording = _RecordingLog()
        base_logger.log = recording
        logger = ThrottledLogger(base_logger, default_interval=60)

        # Act: two calls in immediate succession, well within the 60s window.
        logger.warning("connection error")
        logger.warning("connection error")

        # Assert: only the first call actually reached the underlying logger.
        assert len(recording.calls) == 1

    def test_call_after_interval_elapses_logs_with_occurrence_count(
        self, monkeypatch: object
    ) -> None:
        # Arrange: control time.time() directly so the throttle window
        # boundary is deterministic rather than racing the wall clock.
        base_logger = logging.getLogger("test.throttle.elapsed")
        recording = _RecordingLog()
        base_logger.log = recording
        logger = ThrottledLogger(base_logger, default_interval=60)
        fake_time = [1000.0]
        monkeypatch.setattr(
            "custom_components.anker_solix_official.throttled_logger.time.time",
            lambda: fake_time[0],
        )

        # Act: 2 calls inside the window (suppressed, but counted), then
        # advance past the 60s interval for a 3rd call.
        logger.error("device offline")
        logger.error("device offline")
        fake_time[0] += 61
        logger.error("device offline")

        # Assert: only 2 log() calls reached the underlying logger (call #2
        # was suppressed); the 3rd carries the accumulated occurrence count.
        assert len(recording.calls) == 2
        first_msg = recording.calls[0][1]
        second_msg = recording.calls[1][1]
        assert first_msg == "device offline"
        assert "occurred 2 times" in second_msg

    def test_distinct_throttle_keys_are_independent(self) -> None:
        # Arrange
        base_logger = logging.getLogger("test.throttle.keys")
        recording = _RecordingLog()
        base_logger.log = recording
        logger = ThrottledLogger(base_logger, default_interval=60)

        # Act
        logger.info("msg a", throttle_key="key_a")
        logger.info("msg b", throttle_key="key_b")

        # Assert: different keys are tracked independently, both log immediately.
        assert len(recording.calls) == 2

    def test_explicit_interval_overrides_default(self, monkeypatch: object) -> None:
        # Arrange: default_interval is huge; passing a small explicit
        # interval must take precedence over it once that much time elapses.
        base_logger = logging.getLogger("test.throttle.override")
        recording = _RecordingLog()
        base_logger.log = recording
        logger = ThrottledLogger(base_logger, default_interval=3600)
        fake_time = [1000.0]
        monkeypatch.setattr(
            "custom_components.anker_solix_official.throttled_logger.time.time",
            lambda: fake_time[0],
        )

        # Act
        logger.debug("m1", throttle_key="k", interval=5)
        fake_time[0] += 6
        logger.debug("m1", throttle_key="k", interval=5)

        # Assert
        assert len(recording.calls) == 2

    def test_zero_interval_falls_back_to_default_due_to_falsy_check(self) -> None:
        # Arrange: `interval or self._default_interval` treats an explicit
        # interval=0 as falsy, so it silently falls back to default_interval
        # instead of "always log immediately" -- a real gotcha in the source
        # worth locking down so a future refactor doesn't reintroduce it
        # differently.
        base_logger = logging.getLogger("test.throttle.zero_interval")
        recording = _RecordingLog()
        base_logger.log = recording
        logger = ThrottledLogger(base_logger, default_interval=3600)

        # Act: two immediate calls with interval=0 explicitly requested.
        logger.debug("m1", throttle_key="k", interval=0)
        logger.debug("m1", throttle_key="k", interval=0)

        # Assert: falls back to the 3600s default, so the 2nd call is suppressed.
        assert len(recording.calls) == 1


class TestResetThrottle:
    """reset_throttle() clears one or all throttle states."""

    def test_reset_specific_key_removes_only_that_state(self) -> None:
        # Arrange
        logger = ThrottledLogger(logging.getLogger("test.throttle.reset1"))
        logger.info("a", throttle_key="k1")
        logger.info("b", throttle_key="k2")

        # Act
        logger.reset_throttle("k1")

        # Assert
        assert logger.get_stats("k1") is None
        assert logger.get_stats("k2") is not None

    def test_reset_without_key_clears_all_states(self) -> None:
        # Arrange
        logger = ThrottledLogger(logging.getLogger("test.throttle.reset2"))
        logger.info("a", throttle_key="k1")
        logger.info("b", throttle_key="k2")

        # Act
        logger.reset_throttle()

        # Assert
        assert logger.get_stats("k1") is None
        assert logger.get_stats("k2") is None


class TestGetStats:
    """get_stats() reporting."""

    def test_unknown_key_returns_none(self) -> None:
        logger = ThrottledLogger(logging.getLogger("test.throttle.stats.none"))
        assert logger.get_stats("never-logged") is None

    def test_known_key_reports_total_count(self) -> None:
        # Arrange
        base_logger = logging.getLogger("test.throttle.stats.known")
        base_logger.log = _RecordingLog()
        logger = ThrottledLogger(base_logger, default_interval=3600)

        # Act: 3 occurrences of the same key, all within the throttle window
        # so only the first is actually emitted, but all 3 are counted.
        logger.info("repeated", throttle_key="k")
        logger.info("repeated", throttle_key="k")
        logger.info("repeated", throttle_key="k")

        # Assert
        stats = logger.get_stats("k")
        assert stats["total_count"] == 3


class _RecordingLog:
    """Stand-in for logging.Logger.log that records call arguments.

    Used instead of a full mocking framework since only positional
    level/message capture is needed and this keeps the tests dependency-free.
    """

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __call__(self, level, message, *args, **kwargs) -> None:
        self.calls.append((level, message, args, kwargs))
