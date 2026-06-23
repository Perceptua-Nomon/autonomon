"""Abstract base class for the Perception layer."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod

from autonomon.messages import PerceptionEvent


class PerceptionBase(ABC):
    """Polls device sensors and emits PerceptionEvents to queue_out.

    Implementations call the nomothetic REST API at a configured interval,
    normalise raw sensor values, and put :class:`PerceptionEvent` instances onto
    queue_out. The layer runs until stop() is called.

    Implementors must not import nomopractic or communicate via the HAT IPC
    socket directly — all sensor access goes through the nomothetic REST API.
    """

    @abstractmethod
    async def run(self, queue_out: asyncio.Queue[PerceptionEvent]) -> None:
        """Poll sensors and emit PerceptionEvents until stopped.

        Parameters
        ----------
        queue_out : asyncio.Queue[PerceptionEvent]
            Receives PerceptionEvent instances.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Signal the layer to finish its current poll and return from run()."""
