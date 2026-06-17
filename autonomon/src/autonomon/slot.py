"""LayerSlot: wraps a single layer implementation to enable runtime hot-swap.

A slot owns its asyncio Task and remembers the queues it was started with.
Calling swap() drains and stops the running implementation, then starts the
replacement using the same queue objects — so in-flight messages are never lost.
"""
from __future__ import annotations

import asyncio
import enum
import logging
from typing import Optional, Union

from autonomon.action.base import ActionBase
from autonomon.perception.base import PerceptionBase
from autonomon.planning.base import PlannerBase
from autonomon.world_model.base import WorldModelBase

logger = logging.getLogger(__name__)

AnyLayer = Union[PerceptionBase, WorldModelBase, PlannerBase, ActionBase]


class SlotState(enum.Enum):
    STOPPED = "stopped"
    RUNNING = "running"
    DRAINING = "draining"


class LayerSlot:
    """Wraps one layer implementation; supports runtime hot-swap via swap().

    Parameters
    ----------
    name : str
        Slot identifier used in log messages and task names.
    impl : AnyLayer
        Initial layer implementation.
    """

    def __init__(self, name: str, impl: AnyLayer) -> None:
        self.name = name
        self._impl = impl
        self._queue_in: Optional[asyncio.Queue] = None   # type: ignore[type-arg]
        self._queue_out: Optional[asyncio.Queue] = None  # type: ignore[type-arg]
        self._task: Optional[asyncio.Task] = None        # type: ignore[type-arg]
        self._state = SlotState.STOPPED
        self._swap_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _make_task(self) -> asyncio.Task:  # type: ignore[type-arg]
        if self._queue_in is None and self._queue_out is not None:
            coro = self._impl.run(self._queue_out)          # Perception
        elif self._queue_out is None and self._queue_in is not None:
            coro = self._impl.run(self._queue_in)           # Action
        elif self._queue_in is not None and self._queue_out is not None:
            coro = self._impl.run(self._queue_in, self._queue_out)  # WorldModel / Planning
        else:
            raise RuntimeError(f"Slot '{self.name}': both queue_in and queue_out are None")
        return asyncio.create_task(coro, name=self.name)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(
        self,
        queue_in: Optional[asyncio.Queue],   # type: ignore[type-arg]
        queue_out: Optional[asyncio.Queue],  # type: ignore[type-arg]
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
        """Signal the current implementation to stop and await its task.

        Parameters
        ----------
        timeout : float
            Seconds to wait for graceful shutdown before cancelling the task.
        """
        if self._state == SlotState.STOPPED:
            return
        self._state = SlotState.DRAINING
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

    async def swap(self, new_impl: AnyLayer, drain_timeout: float = 2.0) -> None:
        """Replace the running implementation without stopping the pipeline.

        The queues assigned at start() are reused. In-flight messages already
        queued remain intact and will be consumed by the downstream layer as
        normal. The old implementation's stop() is called before the new one
        starts.

        Parameters
        ----------
        new_impl : AnyLayer
            Replacement implementation (must be the same layer type).
        drain_timeout : float
            Seconds to wait for the old implementation to exit cleanly.
        """
        async with self._swap_lock:
            logger.info("slot '%s': swapping implementation", self.name)
            await self.stop(timeout=drain_timeout)
            self._impl = new_impl
            self._task = self._make_task()
            self._state = SlotState.RUNNING
            logger.info("slot '%s': swap complete", self.name)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def impl(self) -> AnyLayer:
        return self._impl

    @property
    def tasks(self) -> list:  # list[asyncio.Task]
        return [self._task] if self._task is not None else []
