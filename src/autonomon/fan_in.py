"""FanInSlot: run several perception sources onto one downstream queue.

Used at the **Perception** position to merge multiple sensor sources (e.g.
ultrasonic + grayscale) so the world model sees every event on one queue. All
sources share the single downstream queue, so back-pressure pauses every source
together.

This is the only multi-source composition autonomon needs today. Competing-planner
arbitration (an ``ARBITRATE`` merge with a timing window and a custom arbiter) and
runtime ``add_impl``/``remove_impl`` were removed as unused speculative machinery —
see ADR-006. If competing planners are ever needed, reintroduce that as a dedicated
planner slot with its decision recorded in a new ADR.
"""

from __future__ import annotations

import asyncio
import logging

from autonomon.slot import AnyLayer

logger = logging.getLogger(__name__)


class FanInSlot:
    """Runs N concurrent perception sources writing to one shared downstream queue.

    Parameters
    ----------
    name : str
        Slot identifier used in log messages and task names.
    impls : list
        Perception implementations to run concurrently. All must be
        ``PerceptionBase`` instances (this slot is a Perception-position
        construct: it has no upstream queue to fan out).
    """

    def __init__(self, name: str, impls: list[AnyLayer]) -> None:
        if not impls:
            raise ValueError(f"FanInSlot '{name}': impls must be non-empty")
        self.name = name
        self._impls: list[AnyLayer] = list(impls)
        self._tasks: list[asyncio.Task] = []  # type: ignore[type-arg]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(
        self,
        queue_in: asyncio.Queue | None,  # type: ignore[type-arg]
        queue_out: asyncio.Queue | None,  # type: ignore[type-arg]
    ) -> list:  # list[asyncio.Task]
        """Start all source tasks; each writes directly to ``queue_out``.

        Parameters
        ----------
        queue_in : asyncio.Queue or None
            Must be None — a fan-in of perception sources has no upstream queue.
        queue_out : asyncio.Queue
            The shared downstream queue all sources write to.
        """
        if queue_in is not None:
            raise ValueError(
                f"FanInSlot '{self.name}': only supported at the Perception position "
                "(queue_in must be None)"
            )
        self._tasks = [
            asyncio.create_task(impl.run(queue_out), name=f"{self.name}:impl:{i}")  # type: ignore[call-arg, arg-type]
            for i, impl in enumerate(self._impls)
        ]
        logger.debug("fan-in slot '%s' started with %d source(s)", self.name, len(self._impls))
        return list(self._tasks)

    async def stop(self, timeout: float = 2.0) -> None:
        """Stop all source implementations and await their tasks."""
        for impl in self._impls:
            try:
                await impl.stop()
            except Exception:
                pass
        for task in list(self._tasks):
            if not task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
        self._tasks = []
        logger.debug("fan-in slot '%s' stopped", self.name)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def tasks(self) -> list:  # list[asyncio.Task]
        return list(self._tasks)
