"""Tests for RulePlanner — table-driven selection, operators, hold, TOML, and
equivalence with the hand-written AvoidancePlanner."""

from __future__ import annotations

import asyncio

import pytest

from autonomon import ActionPlan, AvoidancePlanner, RulePlanner, WorldStateUpdate
from autonomon.planning.base import PlannerBase
from autonomon.planning.rule import bundled_rules_path

# A compact avoid/cruise table equivalent to AvoidancePlanner with no hold.
_AVOID_CRUISE_RULES = [
    {
        "name": "avoid",
        "any_of": [{"obstacle_ahead": True}, {"cliff_detected": True}],
        "actions": [
            {"method": "stop", "params": {}, "priority": 0},
            {"method": "drive", "params": {"speed_pct": -40.0}, "priority": 1},
            {"method": "steer", "params": {"angle_deg": 120.0}, "priority": 2},
        ],
    },
    {
        "name": "cruise",
        "when": {},
        "actions": [
            {"method": "steer", "params": {"angle_deg": 90.0}, "priority": 0},
            {"method": "drive", "params": {"speed_pct": 50.0}, "priority": 1},
        ],
    },
]


def _state(**fields: object) -> WorldStateUpdate:
    return WorldStateUpdate(timestamp="t", device_id="d", state=dict(fields))


async def _run(planner: PlannerBase, states: list[WorldStateUpdate]) -> list[ActionPlan]:
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


def _kinds(plans: list[ActionPlan]) -> list[str]:
    return [p.plan_id.rsplit("-", 1)[0] for p in plans]


@pytest.mark.asyncio
async def test_first_matching_rule_wins() -> None:
    planner = RulePlanner("d", _AVOID_CRUISE_RULES)
    plans = await _run(planner, [_state(obstacle_ahead=True)])
    assert len(plans) == 1
    assert plans[0].plan_id.startswith("avoid-")
    assert [a["method"] for a in plans[0].actions] == ["stop", "drive", "steer"]


@pytest.mark.asyncio
async def test_catch_all_cruise_when_clear() -> None:
    planner = RulePlanner("d", _AVOID_CRUISE_RULES)
    plans = await _run(planner, [_state(obstacle_ahead=False)])
    assert len(plans) == 1
    assert plans[0].plan_id.startswith("cruise-")


@pytest.mark.asyncio
async def test_debounce_only_emits_on_rule_change() -> None:
    planner = RulePlanner("d", _AVOID_CRUISE_RULES)
    plans = await _run(
        planner,
        [
            _state(obstacle_ahead=True),
            _state(obstacle_ahead=True),  # same rule → no emit
            _state(),  # change → cruise
            _state(),  # same → no emit
            _state(cliff_detected=True),  # change → avoid (same name as before)
        ],
    )
    assert _kinds(plans) == ["avoid", "cruise", "avoid"]


@pytest.mark.asyncio
async def test_no_match_emits_default_stop() -> None:
    # A table with a single non-catch-all rule; an unrelated state matches nothing.
    planner = RulePlanner(
        "d",
        rules=[
            {
                "name": "go",
                "when": {"go": True},
                "actions": [{"method": "drive", "params": {"speed_pct": 10}, "priority": 0}],
            }
        ],
    )
    plans = await _run(planner, [_state(go=False)])
    assert len(plans) == 1
    assert plans[0].plan_id.startswith("default-")
    assert [a["method"] for a in plans[0].actions] == ["stop"]


@pytest.mark.asyncio
async def test_custom_default_actions_and_name() -> None:
    planner = RulePlanner(
        "d",
        rules=[{"name": "go", "when": {"go": True}, "actions": []}],
        default_actions=[{"method": "stop", "params": {}, "priority": 0}],
        default_name="idle",
    )
    plans = await _run(planner, [_state(go=False)])
    assert plans[0].plan_id.startswith("idle-")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "matcher, value, should_match",
    [
        ({"lt": 20}, 10, True),
        ({"lt": 20}, 30, False),
        ({"le": 20}, 20, True),
        ({"gt": 20}, 30, True),
        ({"ge": 20}, 20, True),
        ({"ne": 5}, 6, True),
        ({"ne": 5}, 5, False),
        ({"in": [1, 2, 3]}, 2, True),
        ({"in": [1, 2, 3]}, 9, False),
        ({"truthy": True}, 0, False),
        ({"truthy": True}, 7, True),
        ({"exists": True}, None, False),
        ({"exists": False}, None, True),
        ({"lt": 20}, None, False),  # missing/None never matches a numeric bound
    ],
)
async def test_operators(matcher: dict, value: object, should_match: bool) -> None:
    planner = RulePlanner(
        "d",
        rules=[
            {
                "name": "hit",
                "when": {"x": matcher},
                "actions": [{"method": "stop", "params": {}, "priority": 0}],
            }
        ],
        default_name="miss",
    )
    plans = await _run(planner, [_state(x=value)])
    kind = plans[0].plan_id.rsplit("-", 1)[0]
    assert kind == ("hit" if should_match else "miss")


@pytest.mark.asyncio
async def test_when_ands_multiple_clauses() -> None:
    planner = RulePlanner(
        "d",
        rules=[
            {
                "name": "both",
                "when": {"a": True, "b": {"gt": 5}},
                "actions": [{"method": "stop", "params": {}, "priority": 0}],
            }
        ],
        default_name="no",
    )
    assert _kinds(await _run(planner, [_state(a=True, b=10)])) == ["both"]
    # b fails the AND → default.
    assert _kinds(
        await _run(RulePlanner("d", planner._rules, default_name="no"), [_state(a=True, b=1)])
    ) == ["no"]


@pytest.mark.asyncio
async def test_hold_commits_rule_for_duration() -> None:
    planner = RulePlanner(
        "d",
        rules=[
            {
                "name": "avoid",
                "hold_s": 0.3,
                "when": {"obstacle_ahead": True},
                "actions": [{"method": "stop", "params": {}, "priority": 0}],
            },
            {
                "name": "cruise",
                "when": {},
                "actions": [{"method": "drive", "params": {}, "priority": 0}],
            },
        ],
    )
    q_in: asyncio.Queue[WorldStateUpdate] = asyncio.Queue()
    q_out: asyncio.Queue[ActionPlan] = asyncio.Queue()
    task = asyncio.create_task(planner.run(q_in, q_out))

    await q_in.put(_state(obstacle_ahead=True))
    first = await asyncio.wait_for(q_out.get(), timeout=1.0)
    assert first.plan_id.startswith("avoid-")

    await q_in.put(_state(obstacle_ahead=False))  # clears, but inside the hold
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(q_out.get(), timeout=0.1)

    # Hold elapses → idle tick releases to cruise without a new update.
    second = await asyncio.wait_for(q_out.get(), timeout=1.0)
    assert second.plan_id.startswith("cruise-")

    await planner.stop()
    await task


@pytest.mark.asyncio
async def test_validation_rejects_malformed_rules() -> None:
    with pytest.raises(ValueError, match="missing 'name'"):
        RulePlanner("d", rules=[{"actions": []}])
    with pytest.raises(ValueError, match="missing 'actions'"):
        RulePlanner("d", rules=[{"name": "x"}])


@pytest.mark.asyncio
async def test_from_toml_loads_bundled_explore_table() -> None:
    planner = RulePlanner.from_toml(bundled_rules_path("explore.toml"), "d")
    plans = await _run(planner, [_state(obstacle_ahead=True)])
    assert plans[0].plan_id.startswith("avoid-")
    drive = next(a for a in plans[0].actions if a["method"] == "drive")
    assert drive["params"]["speed_pct"] == -60.0


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["avoid", "cruise"])
async def test_explore_toml_equivalent_to_avoidance_planner(first: str) -> None:
    """The bundled explore.toml reproduces AvoidancePlanner(explore defaults) plan-for-plan
    on the first emission (single emit avoids waiting out the 2.5 s hold)."""
    state = _state(obstacle_ahead=True) if first == "avoid" else _state()
    rule = RulePlanner.from_toml(bundled_rules_path("explore.toml"), "d")
    hand = AvoidancePlanner(
        "d",
        forward_speed_pct=60.0,
        reverse_speed_pct=-60.0,
        turn_angle_deg=135.0,
        avoid_duration_s=2.5,
    )
    rule_plans = await _run(rule, [state])
    hand_plans = await _run(hand, [state])
    assert len(rule_plans) == len(hand_plans) == 1
    assert _kinds(rule_plans) == _kinds(hand_plans)
    assert rule_plans[0].actions == hand_plans[0].actions
