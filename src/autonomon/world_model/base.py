"""Abstract base class for the World Model layer."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod

from autonomon.messages import PerceptionEvent, WorldStateUpdate


class WorldModelBase(ABC):
    """Consumes PerceptionEvents and maintains current world state.

    Reads :class:`PerceptionEvent` instances from queue_in, updates internal
    state, and emits :class:`WorldStateUpdate` instances to queue_out only when
    state changes. Delta-based emission keeps the Planning layer from being
    flooded with no-op updates.
    """

    @abstractmethod
    async def run(
        self,
        queue_in: asyncio.Queue[PerceptionEvent],
        queue_out: asyncio.Queue[WorldStateUpdate],
    ) -> None:
        """Process perception events and emit world state updates until stopped.

        Parameters
        ----------
        queue_in : asyncio.Queue[PerceptionEvent]
            Source of PerceptionEvents from the Perception layer.
        queue_out : asyncio.Queue[WorldStateUpdate]
            Receives WorldStateUpdate instances.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Signal the layer to drain queue_in and return from run()."""
