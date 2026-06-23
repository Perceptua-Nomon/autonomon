"""Tests for PursuitPlanner — steer-to-bearing, standoff control, debounce."""

from __future__ import annotations

import asyncio

import pytest

from autonomon import ActionPlan, PursuitPlanner, WorldStateUpdate


def _state(
    visible: bool, bearing: float | None = None, distance: float | None = None
) -> WorldStateUpdate:
    return WorldStateUpdate(
        timestamp="t",
        device_id="d",
        state={
            "target_visible": visible,
            "target_bearing_deg": bearing,
            "target_distance_cm": distance,
        },
    )


async def _run_states(planner: PursuitPlanner, states: list[WorldStateUpdate]) -> list[ActionPlan]:
    q_in: asyncio.Queue[WorldStateUpdate] = asyncio.Queue()
    q_out: asyncio.Queue[ActionPlan] = asyncio.Queue()
    task = asyncio.create_task(planner.run(q_in, q_out))
    for s in states:
        await q_in.put(s)
    while not q_in.empty():
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.02)
    await planner.stop()
    await task
    plans = []
    while not q_out.empty():
        plans.append(q_out.get_nowait())
    return plans


def _action(plan: ActionPlan, method: str) -> dict:
    return next(a for a in plan.actions if a["method"] == method)


@pytest.mark.asyncio
async def test_visible_target_far_drives_forward() -> None:
    planner = PursuitPlanner("d", target_distance_cm=80.0, max_speed_pct=60.0, speed_kp=1.0)
    plans = await _run_states(planner, [_state(True, bearing=0.0, distance=200.0)])

    assert len(plans) == 1
    assert plans[0].plan_id.startswith("pursue-")
    assert [a["method"] for a in plans[0].actions] == ["steer", "drive"]
    assert _action(plans[0], "drive")["params"]["speed_pct"] > 0  # approach the target
    assert _action(plans[0], "steer")["params"]["angle_deg"] == pytest.approx(90.0)  # straight


@pytest.mark.asyncio
async def test_bearing_right_steers_past_centre() -> None:
    planner = PursuitPlanner("d", steer_gain=2.0)
    plans = await _run_states(planner, [_state(True, bearing=10.0, distance=80.0)])
    # 90 + 2*10 = 110 deg (steer toward the right-of-centre target).
    assert _action(plans[0], "steer")["params"]["angle_deg"] == pytest.approx(110.0)


@pytest.mark.asyncio
async def test_within_standoff_holds_speed_zero() -> None:
    planner = PursuitPlanner("d", target_distance_cm=80.0, distance_deadband_cm=15.0)
    plans = await _run_states(planner, [_state(True, bearing=0.0, distance=85.0)])
    assert _action(plans[0], "drive")["params"]["speed_pct"] == 0


@pytest.mark.asyncio
async def test_too_close_reverses() -> None:
    planner = PursuitPlanner("d", target_distance_cm=80.0, distance_deadband_cm=15.0, speed_kp=1.0)
    plans = await _run_states(planner, [_state(True, bearing=0.0, distance=40.0)])
    assert _action(plans[0], "drive")["params"]["speed_pct"] < 0  # back off to standoff


@pytest.mark.asyncio
async def test_speed_clamped_to_max() -> None:
    planner = PursuitPlanner("d", target_distance_cm=80.0, max_speed_pct=30.0, speed_kp=1.0)
    plans = await _run_states(planner, [_state(True, bearing=0.0, distance=500.0)])
    assert _action(plans[0], "drive")["params"]["speed_pct"] == 30


@pytest.mark.asyncio
async def test_not_visible_stops() -> None:
    planner = PursuitPlanner("d")
    plans = await _run_states(planner, [_state(False)])
    assert len(plans) == 1
    assert plans[0].plan_id.startswith("lost-")
    assert [a["method"] for a in plans[0].actions] == ["stop"]


@pytest.mark.asyncio
async def test_debounce_only_emits_on_command_change() -> None:
    planner = PursuitPlanner("d", target_distance_cm=80.0)
    plans = await _run_states(
        planner,
        [
            _state(True, 0.0, 200.0),  # pursue forward
            _state(True, 0.0, 200.0),  # identical command → no emit
            _state(False),  # target lost → stop
        ],
    )
    kinds = [p.plan_id.rsplit("-", 1)[0] for p in plans]
    assert kinds == ["pursue", "lost"]
