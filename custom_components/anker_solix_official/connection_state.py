"""Connection state management for Modbus connections."""
import asyncio
import logging
from enum import Enum
from typing import Optional


class ConnectionState(Enum):
    """Enumeration of possible connection states."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    ERROR = "error"
    CLOSING = "closing"


class ConnectionStateMachine:
    """State machine for managing connection lifecycle.

    This class provides thread-safe state transitions and state waiting
    capabilities for Modbus connection management.
    """

    # Valid state transitions (from_state -> [to_states])
    _VALID_TRANSITIONS = {
        ConnectionState.DISCONNECTED: [
            ConnectionState.CONNECTING,
            ConnectionState.CLOSING,
        ],
        ConnectionState.CONNECTING: [
            ConnectionState.CONNECTED,
            ConnectionState.ERROR,
            ConnectionState.DISCONNECTED,
            ConnectionState.CLOSING,
        ],
        ConnectionState.CONNECTED: [
            ConnectionState.DISCONNECTED,
            ConnectionState.RECONNECTING,
            ConnectionState.ERROR,
            ConnectionState.CLOSING,
        ],
        ConnectionState.RECONNECTING: [
            ConnectionState.CONNECTED,
            ConnectionState.ERROR,
            ConnectionState.DISCONNECTED,
            ConnectionState.CLOSING,
        ],
        ConnectionState.ERROR: [
            ConnectionState.RECONNECTING,
            ConnectionState.DISCONNECTED,
            ConnectionState.CLOSING,
        ],
        ConnectionState.CLOSING: [
            ConnectionState.DISCONNECTED,
        ],
    }

    def __init__(self, initial_state: ConnectionState = ConnectionState.DISCONNECTED):
        """Initialize state machine.

        Args:
            initial_state: Initial connection state (default: DISCONNECTED)
        """
        self._state = initial_state
        self._lock = asyncio.Lock()
        self._state_changed = asyncio.Event()
        self._logger = logging.getLogger(__name__)

    @property
    def current_state(self) -> ConnectionState:
        """Get current connection state (thread-safe read)."""
        return self._state

    def _is_valid_transition(
        self, from_state: ConnectionState, to_state: ConnectionState
    ) -> bool:
        """Check if state transition is valid.

        Args:
            from_state: Current state
            to_state: Target state

        Returns:
            True if transition is valid, False otherwise
        """
        valid_targets = self._VALID_TRANSITIONS.get(from_state, [])
        return to_state in valid_targets

    async def transition_to(self, new_state: ConnectionState) -> bool:
        """Attempt to transition to a new state.

        Args:
            new_state: Target state

        Returns:
            True if transition succeeded, False if transition was invalid
        """
        async with self._lock:
            if not self._is_valid_transition(self._state, new_state):
                self._logger.warning(
                    "Invalid state transition: %s -> %s",
                    self._state.value,
                    new_state.value,
                )
                return False

            old_state = self._state
            self._state = new_state
            self._state_changed.set()
            self._state_changed.clear()

            self._logger.debug(
                "State transition: %s -> %s", old_state.value, new_state.value
            )
            return True

    async def wait_for_state(
        self, target_state: ConnectionState, timeout: Optional[float] = None
    ) -> bool:
        """Wait for state machine to reach a specific state.

        Args:
            target_state: State to wait for
            timeout: Maximum time to wait in seconds (None = no timeout)

        Returns:
            True if target state was reached, False if timeout occurred
        """
        if self._state == target_state:
            return True

        try:
            if timeout:
                async with asyncio.timeout(timeout):
                    while self._state != target_state:
                        await self._state_changed.wait()
            else:
                while self._state != target_state:
                    await self._state_changed.wait()
            return True
        except asyncio.TimeoutError:
            self._logger.debug(
                "Timeout waiting for state %s (current: %s)",
                target_state.value,
                self._state.value,
            )
            return False

    def is_connected(self) -> bool:
        """Check if currently in CONNECTED state.

        Returns:
            True if state is CONNECTED
        """
        return self._state == ConnectionState.CONNECTED

    def is_error(self) -> bool:
        """Check if currently in ERROR state.

        Returns:
            True if state is ERROR
        """
        return self._state == ConnectionState.ERROR

    def is_disconnected(self) -> bool:
        """Check if currently in DISCONNECTED state.

        Returns:
            True if state is DISCONNECTED
        """
        return self._state == ConnectionState.DISCONNECTED

    def reset(self) -> None:
        """Reset state machine to DISCONNECTED."""
        self._state = ConnectionState.DISCONNECTED
        self._state_changed.set()
        self._state_changed.clear()
