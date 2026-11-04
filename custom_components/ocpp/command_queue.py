"""OCPP command queue for timeout-triggered reconnect + replay."""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


def profile_purpose(req) -> str | None:
    """Read a SetChargingProfile request's purpose, whatever shape it has.

    1.6 carries a plain dict keyed by the wire names from OcppMisc; 2.0.1
    carries a dataclass or a snake_case dict. An unrecognised shape yields
    None, which only makes coalescing coarser, never wrong in the other
    direction.
    """
    profile = getattr(req, "cs_charging_profiles", None)
    if profile is None:
        profile = getattr(req, "charging_profile", None)
    if isinstance(profile, dict):
        for key in ("chargingProfilePurpose", "charging_profile_purpose"):
            if key in profile:
                value = profile[key]
                return None if value is None else str(value)
        return None
    value = getattr(profile, "charging_profile_purpose", None)
    return None if value is None else str(value)


@dataclass
class QueuedCommand:
    """Represents a queued OCPP call for replay after timeout/reconnect."""

    call_type: str
    call_fn: Callable
    args: tuple = field(default_factory=tuple)
    kwargs: dict = field(default_factory=dict)
    connector_id: int | None = None
    profile_purpose: str | None = None
    created_at: float = field(default_factory=time.monotonic)

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
