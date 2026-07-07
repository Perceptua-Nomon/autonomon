"""Tests for OccupancyWorldModel — local costmap marking, decay, derived memory,
and salient-change emission."""

from __future__ import annotations

import asyncio

import pytest

from autonomon import OccupancyWorldModel, PerceptionEvent, WorldStateUpdate


def _ultrasonic(distance_cm: float | None) -> PerceptionEvent:
    return PerceptionEvent(
        timestamp="t", device_id="d", sensor_type="ultrasonic", data={"distance_cm": distance_cm}
    )


def _grayscale(values: list[int | None]) -> PerceptionEvent:
    return PerceptionEvent(
        timestamp="t",
        device_id="d",
        sensor_type="grayscale",
        data={"channels": [0, 1, 2], "values": values},
    )


async def _run_events(
    wm: OccupancyWorldModel, events: list[PerceptionEvent]
) -> list[WorldStateUpdate]:
    q_in: asyncio.Queue[PerceptionEvent] = asyncio.Queue()
    q_out: asyncio.Queue[WorldStateUpdate] = asyncio.Queue()
    task = asyncio.create_task(wm.run(q_in, q_out))
    for ev in events:
        await q_in.put(ev)
    while not q_in.empty():
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.02)
    await wm.stop()
    await task
    updates = []
    while not q_out.empty():
        updates.append(q_out.get_nowait())
    return updates


# --- Deterministic sync tests over _observe / _decay / _snapshot (explicit clock) ---


def test_observe_marks_forward_cell_and_obstacle_flag() -> None:
    wm = OccupancyWorldModel(
        "d", cell_size_cm=10.0, grid_radius_cm=100.0, obstacle_threshold_cm=20.0
    )
    wm._observe(_ultrasonic(20.0), now=100.0)
    snap = wm._snapshot()
    assert snap["obstacle_ahead"] is True
    assert snap["recently_blocked"] is True
    assert snap["occupied_cells"] == 1
    assert snap["nearest_obstacle_cm"] == 20.0
    assert snap["occupancy"] == [{"x": 0, "y": 2}]


def test_reading_beyond_radius_is_clear() -> None:
    wm = OccupancyWorldModel("d", grid_radius_cm=100.0)
    wm._observe(_ultrasonic(150.0), now=100.0)
    snap = wm._snapshot()
    assert snap["obstacle_ahead"] is False
    assert snap["occupied_cells"] == 0
    assert snap["recently_blocked"] is False
    assert snap["nearest_obstacle_cm"] is None


def test_none_reading_marks_nothing() -> None:
    wm = OccupancyWorldModel("d")
    wm._observe(_ultrasonic(None), now=100.0)
    assert wm._snapshot()["occupied_cells"] == 0


def test_decay_expires_cells_after_decay_s() -> None:
    wm = OccupancyWorldModel("d", decay_s=3.0)
    wm._observe(_ultrasonic(30.0), now=100.0)
    wm._decay(now=102.0)  # still within the window
    assert wm._snapshot()["recently_blocked"] is True
    wm._decay(now=104.0)  # > 3 s since last seen → expires
    snap = wm._snapshot()
    assert snap["recently_blocked"] is False
    assert snap["occupied_cells"] == 0


def test_memory_persists_after_obstacle_clears() -> None:
    """recently_blocked stays True after the front reading clears (the value-add)."""
    wm = OccupancyWorldModel("d", obstacle_threshold_cm=20.0, decay_s=3.0)
    wm._observe(_ultrasonic(20.0), now=100.0)  # close obstacle
    wm._observe(_ultrasonic(None), now=100.5)  # echo lost → no longer "ahead"
    wm._decay(now=100.5)
    snap = wm._snapshot()
    assert snap["obstacle_ahead"] is False  # current reading is clear
    assert snap["recently_blocked"] is True  # but the grid remembers


def test_nearest_is_closest_remembered_cell() -> None:
    wm = OccupancyWorldModel("d", cell_size_cm=10.0, grid_radius_cm=100.0)
    wm._observe(_ultrasonic(60.0), now=100.0)
    wm._observe(_ultrasonic(30.0), now=100.1)
    assert wm._snapshot()["nearest_obstacle_cm"] == 30.0
    assert wm._snapshot()["occupied_cells"] == 2


def test_grayscale_sets_cliff_without_marking_grid() -> None:
    wm = OccupancyWorldModel("d", cliff_threshold=200.0)
    wm._observe(_grayscale([600, 30, 500]), now=100.0)
    snap = wm._snapshot()
    assert snap["cliff_detected"] is True
    assert snap["occupied_cells"] == 0  # cliffs are not mapped into the obstacle grid


# --- Async run-loop tests over the queue contract ---


@pytest.mark.asyncio
async def test_baseline_emitted_on_first_event() -> None:
    wm = OccupancyWorldModel("d")
    updates = await _run_events(wm, [_ultrasonic(50.0)])
    assert len(updates) == 1
    assert updates[0].type == "world_state_update"
    assert updates[0].delta == {}
    assert updates[0].state["obstacle_ahead"] is False
    assert set(updates[0].state) == {
        "obstacle_ahead",
        "cliff_detected",
        "recently_blocked",
        "occupied_cells",
        "nearest_obstacle_cm",
        "occupancy",
    }


@pytest.mark.asyncio
async def test_grid_churn_does_not_re_emit() -> None:
    """Different ranges keep obstacle_ahead True; cell churn must not flood the planner."""
    wm = OccupancyWorldModel("d", obstacle_threshold_cm=20.0)
    updates = await _run_events(wm, [_ultrasonic(15.0), _ultrasonic(12.0), _ultrasonic(18.0)])
    assert len(updates) == 1  # one baseline; no re-emit on grid churn
    assert updates[0].state["obstacle_ahead"] is True


@pytest.mark.asyncio
async def test_emits_when_recently_blocked_decays() -> None:
    wm = OccupancyWorldModel("d", obstacle_threshold_cm=20.0, grid_radius_cm=100.0, decay_s=0.2)
    q_in: asyncio.Queue[PerceptionEvent] = asyncio.Queue()
    q_out: asyncio.Queue[WorldStateUpdate] = asyncio.Queue()
    task = asyncio.create_task(wm.run(q_in, q_out))

    await q_in.put(_ultrasonic(15.0))  # obstacle ahead
    first = await asyncio.wait_for(q_out.get(), timeout=1.0)
    assert first.state["obstacle_ahead"] is True
    assert first.state["recently_blocked"] is True

    await q_in.put(_ultrasonic(None))  # echo lost: ahead clears, memory holds
    second = await asyncio.wait_for(q_out.get(), timeout=1.0)
    assert second.state["obstacle_ahead"] is False
    assert second.state["recently_blocked"] is True

    # Idle: after decay_s the remembered cell expires → recently_blocked flips False.
    third = await asyncio.wait_for(q_out.get(), timeout=1.0)
    assert third.state["recently_blocked"] is False
    assert third.delta == {
        "recently_blocked": False,
        "occupied_cells": 0,
        "nearest_obstacle_cm": None,
        "occupancy": [],
    }

    await wm.stop()
    await task
