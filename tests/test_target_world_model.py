"""Tests for TargetWorldModel — visibility tracking, dropout bridging, smoothing."""

from __future__ import annotations

import asyncio

import pytest

from autonomon import PerceptionEvent, TargetWorldModel, WorldStateUpdate


def _vis(
    detected: bool,
    bearing: float | None = None,
    distance: float | None = None,
    confidence: float | None = None,
    vertical_bearing: float | None = None,
) -> PerceptionEvent:
    return PerceptionEvent(
        timestamp="t",
        device_id="d",
        sensor_type="vision",
        data={
            "detected": detected,
            "target_bearing_deg": bearing,
            "target_vertical_bearing_deg": vertical_bearing,
            "target_distance_cm": distance,
            "confidence": confidence,
        },
    )


def _ultra(distance: float | None) -> PerceptionEvent:
    return PerceptionEvent(
        timestamp="t",
        device_id="d",
        sensor_type="ultrasonic",
        data={"distance_cm": distance},
    )


async def _run_events(
    wm: TargetWorldModel, events: list[PerceptionEvent], settle: float = 0.1
) -> list[WorldStateUpdate]:
    q_in: asyncio.Queue[PerceptionEvent] = asyncio.Queue()
    q_out: asyncio.Queue[WorldStateUpdate] = asyncio.Queue()
    task = asyncio.create_task(wm.run(q_in, q_out))
    for e in events:
        await q_in.put(e)
    while not q_in.empty():
        await asyncio.sleep(0.01)
    await asyncio.sleep(settle)
    await wm.stop()
    await task
    out = []
    while not q_out.empty():
        out.append(q_out.get_nowait())
    return out


@pytest.mark.asyncio
async def test_first_detection_emits_visible_state() -> None:
    wm = TargetWorldModel("d", smoothing=1.0)
    out = await _run_events(wm, [_vis(True, 10.0, 100.0, 0.9)])
    assert len(out) == 1
    assert out[0].state["target_visible"] is True
    assert out[0].state["target_bearing_deg"] == pytest.approx(10.0)
    assert out[0].state["target_distance_cm"] == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_target_ages_out_to_not_visible() -> None:
    wm = TargetWorldModel("d", lost_target_timeout_s=0.1, smoothing=1.0)
    q_in: asyncio.Queue[PerceptionEvent] = asyncio.Queue()
    q_out: asyncio.Queue[WorldStateUpdate] = asyncio.Queue()
    task = asyncio.create_task(wm.run(q_in, q_out))

    await q_in.put(_vis(True, 0.0, 90.0, 0.9))
    first = await asyncio.wait_for(q_out.get(), timeout=1.0)
    assert first.state["target_visible"] is True

    # No further detections; after the timeout the model flips to not-visible.
    second = await asyncio.wait_for(q_out.get(), timeout=1.0)
    assert second.state["target_visible"] is False
    assert second.state["target_bearing_deg"] is None

    await wm.stop()
    await task


@pytest.mark.asyncio
async def test_brief_dropout_keeps_target_visible() -> None:
    wm = TargetWorldModel("d", lost_target_timeout_s=1.0, smoothing=1.0)
    out = await _run_events(wm, [_vis(True, 5.0, 80.0, 0.9), _vis(False)])
    # The not-detected frame is within the hold window, so visibility never flips.
    assert len(out) == 1
    assert out[0].state["target_visible"] is True


@pytest.mark.asyncio
async def test_bearing_change_beyond_epsilon_emits() -> None:
    wm = TargetWorldModel("d", smoothing=1.0, emit_bearing_epsilon_deg=2.0)
    out = await _run_events(wm, [_vis(True, 0.0, 100.0, 0.9), _vis(True, 10.0, 100.0, 0.9)])
    assert len(out) == 2
    assert out[0].state["target_bearing_deg"] == pytest.approx(0.0)
    assert out[1].state["target_bearing_deg"] == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_small_jitter_below_epsilon_does_not_emit() -> None:
    wm = TargetWorldModel(
        "d", smoothing=1.0, emit_bearing_epsilon_deg=5.0, emit_distance_epsilon_cm=5.0
    )
    out = await _run_events(wm, [_vis(True, 0.0, 100.0, 0.9), _vis(True, 1.0, 101.0, 0.9)])
    assert len(out) == 1  # the 1°/1cm move is below the emit epsilon


@pytest.mark.asyncio
async def test_ema_smooths_measurements() -> None:
    wm = TargetWorldModel("d", smoothing=0.5, emit_bearing_epsilon_deg=0.1)
    out = await _run_events(wm, [_vis(True, 0.0, 100.0, 0.9), _vis(True, 10.0, 100.0, 0.9)])
    # Second bearing is EMA-smoothed: 0.5*10 + 0.5*0 = 5.0 (not the raw 10).
    assert out[-1].state["target_bearing_deg"] == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_vertical_bearing_tracked_smoothed_and_cleared() -> None:
    wm = TargetWorldModel("d", smoothing=0.5, emit_bearing_epsilon_deg=0.1)
    out = await _run_events(
        wm,
        [
            _vis(True, 0.0, 100.0, 0.9, vertical_bearing=0.0),
            _vis(True, 0.0, 100.0, 0.9, vertical_bearing=10.0),
        ],
    )
    # First emission carries the vertical bearing; second is EMA-smoothed to 5.0,
    # and a vertical-only move past epsilon is enough to emit a new state.
    assert out[0].state["target_vertical_bearing_deg"] == pytest.approx(0.0)
    assert out[-1].state["target_vertical_bearing_deg"] == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_ultrasonic_distance_preferred_over_vision() -> None:
    wm = TargetWorldModel("d", smoothing=1.0)
    # Vision range saturates at 75 cm; a fresh ultrasonic reading of 40 cm wins,
    # so the planner can see the target is too close and back up.
    out = await _run_events(wm, [_vis(True, 0.0, 75.0, 0.9), _ultra(40.0)])
    assert out[-1].state["target_distance_cm"] == pytest.approx(40.0)


@pytest.mark.asyncio
async def test_stale_ultrasonic_falls_back_to_vision_range() -> None:
    # ultrasonic_max_age_s=0 → any ultrasonic reading is stale by the next tick,
    # so the world model falls back to the vision range estimate.
    wm = TargetWorldModel("d", smoothing=1.0, ultrasonic_max_age_s=0.0)
    out = await _run_events(wm, [_ultra(40.0), _vis(True, 0.0, 75.0, 0.9)])
    assert out[-1].state["target_distance_cm"] == pytest.approx(75.0)


@pytest.mark.asyncio
async def test_vertical_bearing_cleared_on_age_out() -> None:
    wm = TargetWorldModel("d", lost_target_timeout_s=0.1, smoothing=1.0)
    out = await _run_events(wm, [_vis(True, 0.0, 90.0, 0.9, vertical_bearing=8.0)])
    # The final emission (after the timeout) reports the target lost with cleared bearings.
    assert out[-1].state["target_visible"] is False
    assert out[-1].state["target_vertical_bearing_deg"] is None
