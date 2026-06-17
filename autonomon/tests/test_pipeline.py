"""Tests for Pipeline queue wiring and graceful shutdown."""
import asyncio

import pytest

from autonomon import (
    ActionBase,
    ActionPlan,
    PerceptionBase,
    PerceptionEvent,
    Pipeline,
    PlannerBase,
    WorldModelBase,
    WorldStateUpdate,
)


class _StubPerception(PerceptionBase):
    def __init__(self, events: list) -> None:
        self._events = events
        self._stop = asyncio.Event()

    async def run(self, queue_out: asyncio.Queue) -> None:
        for e in self._events:
            if self._stop.is_set():
                break
            await queue_out.put(e)
        await self._stop.wait()

    async def stop(self) -> None:
        self._stop.set()


class _StubWorldModel(WorldModelBase):
    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self.received: list = []

    async def run(self, queue_in: asyncio.Queue, queue_out: asyncio.Queue) -> None:
        while not self._stop.is_set():
            try:
                msg = await asyncio.wait_for(queue_in.get(), timeout=0.05)
                self.received.append(msg)
                state = WorldStateUpdate(
                    timestamp="t", device_id="test", state={"raw": msg}
                )
                await queue_out.put(state.to_dict())
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop.set()


class _StubPlanner(PlannerBase):
    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self.received: list = []

    async def run(self, queue_in: asyncio.Queue, queue_out: asyncio.Queue) -> None:
        while not self._stop.is_set():
            try:
                msg = await asyncio.wait_for(queue_in.get(), timeout=0.05)
                self.received.append(msg)
                plan = ActionPlan(timestamp="t", device_id="test", plan_id="p1", actions=[])
                await queue_out.put(plan.to_dict())
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop.set()


class _StubAction(ActionBase):
    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self.received: list = []

    async def run(self, queue_in: asyncio.Queue) -> None:
        while not self._stop.is_set():
            try:
                msg = await asyncio.wait_for(queue_in.get(), timeout=0.05)
                self.received.append(msg)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop.set()


@pytest.mark.asyncio
async def test_pipeline_routes_messages_end_to_end() -> None:
    event = PerceptionEvent(
        timestamp="2026-01-01T00:00:00Z", device_id="nomon-test", sensor_type="test", data={}
    )
    perception = _StubPerception([event.to_dict()])
    world_model = _StubWorldModel()
    planner = _StubPlanner()
    action = _StubAction()

    pipeline = Pipeline(perception, world_model, planner, action)

    async def _run_and_stop() -> None:
        await asyncio.sleep(0.2)
        await pipeline.stop()

    await asyncio.gather(pipeline.run(), _run_and_stop(), return_exceptions=True)

    assert len(world_model.received) >= 1
    assert world_model.received[0]["type"] == "perception_event"
    assert len(planner.received) >= 1
    assert planner.received[0]["type"] == "world_state_update"
    assert len(action.received) >= 1
    assert action.received[0]["type"] == "action_plan"


@pytest.mark.asyncio
async def test_pipeline_stops_cleanly() -> None:
    perception = _StubPerception([])
    pipeline = Pipeline(perception, _StubWorldModel(), _StubPlanner(), _StubAction())

    async def _stop_after() -> None:
        await asyncio.sleep(0.1)
        await pipeline.stop()

    await asyncio.gather(pipeline.run(), _stop_after(), return_exceptions=True)
    assert pipeline._tasks == []
