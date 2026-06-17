"""Pipeline: wires the four layers together with bounded asyncio queues."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from autonomon.action.base import ActionBase
from autonomon.perception.base import PerceptionBase
from autonomon.planning.base import PlannerBase
from autonomon.world_model.base import WorldModelBase

logger = logging.getLogger(__name__)

_DEFAULT_QUEUE_SIZE = 32


class Pipeline:
    """Connects Perception → World Model → Planning → Action via asyncio queues.

    Each layer runs as a separate asyncio task. Queues are bounded (default 32)
    to create back-pressure: if Action is slow, the planner pauses; if the
    planner pauses, the world model pauses; if the world model pauses, perception
    slows its polling rate.

    Parameters
    ----------
    perception : PerceptionBase
        Perception layer implementation.
    world_model : WorldModelBase
        World model layer implementation.
    planner : PlannerBase
        Planning layer implementation.
    action : ActionBase
        Action layer implementation.
    queue_size : int
        Capacity of each inter-layer queue.
    """

    def __init__(
        self,
        perception: PerceptionBase,
        world_model: WorldModelBase,
        planner: PlannerBase,
        action: ActionBase,
        queue_size: int = _DEFAULT_QUEUE_SIZE,
    ) -> None:
        self._perception = perception
        self._world_model = world_model
        self._planner = planner
        self._action = action
        self._queue_size = queue_size
        self._tasks: list[asyncio.Task] = []  # type: ignore[type-arg]

    async def run(self) -> None:
        """Start all layers and run until stop() is called or a layer raises."""
        q_perception: asyncio.Queue = asyncio.Queue(maxsize=self._queue_size)
        q_world: asyncio.Queue = asyncio.Queue(maxsize=self._queue_size)
        q_plan: asyncio.Queue = asyncio.Queue(maxsize=self._queue_size)

        self._tasks = [
            asyncio.create_task(
                self._perception.run(q_perception), name="perception"
            ),
            asyncio.create_task(
                self._world_model.run(q_perception, q_world), name="world_model"
            ),
            asyncio.create_task(
                self._planner.run(q_world, q_plan), name="planner"
            ),
            asyncio.create_task(
                self._action.run(q_plan), name="action"
            ),
        ]

        logger.info("pipeline started")
        try:
            done, pending = await asyncio.wait(
                self._tasks, return_when=asyncio.FIRST_EXCEPTION
            )
            for task in done:
                if task.exception():
                    logger.error(
                        "layer %s raised: %s", task.get_name(), task.exception()
                    )
                    raise task.exception()  # type: ignore[misc]
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Signal all layers to stop and cancel their tasks."""
        for layer in (self._perception, self._world_model, self._planner, self._action):
            try:
                await layer.stop()
            except Exception:
                pass

        for task in self._tasks:
            if not task.done():
                task.cancel()

        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

        self._tasks = []
        logger.info("pipeline stopped")
