"""Pipeline: wires the four layers together via LayerSlot instances.

Each layer position in the pipeline is a LayerSlot (or FanInSlot) that owns
its asyncio Task. Queues are created once in run() and persist for the life
of the pipeline — allowing hot-swap via swap_layer() without losing in-flight
messages.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Literal, Union

from autonomon.action.base import ActionBase
from autonomon.fan_in import FanInSlot
from autonomon.perception.base import PerceptionBase
from autonomon.planning.base import PlannerBase
from autonomon.slot import AnyLayer, LayerSlot
from autonomon.world_model.base import WorldModelBase

logger = logging.getLogger(__name__)

_DEFAULT_QUEUE_SIZE = 32

_PerceptionArg = Union[PerceptionBase, FanInSlot]
_WorldModelArg = Union[WorldModelBase, FanInSlot]
_PlannerArg = Union[PlannerBase, FanInSlot]
_ActionArg = Union[ActionBase, FanInSlot]
_AnySlot = Union[LayerSlot, FanInSlot]


def _to_slot(name: str, arg: Union[AnyLayer, FanInSlot]) -> _AnySlot:
    if isinstance(arg, FanInSlot):
        return arg
    return LayerSlot(name, arg)


class Pipeline:
    """Connects Perception → World Model → Planning → Action via asyncio queues.

    Each layer runs in its own asyncio task inside a LayerSlot. Queues are
    bounded (default 32) to create back-pressure: if Action is slow, the
    planner pauses; if the planner pauses, the world model pauses; if the
    world model pauses, perception slows its polling rate.

    To run multiple implementations at one position (e.g. YOLO + ultrasonic
    as concurrent perception sources), pass a FanInSlot instead of a single
    implementation.

    Parameters
    ----------
    perception : PerceptionBase or FanInSlot
    world_model : WorldModelBase or FanInSlot
    planner : PlannerBase or FanInSlot
    action : ActionBase or FanInSlot
    queue_size : int
        Capacity of each inter-layer queue.
    """

    def __init__(
        self,
        perception: _PerceptionArg,
        world_model: _WorldModelArg,
        planner: _PlannerArg,
        action: _ActionArg,
        queue_size: int = _DEFAULT_QUEUE_SIZE,
    ) -> None:
        self._slots: dict[str, _AnySlot] = {
            "perception": _to_slot("perception", perception),
            "world_model": _to_slot("world_model", world_model),
            "planner": _to_slot("planner", planner),
            "action": _to_slot("action", action),
        }
        self._queue_size = queue_size
        self._running = False

    async def run(self) -> None:
        """Start all layers and run until stop() is called or a layer raises."""
        q_perception: asyncio.Queue = asyncio.Queue(maxsize=self._queue_size)
        q_world: asyncio.Queue = asyncio.Queue(maxsize=self._queue_size)
        q_plan: asyncio.Queue = asyncio.Queue(maxsize=self._queue_size)

        self._slots["perception"].start(queue_in=None,         queue_out=q_perception)
        self._slots["world_model"].start(queue_in=q_perception, queue_out=q_world)
        self._slots["planner"].start(queue_in=q_world,         queue_out=q_plan)
        self._slots["action"].start(queue_in=q_plan,           queue_out=None)

        all_tasks = [t for slot in self._slots.values() for t in slot.tasks]
        self._running = True
        logger.info("pipeline started")

        try:
            done, _ = await asyncio.wait(all_tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                exc = task.exception() if not task.cancelled() else None
                if exc:
                    logger.error("layer task '%s' raised: %s", task.get_name(), exc)
                    raise exc
        finally:
            self._running = False
            await self.stop()

    async def stop(self, timeout: float = 2.0) -> None:
        """Signal all layers to stop and await their tasks."""
        for slot in self._slots.values():
            await slot.stop(timeout=timeout)
        logger.info("pipeline stopped")

    async def swap_layer(
        self,
        position: Literal["perception", "world_model", "planner", "action"],
        new_impl: AnyLayer,
        drain_timeout: float = 2.0,
    ) -> None:
        """Replace one layer's implementation without stopping the pipeline.

        In-flight messages already queued between layers are preserved.
        The old implementation's stop() is called before the new one starts.

        Note: swapping a WorldModelBase loses its accumulated state. If
        continuity matters, initialise the replacement with a state snapshot
        before calling swap_layer.

        Parameters
        ----------
        position : str
            Which layer to replace: "perception", "world_model", "planner",
            or "action".
        new_impl : AnyLayer
            Replacement implementation (must satisfy the same base class).
        drain_timeout : float
            Seconds to wait for the old implementation to exit cleanly.

        Raises
        ------
        KeyError
            If position is not a valid layer name.
        TypeError
            If the slot at position is a FanInSlot; use add_impl/remove_impl
            on the FanInSlot directly for dynamic multi-source changes.
        """
        slot = self._slots[position]
        if isinstance(slot, FanInSlot):
            raise TypeError(
                f"Position '{position}' is a FanInSlot. "
                "Use FanInSlot.add_impl() / remove_impl() to modify it at runtime."
            )
        await slot.swap(new_impl, drain_timeout=drain_timeout)
        logger.info("pipeline: '%s' layer swapped to %s", position, type(new_impl).__name__)
