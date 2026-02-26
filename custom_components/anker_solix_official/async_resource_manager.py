"""Async resource manager for managing background tasks and cleanup."""
import asyncio
import logging
from typing import Coroutine, Optional, Set


class AsyncResourceManager:
    """Manager for async resources with proper cleanup.

    This class ensures all background tasks are properly cancelled and
    cleaned up using a three-phase shutdown process:
    1. Cancel all tasks
    2. Wait for cancellation with timeout
    3. Collect and log exceptions
    """

    def __init__(self, cleanup_timeout: float = 10.0):
        """Initialize resource manager.

        Args:
            cleanup_timeout: Timeout in seconds for task cleanup (default: 10.0)
        """
        self._background_tasks: Set[asyncio.Task] = set()
        self._cleanup_timeout = cleanup_timeout
        self._logger = logging.getLogger(__name__)
        self._lock = asyncio.Lock()

    def create_task(
        self, coro: Coroutine, *, name: Optional[str] = None
    ) -> asyncio.Task:
        """Create and track a background task.

        Args:
            coro: Coroutine to run as background task
            name: Optional task name for debugging

        Returns:
            The created asyncio.Task
        """
        task = asyncio.create_task(coro, name=name)
        self._background_tasks.add(task)

        # Remove task from tracking when it completes
        task.add_done_callback(self._background_tasks.discard)

        return task

    async def shutdown(self, timeout: Optional[float] = None) -> None:
        """Shutdown all background tasks with proper cleanup.

        Uses three-phase shutdown:
        1. Cancel all running tasks
        2. Wait for tasks to complete (with timeout)
        3. Collect and log any exceptions

        Args:
            timeout: Timeout in seconds (uses cleanup_timeout if not specified)
        """
        if not self._background_tasks:
            return

        timeout = timeout or self._cleanup_timeout

        async with self._lock:
            # Phase 1: Cancel all tasks
            self._logger.debug(
                "Shutting down %d background tasks", len(self._background_tasks)
            )
            for task in self._background_tasks:
                if not task.done():
                    task.cancel()

            # Phase 2: Wait for cancellation with timeout
            try:
                async with asyncio.timeout(timeout):
                    await asyncio.gather(*self._background_tasks, return_exceptions=True)
            except asyncio.TimeoutError:
                self._logger.warning(
                    "Timeout waiting for %d tasks to complete", len(self._background_tasks)
                )

            # Phase 3: Collect exceptions and log errors
            for task in self._background_tasks:
                if task.done() and not task.cancelled():
                    try:
                        task.result()
                    except asyncio.CancelledError:
                        pass  # Expected for cancelled tasks
                    except Exception as e:
                        self._logger.error(
                            "Background task '%s' failed: %s",
                            task.get_name(),
                            e,
                            exc_info=True,
                        )

            # Clear task set
            self._background_tasks.clear()
            self._logger.debug("All background tasks shut down")

    async def cancel_task(self, task: asyncio.Task) -> None:
        """Cancel a specific task.

        Args:
            task: Task to cancel
        """
        if task in self._background_tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def get_running_tasks(self) -> Set[asyncio.Task]:
        """Get set of currently running tasks.

        Returns:
            Set of running tasks (copy)
        """
        return self._background_tasks.copy()

    def task_count(self) -> int:
        """Get count of tracked tasks.

        Returns:
            Number of tracked tasks
        """
        return len(self._background_tasks)
