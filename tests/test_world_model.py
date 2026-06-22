"""Tests for ObstacleWorldModel — threshold fusion and delta-based emission."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from autonomon import ObstacleWorldModel, PerceptionEvent


def _ultrasonic(distance_cm: float | None) -> dict[str, Any]:
    return PerceptionEvent(
        timestamp="t", device_id="d", sensor_type="ultrasonic", data={"distance_cm": distance_cm}
    ).to_dict()


def _grayscale(normalized: list[float | None]) -> dict[str, Any]:
    return PerceptionEvent(
        timestamp="t",
        device_id="d",
        sensor_type="grayscale",
        data={"channels": [0, 1, 2], "normalized": normalized},
    ).to_dict()


async def _run_events(wm: ObstacleWorldModel, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Push events through the world model; return the emitted updates."""
    q_in: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    q_out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    task = asyncio.create_task(wm.run(q_in, q_out))
    for ev in events:
        await q_in.put(ev)
    # Let the model drain the input queue.
    while not q_in.empty():
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.02)
    await wm.stop()
    await task
    updates = []
    while not q_out.empty():
        updates.append(q_out.get_nowait())
    return updates


@pytest.mark.asyncio
async def test_obstacle_detected_below_threshold() -> None:
    wm = ObstacleWorldModel(device_id="nomon-test", obstacle_threshold_cm=20.0)
    updates = await _run_events(wm, [_ultrasonic(15.0)])

    assert len(updates) == 1
    assert updates[0]["type"] == "world_state_update"
    assert updates[0]["state"]["obstacle_ahead"] is True
    assert updates[0]["delta"] == {"obstacle_ahead": True}


@pytest.mark.asyncio
async def test_first_observation_emits_baseline() -> None:
    """The first reading always emits a baseline (empty delta) so the planner starts."""
    wm = ObstacleWorldModel(device_id="nomon-test", obstacle_threshold_cm=20.0)
    updates = await _run_events(wm, [_ultrasonic(50.0)])

    assert len(updates) == 1
    assert updates[0]["delta"] == {}  # baseline: nothing changed from defaults
    assert updates[0]["state"] == {"obstacle_ahead": False, "cliff_detected": False}


@pytest.mark.asyncio
async def test_repeated_clear_after_baseline_is_noop() -> None:
    """After the baseline, identical clear readings emit nothing."""
    wm = ObstacleWorldModel(device_id="nomon-test", obstacle_threshold_cm=20.0)
    updates = await _run_events(wm, [_ultrasonic(50.0), _ultrasonic(60.0)])

    # One baseline emission only; the second clear reading is a no-op.
    assert len(updates) == 1
    assert updates[0]["delta"] == {}


@pytest.mark.asyncio
async def test_none_distance_is_clear() -> None:
    """A None reading (no echo / out of range) must not register an obstacle."""
    wm = ObstacleWorldModel(device_id="nomon-test", obstacle_threshold_cm=20.0)
    # First trip an obstacle, then send None — should clear back to False.
    updates = await _run_events(wm, [_ultrasonic(10.0), _ultrasonic(None)])

    assert len(updates) == 2
    assert updates[0]["delta"] == {"obstacle_ahead": True}
    assert updates[1]["delta"] == {"obstacle_ahead": False}


@pytest.mark.asyncio
async def test_delta_emission_only_on_change() -> None:
    """Repeated identical readings emit exactly one update."""
    wm = ObstacleWorldModel(device_id="nomon-test", obstacle_threshold_cm=20.0)
    updates = await _run_events(wm, [_ultrasonic(10.0), _ultrasonic(12.0), _ultrasonic(8.0)])

    # All three are "obstacle"; only the first transition emits.
    assert len(updates) == 1
    assert updates[0]["delta"] == {"obstacle_ahead": True}


@pytest.mark.asyncio
async def test_state_snapshot_is_full_not_just_delta() -> None:
    wm = ObstacleWorldModel(device_id="nomon-test")
    updates = await _run_events(wm, [_ultrasonic(5.0)])

    state = updates[0]["state"]
    assert set(state) == {"obstacle_ahead", "cliff_detected"}
    assert state["obstacle_ahead"] is True
    assert state["cliff_detected"] is False


@pytest.mark.asyncio
async def test_grayscale_cliff_detection() -> None:
    # High normalised reading = non-reflective = no surface = cliff (>= threshold).
    wm = ObstacleWorldModel(device_id="nomon-test", cliff_threshold=0.7)
    updates = await _run_events(wm, [_grayscale([0.2, 0.9, 0.3])])

    assert len(updates) == 1
    assert updates[0]["delta"] == {"cliff_detected": True}
    assert updates[0]["state"]["cliff_detected"] is True


@pytest.mark.asyncio
async def test_grayscale_reflective_floor_is_not_cliff() -> None:
    """Low normalised readings = a reflective surface is present = NOT a cliff.

    Regression guard for the convention (nomopractic: 0.0 reflective, 1.0
    non-reflective/edge). The pre-fix code triggered a cliff here, with the
    comparison inverted.
    """
    wm = ObstacleWorldModel(device_id="nomon-test", cliff_threshold=0.7)
    updates = await _run_events(wm, [_grayscale([0.05, 0.1, 0.2])])

    assert len(updates) == 1
    assert updates[0]["delta"] == {}  # baseline only; no cliff over a real floor
    assert updates[0]["state"]["cliff_detected"] is False


@pytest.mark.asyncio
async def test_grayscale_default_threshold_trips_on_high_reading() -> None:
    """With the default 0.7 threshold, a high (non-reflective) reading is a cliff."""
    wm = ObstacleWorldModel(device_id="nomon-test")  # default cliff_threshold = 0.7
    updates = await _run_events(wm, [_grayscale([0.1, 0.1, 0.85])])

    assert updates[-1]["state"]["cliff_detected"] is True


@pytest.mark.asyncio
async def test_grayscale_none_channel_is_tolerated() -> None:
    """A None channel (dropped reading) must not crash the world model."""
    wm = ObstacleWorldModel(device_id="nomon-test", cliff_threshold=0.7)
    # 0.95 >= 0.7 -> cliff; the None element must be skipped, not raise TypeError.
    updates = await _run_events(wm, [_grayscale([0.95, None, 0.3])])

    assert len(updates) == 1
    assert updates[0]["delta"] == {"cliff_detected": True}


@pytest.mark.asyncio
async def test_grayscale_all_none_is_clear_not_crash() -> None:
    wm = ObstacleWorldModel(device_id="nomon-test", cliff_threshold=0.7)
    updates = await _run_events(wm, [_grayscale([None, None, None])])

    # No numeric channel trips the threshold; baseline emit with no cliff.
    assert len(updates) == 1
    assert updates[0]["state"]["cliff_detected"] is False


@pytest.mark.asyncio
async def test_independent_obstacle_and_cliff_fields() -> None:
    wm = ObstacleWorldModel(device_id="nomon-test", obstacle_threshold_cm=20.0, cliff_threshold=0.7)
    updates = await _run_events(wm, [_ultrasonic(10.0), _grayscale([0.95, 0.1, 0.1])])

    assert len(updates) == 2
    assert updates[0]["delta"] == {"obstacle_ahead": True}
    assert updates[1]["delta"] == {"cliff_detected": True}
    # Final snapshot carries both.
    assert updates[1]["state"] == {"obstacle_ahead": True, "cliff_detected": True}
