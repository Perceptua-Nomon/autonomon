"""Tests for AvoidancePlanner — rule selection and debounce."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from autonomon import AvoidancePlanner, WorldStateUpdate


def _state(obstacle_ahead: bool = False, cliff_detected: bool = False) -> dict[str, Any]:
    return WorldStateUpdate(
        timestamp="t",
        device_id="d",
        state={"obstacle_ahead": obstacle_ahead, "cliff_detected": cliff_detected},
    ).to_dict()


async def _run_states(
    planner: AvoidancePlanner, states: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    q_in: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    q_out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
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


def _methods(plan: dict[str, Any]) -> list[str]:
    return [a["method"] for a in plan["actions"]]


@pytest.mark.asyncio
async def test_obstacle_produces_avoid_plan() -> None:
    planner = AvoidancePlanner(device_id="nomon-test")
    plans = await _run_states(planner, [_state(obstacle_ahead=True)])

    assert len(plans) == 1
    assert plans[0]["type"] == "action_plan"
    assert plans[0]["plan_id"].startswith("avoid-")
    assert _methods(plans[0]) == ["stop", "drive", "steer"]
    # Reverse + turn.
    drive = plans[0]["actions"][1]
    assert drive["params"]["speed_pct"] < 0


@pytest.mark.asyncio
async def test_cliff_also_produces_avoid_plan() -> None:
    planner = AvoidancePlanner(device_id="nomon-test")
    plans = await _run_states(planner, [_state(cliff_detected=True)])

    assert len(plans) == 1
    assert plans[0]["plan_id"].startswith("avoid-")


@pytest.mark.asyncio
async def test_clear_state_produces_cruise_plan() -> None:
    planner = AvoidancePlanner(device_id="nomon-test")
    # Start avoiding, then clear → cruise (a change, so it emits).
    plans = await _run_states(planner, [_state(obstacle_ahead=True), _state()])

    assert len(plans) == 2
    assert plans[0]["plan_id"].startswith("avoid-")
    assert plans[1]["plan_id"].startswith("cruise-")
    assert _methods(plans[1]) == ["steer", "drive"]
    assert plans[1]["actions"][1]["params"]["speed_pct"] > 0


@pytest.mark.asyncio
async def test_debounce_only_emits_on_strategy_change() -> None:
    planner = AvoidancePlanner(device_id="nomon-test")
    plans = await _run_states(
        planner,
        [
            _state(obstacle_ahead=True),
            _state(obstacle_ahead=True),  # same strategy → no emit
            _state(),  # change → emit cruise
            _state(),  # same → no emit
            _state(obstacle_ahead=True),  # change → emit avoid
        ],
    )

    kinds = [p["plan_id"].rsplit("-", 1)[0] for p in plans]
    assert kinds == ["avoid", "cruise", "avoid"]


@pytest.mark.asyncio
async def test_first_clear_state_still_emits_initial_cruise() -> None:
    """The very first update (clear) has no prior strategy, so it emits once."""
    planner = AvoidancePlanner(device_id="nomon-test")
    plans = await _run_states(planner, [_state()])

    assert len(plans) == 1
    assert plans[0]["plan_id"].startswith("cruise-")


@pytest.mark.asyncio
async def test_avoid_held_for_duration_suppresses_early_clear() -> None:
    """An avoid maneuver commits for ``avoid_duration_s`` even if the path clears early."""
    planner = AvoidancePlanner(device_id="nomon-test", avoid_duration_s=0.3)
    q_in: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    q_out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    task = asyncio.create_task(planner.run(q_in, q_out))

    await q_in.put(_state(obstacle_ahead=True))
    first = await asyncio.wait_for(q_out.get(), timeout=1.0)
    assert first["plan_id"].startswith("avoid-")

    # Obstacle clears almost immediately — still inside the 0.3 s hold window.
    await q_in.put(_state())
    # No cruise plan must appear before the hold elapses.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(q_out.get(), timeout=0.1)

    # Once the hold elapses, the planner releases to cruise on its own (the idle
    # tick re-evaluates the retained state — no new world update is required).
    second = await asyncio.wait_for(q_out.get(), timeout=1.0)
    assert second["plan_id"].startswith("cruise-")

    await planner.stop()
    await task


@pytest.mark.asyncio
async def test_zero_duration_releases_immediately_on_clear() -> None:
    """The default (0.0) hold preserves the original immediate-release behavior."""
    planner = AvoidancePlanner(device_id="nomon-test")  # avoid_duration_s defaults to 0.0
    plans = await _run_states(planner, [_state(obstacle_ahead=True), _state()])
    kinds = [p["plan_id"].rsplit("-", 1)[0] for p in plans]
    assert kinds == ["avoid", "cruise"]


@pytest.mark.asyncio
async def test_custom_speeds_and_angle() -> None:
    planner = AvoidancePlanner(
        device_id="nomon-test",
        forward_speed_pct=50.0,
        reverse_speed_pct=-40.0,
        turn_angle_deg=120.0,
    )
    plans = await _run_states(planner, [_state(obstacle_ahead=True)])
    actions = {a["method"]: a["params"] for a in plans[0]["actions"]}
    assert actions["drive"]["speed_pct"] == -40.0
    assert actions["steer"]["angle_deg"] == 120.0
