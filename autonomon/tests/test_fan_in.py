"""Tests for FanInSlot multi-source and arbitration behaviour."""

import asyncio
from typing import Any

import pytest

from autonomon import MergeStrategy, PerceptionBase, PlannerBase, WorldModelBase
from autonomon.fan_in import FanInSlot
from autonomon.messages import ActionPlan, PerceptionEvent, WorldStateUpdate


class _TaggedPerception(PerceptionBase):
    def __init__(self, tag: str, count: int = 5) -> None:
        self.tag = tag
        self._count = count
        self._stop = asyncio.Event()

    async def run(self, queue_out: asyncio.Queue) -> None:
        for i in range(self._count):
            if self._stop.is_set():
                break
            ev = PerceptionEvent(
                timestamp="t", device_id="test", sensor_type=self.tag, data={"i": i}
            )
            await queue_out.put(ev.to_dict())
            await asyncio.sleep(0.005)
        await self._stop.wait()

    async def stop(self) -> None:
        self._stop.set()


class _PassThroughWorldModel(WorldModelBase):
    def __init__(self) -> None:
        self._stop = asyncio.Event()

    async def run(self, queue_in: asyncio.Queue, queue_out: asyncio.Queue) -> None:
        while not self._stop.is_set():
            try:
                msg = await asyncio.wait_for(queue_in.get(), timeout=0.05)
                update = WorldStateUpdate(timestamp="t", device_id="test", state={"raw": msg})
                await queue_out.put(update.to_dict())
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop.set()


class _ConfidentPlanner(PlannerBase):
    def __init__(self, confidence: float) -> None:
        self._confidence = confidence
        self._stop = asyncio.Event()

    async def run(self, queue_in: asyncio.Queue, queue_out: asyncio.Queue) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(queue_in.get(), timeout=0.05)
                plan = ActionPlan(
                    timestamp="t",
                    device_id="test",
                    plan_id=f"p-{self._confidence}",
                    actions=[{"confidence": self._confidence}],
                )
                await queue_out.put(plan.to_dict())
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------------------
# PASS_THROUGH — Perception fan-in
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fan_in_perception_pass_through() -> None:
    """Both perception sources emit to the same queue; all messages reach downstream."""
    src_a = _TaggedPerception("ultrasonic", count=3)
    src_b = _TaggedPerception("grayscale", count=3)
    q_out: asyncio.Queue = asyncio.Queue(maxsize=64)

    slot = FanInSlot("perception", [src_a, src_b], MergeStrategy.PASS_THROUGH)
    slot.start(queue_in=None, queue_out=q_out)

    await asyncio.sleep(0.15)
    await slot.stop()

    sensor_types = set()
    while not q_out.empty():
        sensor_types.add(q_out.get_nowait()["sensor_type"])

    assert "ultrasonic" in sensor_types
    assert "grayscale" in sensor_types


# ---------------------------------------------------------------------------
# PASS_THROUGH — WorldModel fan-in (both see all events via dispatcher)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fan_in_world_model_both_see_all_events() -> None:
    """Two world models share a fan-out queue_in; both receive every event."""
    wm_a = _PassThroughWorldModel()
    wm_b = _PassThroughWorldModel()
    q_in: asyncio.Queue = asyncio.Queue(maxsize=32)
    q_out: asyncio.Queue = asyncio.Queue(maxsize=64)

    slot = FanInSlot("world_model", [wm_a, wm_b], MergeStrategy.PASS_THROUGH)
    slot.start(queue_in=q_in, queue_out=q_out)

    # Put 3 events in
    for i in range(3):
        ev = PerceptionEvent(timestamp="t", device_id="d", sensor_type="test", data={"i": i})
        await q_in.put(ev.to_dict())

    await asyncio.sleep(0.15)
    await slot.stop()

    # Both world models should have processed each event → 2 × 3 = 6 updates
    count = 0
    while not q_out.empty():
        q_out.get_nowait()
        count += 1

    assert count == 6


# ---------------------------------------------------------------------------
# ARBITRATE — Planning fan-in
# ---------------------------------------------------------------------------


def _pick_highest_confidence(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    return max(candidates, key=lambda p: p["actions"][0]["confidence"])


@pytest.mark.asyncio
async def test_fan_in_planning_arbitrate_picks_best() -> None:
    """Arbiter selects the plan with the highest confidence from competing planners."""
    low_planner = _ConfidentPlanner(confidence=0.3)
    high_planner = _ConfidentPlanner(confidence=0.9)
    q_in: asyncio.Queue = asyncio.Queue(maxsize=32)
    q_out: asyncio.Queue = asyncio.Queue(maxsize=64)

    slot = FanInSlot(
        "planner",
        [low_planner, high_planner],
        MergeStrategy.ARBITRATE,
        arbiter=_pick_highest_confidence,
        arbitration_window_ms=80,
    )
    slot.start(queue_in=q_in, queue_out=q_out)

    # Feed 2 world-state events
    for _ in range(2):
        update = WorldStateUpdate(timestamp="t", device_id="d", state={})
        await q_in.put(update.to_dict())

    await asyncio.sleep(0.3)
    await slot.stop()

    plans = []
    while not q_out.empty():
        plans.append(q_out.get_nowait())

    assert len(plans) >= 1
    for plan in plans:
        assert plan["actions"][0]["confidence"] == 0.9


# ---------------------------------------------------------------------------
# Dynamic add / remove
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fan_in_add_remove_impl() -> None:
    src_a = _TaggedPerception("a", count=100)
    src_b = _TaggedPerception("b", count=100)
    q_out: asyncio.Queue = asyncio.Queue(maxsize=128)

    slot = FanInSlot("perception", [src_a], MergeStrategy.PASS_THROUGH)
    slot.start(queue_in=None, queue_out=q_out)
    assert len(slot._impls) == 1

    # Add a second source
    await slot.add_impl(src_b)
    assert len(slot._impls) == 2

    await asyncio.sleep(0.05)

    # Remove original source
    await slot.remove_impl(src_a)
    assert len(slot._impls) == 1
    assert slot._impls[0] is src_b

    await slot.stop()

    sensor_types = set()
    while not q_out.empty():
        sensor_types.add(q_out.get_nowait()["sensor_type"])

    assert "a" in sensor_types
    assert "b" in sensor_types
