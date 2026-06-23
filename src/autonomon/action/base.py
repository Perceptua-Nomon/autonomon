"""Abstract base class for the Action layer."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod

from autonomon.messages import ActionPlan


class ActionBase(ABC):
    """Executes ActionPlans by calling the nomothetic REST API.

    Reads :class:`ActionPlan` instances from queue_in, calls the appropriate
    nomothetic endpoints for each action in priority order, and produces
    ActionResults describing success or failure. This is the only layer
    permitted to make outbound HTTP calls that mutate device state.

    Implementations receive a pre-configured ``httpx.AsyncClient`` at
    construction time (per ADR-002).
    """

    @abstractmethod
    async def run(self, queue_in: asyncio.Queue[ActionPlan]) -> None:
        """Execute action plans until stopped.

        Parameters
        ----------
        queue_in : asyncio.Queue[ActionPlan]
            Source of ActionPlans from the Planning layer.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Signal the layer to finish the current plan and return from run()."""
