"""Abstract base class for the Planning layer."""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod


class PlannerBase(ABC):
    """Evaluates world state and selects action plans.

    Reads WorldStateUpdate dicts from queue_in and emits ActionPlan dicts
    to queue_out when the optimal plan changes. Implementations should be
    pure logic with no I/O — this makes the planner fully testable without
    a mock HTTP client or device connection.
    """

    @abstractmethod
    async def run(self, queue_in: asyncio.Queue, queue_out: asyncio.Queue) -> None:  # type: ignore[type-arg]
        """Evaluate world state and emit action plans until stopped.

        Parameters
        ----------
        queue_in : asyncio.Queue
            Source of WorldStateUpdate dicts from the World Model layer.
        queue_out : asyncio.Queue
            Receives ActionPlan.to_dict() items.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Signal the layer to drain queue_in and return from run()."""
