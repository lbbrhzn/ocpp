"""OCPP command queue for timeout-triggered reconnect + replay."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

_LOGGER = None


@dataclass
class QueuedCommand:
    """Represents a queued OCPP call for replay after timeout/reconnect."""

    call_type: str
    call_fn: Callable
    args: tuple = field(default_factory=tuple)
    kwargs: dict = field(default_factory=dict)
    connector_id: int | None = None
    profile_purpose: str | None = None
    created_at: float = field(default_factory=lambda: __import__("time").time())

    async def execute(self) -> Any:
        """Execute the queued call."""
        return await self.call_fn(*self.args, **self.kwargs)


class CommandQueue:
    """Queue with FIFO ordering and coalescing by command type + connector."""

    def __init__(self):
        """Initialize the command queue."""
        self._queue: list[QueuedCommand] = []
        self._lock = asyncio.Lock()

    async def enqueue(self, command: QueuedCommand) -> None:
        """Enqueue a command, coalescing by type+connector if applicable.

        For commands tied to a specific connector (e.g., SetChargingProfile),
        drop any older command of the same type for that connector.
        """
        async with self._lock:
            if command.call_type in (
                "SetChargingProfile",
                "RemoteStartTransaction",
                "RemoteStopTransaction",
            ):
                # Coalesce: remove older command of same type for same connector
                # For SetChargingProfile, also match profile purpose to avoid dropping
                # active TxProfile when TxDefaultProfile is queued
                self._queue = [
                    cmd
                    for cmd in self._queue
                    if not (
                        cmd.call_type == command.call_type
                        and cmd.connector_id == command.connector_id
                        and (
                            command.call_type != "SetChargingProfile"
                            or cmd.profile_purpose == command.profile_purpose
                        )
                    )
                ]
            self._queue.append(command)

    async def dequeue_all(self) -> list[QueuedCommand]:
        """Drain the entire queue and return commands in FIFO order."""
        async with self._lock:
            commands = self._queue[:]
            self._queue.clear()
            return commands

    async def clear(self) -> None:
        """Clear the queue."""
        async with self._lock:
            self._queue.clear()

    def is_empty(self) -> bool:
        """Check if the queue is empty."""
        return len(self._queue) == 0
