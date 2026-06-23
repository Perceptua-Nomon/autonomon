"""Abstract base class for the Planning layer."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod

from autonomon.messages import ActionPlan, WorldStateUpdate


class PlannerBase(ABC):
    """Evaluates world state and selects action plans.

    Reads :class:`WorldStateUpdate` instances from queue_in and emits
    :class:`ActionPlan` instances to queue_out when the optimal plan changes.
    Implementations should be pure logic with no I/O — this makes the planner
    fully testable without a mock HTTP client or device connection.
    """

    @abstractmethod
    async def run(
        self,
        queue_in: asyncio.Queue[WorldStateUpdate],
        queue_out: asyncio.Queue[ActionPlan],
    ) -> None:
        """Evaluate world state and emit action plans until stopped.

        Parameters
        ----------
        queue_in : asyncio.Queue[WorldStateUpdate]
            Source of WorldStateUpdates from the World Model layer.
        queue_out : asyncio.Queue[ActionPlan]
            Receives ActionPlan instances.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Signal the layer to drain queue_in and return from run()."""
