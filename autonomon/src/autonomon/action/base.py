"""Abstract base class for the Action layer."""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod


class ActionBase(ABC):
    """Executes ActionPlans by calling the nomothetic REST API.

    Reads ActionPlan dicts from queue_in, calls the appropriate nomothetic
    endpoints for each action in priority order, and emits ActionResult dicts
    describing success or failure. This is the only layer permitted to make
    outbound HTTP calls that mutate device state.

    Implementations receive the device URL and auth token at construction time.
    """

    @abstractmethod
    async def run(self, queue_in: asyncio.Queue) -> None:  # type: ignore[type-arg]
        """Execute action plans until stopped.

        Parameters
        ----------
        queue_in : asyncio.Queue
            Source of ActionPlan dicts from the Planning layer.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Signal the layer to finish the current plan and return from run()."""
