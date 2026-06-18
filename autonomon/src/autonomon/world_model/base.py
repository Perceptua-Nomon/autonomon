"""Abstract base class for the World Model layer."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod


class WorldModelBase(ABC):
    """Consumes PerceptionEvents and maintains current world state.

    Reads PerceptionEvent dicts from queue_in, updates internal state,
    and emits WorldStateUpdate dicts to queue_out only when state changes.
    Delta-based emission keeps the Planning layer from being flooded with
    no-op updates.
    """

    @abstractmethod
    async def run(self, queue_in: asyncio.Queue, queue_out: asyncio.Queue) -> None:  # type: ignore[type-arg]
        """Process perception events and emit world state updates until stopped.

        Parameters
        ----------
        queue_in : asyncio.Queue
            Source of PerceptionEvent dicts from the Perception layer.
        queue_out : asyncio.Queue
            Receives WorldStateUpdate.to_dict() items.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Signal the layer to drain queue_in and return from run()."""
