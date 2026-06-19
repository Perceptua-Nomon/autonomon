"""Tests for message serialisation round-trips."""

from autonomon.messages import ActionPlan, ActionResult, PerceptionEvent, WorldStateUpdate


def test_perception_event_round_trip() -> None:
    msg = PerceptionEvent(
        timestamp="2026-01-01T00:00:00Z",
        device_id="nomon-ab12",
        sensor_type="ultrasonic",
        data={"distance_cm": 18.4},
    )
    d = msg.to_dict()
    assert d["type"] == "perception_event"
    assert d["data"]["distance_cm"] == 18.4
    restored = PerceptionEvent.from_dict(d)
    assert restored.sensor_type == "ultrasonic"
    assert restored.data["distance_cm"] == 18.4


def test_world_state_update_round_trip() -> None:
    msg = WorldStateUpdate(
        timestamp="t",
        device_id="nomon-ab12",
        state={"obstacle_ahead": True},
        delta={"obstacle_ahead": True},
    )
    d = msg.to_dict()
    assert d["type"] == "world_state_update"
    restored = WorldStateUpdate.from_dict(d)
    assert restored.state["obstacle_ahead"] is True
    assert restored.delta["obstacle_ahead"] is True


def test_action_plan_round_trip() -> None:
    msg = ActionPlan(
        timestamp="t",
        device_id="nomon-ab12",
        plan_id="avoid-001",
        actions=[{"method": "stop", "params": {}, "priority": 0}],
    )
    d = msg.to_dict()
    assert d["type"] == "action_plan"
    restored = ActionPlan.from_dict(d)
    assert restored.plan_id == "avoid-001"
    assert restored.actions[0]["method"] == "stop"


def test_action_result_defaults() -> None:
    msg = ActionResult(
        timestamp="t",
        device_id="nomon-ab12",
        plan_id="avoid-001",
        action={"method": "stop", "params": {}},
        success=True,
    )
    d = msg.to_dict()
    assert d["error"] is None
    assert d["success"] is True
