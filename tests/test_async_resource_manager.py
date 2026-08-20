"""Unit tests for AsyncResourceManager background task lifecycle."""

import asyncio

from custom_components.anker_solix_official.async_resource_manager import (
    AsyncResourceManager,
)


class TestCreateTask:
    """create_task() tracking behaviour."""

    async def test_created_task_is_tracked(self) -> None:
        # Arrange
        manager = AsyncResourceManager()

        # Act
        task = manager.create_task(asyncio.sleep(0), name="t1")
        assert manager.task_count() == 1

        # Assert: after completion the done-callback removes it automatically.
        await task
        await asyncio.sleep(0)  # let the done_callback run
        assert manager.task_count() == 0

    async def test_get_running_tasks_returns_a_copy(self) -> None:
        # Arrange
        manager = AsyncResourceManager()
        manager.create_task(asyncio.sleep(1), name="long")

        # Act
        snapshot = manager.get_running_tasks()
        snapshot.clear()

        # Assert: mutating the returned set must not affect internal state.
        assert manager.task_count() == 1

        # Cleanup
        await manager.shutdown(timeout=1)


class TestShutdown:
    """shutdown() three-phase cancellation."""

    async def test_shutdown_with_no_tasks_returns_immediately(self) -> None:
        manager = AsyncResourceManager()
        await manager.shutdown()  # must not raise
        assert manager.task_count() == 0

    async def test_shutdown_cancels_running_tasks(self) -> None:
        # Arrange
        manager = AsyncResourceManager()
        manager.create_task(asyncio.sleep(10), name="never-finishes")

        # Act
        await manager.shutdown(timeout=1)

        # Assert
        assert manager.task_count() == 0

    async def test_shutdown_logs_but_swallows_task_exceptions(self) -> None:
        # Arrange
        async def _boom() -> None:
            raise ValueError("deliberate failure")

        manager = AsyncResourceManager()
        manager.create_task(_boom(), name="failing")
        await asyncio.sleep(0.01)  # let the task actually raise before shutdown

        # Act & Assert: shutdown must not propagate the task's exception.
        await manager.shutdown(timeout=1)
        assert manager.task_count() == 0

    async def test_shutdown_timeout_is_handled_without_raising(self) -> None:
        # Arrange: a task that ignores cancellation for longer than the
        # shutdown timeout, forcing the asyncio.TimeoutError branch.
        async def _uncancellable() -> None:
            while True:
                try:
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    continue  # swallow cancellation, keep looping

        manager = AsyncResourceManager()
        task = manager.create_task(_uncancellable(), name="stubborn")

        # Act: must return without raising even though the task never stops
        # within the given timeout.
        await manager.shutdown(timeout=0.1)

        # Cleanup: forcibly kill the still-alive task so it doesn't leak
        # into other tests via the event loop.
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=1)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass


class TestCancelTask:
    """cancel_task() targeted cancellation."""

    async def test_cancels_a_tracked_task(self) -> None:
        # Arrange
        manager = AsyncResourceManager()
        task = manager.create_task(asyncio.sleep(10), name="target")

        # Act
        await manager.cancel_task(task)

        # Assert
        assert task.cancelled()

    async def test_untracked_task_is_a_no_op(self) -> None:
        # Arrange: a task never registered via create_task().
        async def _noop() -> None:
            return None

        manager = AsyncResourceManager()
        stray_task = asyncio.create_task(_noop())
        await stray_task

        # Act & Assert: must not raise even though the task isn't tracked.
        await manager.cancel_task(stray_task)
