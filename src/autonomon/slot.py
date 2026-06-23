"""LayerSlot: owns one layer implementation's asyncio Task and its queues.

The :class:`~autonomon.pipeline.Pipeline` wraps every layer position in a
``LayerSlot``. The slot builds the layer's task from the queues it is started
with and stops it cleanly on shutdown.

Runtime hot-swap (replacing a layer mid-run) was removed as unused — no routine
swapped a layer at runtime, and the registry/factory already selects layer
implementations per routine at wiring time. See ADR-006.
"""

from __future__ import annotations

import asyncio
import enum
import logging
from typing import Union

from autonomon.action.base import ActionBase
from autonomon.perception.base import PerceptionBase
from autonomon.planning.base import PlannerBase
from autonomon.world_model.base import WorldModelBase

logger = logging.getLogger(__name__)

AnyLayer = Union[PerceptionBase, WorldModelBase, PlannerBase, ActionBase]


class SlotState(enum.Enum):
    STOPPED = "stopped"
    RUNNING = "running"


class LayerSlot:
    """Owns one layer implementation and the asyncio Task running it.

    Parameters
    ----------
    name : str
        Slot identifier used in log messages and task names.
    impl : AnyLayer
        Layer implementation to run.
    """

    def __init__(self, name: str, impl: AnyLayer) -> None:
        self.name = name
        self._impl = impl
        self._queue_in: asyncio.Queue | None = None  # type: ignore[type-arg]
        self._queue_out: asyncio.Queue | None = None  # type: ignore[type-arg]
        self._task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._state = SlotState.STOPPED

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _make_task(self) -> asyncio.Task:  # type: ignore[type-arg]
        if self._queue_in is None and self._queue_out is not None:
            coro = self._impl.run(self._queue_out)  # type: ignore[call-arg]  # Perception
        elif self._queue_out is None and self._queue_in is not None:
            coro = self._impl.run(self._queue_in)  # type: ignore[call-arg]  # Action
        elif self._queue_in is not None and self._queue_out is not None:
            coro = self._impl.run(self._queue_in, self._queue_out)  # type: ignore[call-arg]
        else:
            raise RuntimeError(f"Slot '{self.name}': both queue_in and queue_out are None")
        return asyncio.create_task(coro, name=self.name)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(
        self,
        queue_in: asyncio.Queue | None,  # type: ignore[type-arg]
        queue_out: asyncio.Queue | None,  # type: ignore[type-arg]
    ) -> asyncio.Task:  # type: ignore[type-arg]
        """Assign queues and start the layer task.

        Parameters
        ----------
        queue_in : asyncio.Queue or None
            Incoming message queue. None for the Perception position.
        queue_out : asyncio.Queue or None
            Outgoing message queue. None for the Action position.
        """
        self._queue_in = queue_in
        self._queue_out = queue_out
        self._task = self._make_task()
        self._state = SlotState.RUNNING
        logger.debug("slot '%s' started", self.name)
        return self._task

    async def stop(self, timeout: float = 2.0) -> None:
        """Signal the implementation to stop and await its task.

        Parameters
        ----------
        timeout : float
            Seconds to wait for graceful shutdown before cancelling the task.
        """
        if self._state == SlotState.STOPPED:
            return
        try:
            await self._impl.stop()
        except Exception:
            pass
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass
        self._state = SlotState.STOPPED
        logger.debug("slot '%s' stopped", self.name)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def impl(self) -> AnyLayer:
        return self._impl

    @property
    def tasks(self) -> list:  # list[asyncio.Task]
        return [self._task] if self._task is not None else []
