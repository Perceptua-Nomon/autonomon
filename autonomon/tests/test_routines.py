"""Tests for the autonomy routine registry and the built-in ``explore`` routine.

No Pi, no network: the device ``httpx.AsyncClient`` is a mock and the pipeline is
never run here (the end-to-end run is covered by test_pipeline_integration).
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest

from autonomon import (
    AvoidancePlanner,
    FanInSlot,
    ObstacleWorldModel,
    Perceptron,
    Pipeline,
    UnknownRoutineError,
    VehicleAction,
    available_routines,
    get_routine,
)
from autonomon.routines import nomon_manifest


def _client() -> AsyncMock:
    return AsyncMock(spec=httpx.AsyncClient)


# ---------------------------------------------------------------------------
# Registry lookup
# ---------------------------------------------------------------------------


def test_available_routines_lists_explore() -> None:
    assert "explore" in available_routines()


def test_get_routine_known_returns_callable() -> None:
    factory = get_routine("explore")
    assert callable(factory)


def test_get_routine_unknown_raises_with_available_names() -> None:
    with pytest.raises(UnknownRoutineError) as exc_info:
        get_routine("does-not-exist")
    message = str(exc_info.value)
    assert "does-not-exist" in message
    assert "explore" in message  # the available names are listed


def test_unknown_routine_error_is_keyerror() -> None:
    # Subclasses KeyError so callers may catch either.
    with pytest.raises(KeyError):
        get_routine("nope")


# ---------------------------------------------------------------------------
# explore factory wiring
# ---------------------------------------------------------------------------


def test_explore_returns_wired_pipeline_with_four_slots() -> None:
    pipeline = get_routine("explore")(_client(), "nomon-1", {})
    assert isinstance(pipeline, Pipeline)

    slots = pipeline._slots
    assert set(slots) == {"perception", "world_model", "planner", "action"}
    assert isinstance(slots["perception"]._impl, Perceptron)  # type: ignore[union-attr]
    assert isinstance(slots["world_model"]._impl, ObstacleWorldModel)  # type: ignore[union-attr]
    assert isinstance(slots["planner"]._impl, AvoidancePlanner)  # type: ignore[union-attr]
    assert isinstance(slots["action"]._impl, VehicleAction)  # type: ignore[union-attr]


def test_explore_cliff_detection_adds_grayscale_fanin() -> None:
    pipeline = get_routine("explore")(_client(), "nomon-1", {"cliff_detection": True})
    perception = pipeline._slots["perception"]
    assert isinstance(perception, FanInSlot)
    # Two perception sources: ultrasonic + grayscale.
    assert len(perception._impls) == 2
    sensor_types = {impl._sensor_type for impl in perception._impls}  # type: ignore[union-attr]
    assert sensor_types == {"ultrasonic", "grayscale"}


def test_explore_params_map_to_layer_args() -> None:
    params: dict[str, Any] = {
        "obstacle_threshold_cm": 12.5,
        "cliff_threshold": 0.4,
        "forward_speed_pct": 55.0,
        "turn_angle_deg": 120.0,
    }
    pipeline = get_routine("explore")(_client(), "nomon-1", params)

    world_model = cast(ObstacleWorldModel, pipeline._slots["world_model"]._impl)  # type: ignore[union-attr]
    planner = cast(AvoidancePlanner, pipeline._slots["planner"]._impl)  # type: ignore[union-attr]
    assert world_model._obstacle_threshold_cm == 12.5
    assert world_model._cliff_threshold == 0.4
    assert planner._forward_speed_pct == 55.0
    assert planner._turn_angle_deg == 120.0


def test_explore_uses_layer_defaults_when_params_absent() -> None:
    pipeline = get_routine("explore")(_client(), "nomon-1", {})
    world_model = cast(ObstacleWorldModel, pipeline._slots["world_model"]._impl)  # type: ignore[union-attr]
    planner = cast(AvoidancePlanner, pipeline._slots["planner"]._impl)  # type: ignore[union-attr]
    # Defaults from the layer constructors.
    assert world_model._obstacle_threshold_cm == 20.0
    assert planner._forward_speed_pct == 30.0
    assert planner._turn_angle_deg == 135.0


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def test_manifest_advertises_routines_and_params() -> None:
    assert nomon_manifest["name"] == "autonomon"
    assert "explore" in nomon_manifest["routines"]  # type: ignore[operator]
    params_schema = nomon_manifest["params_schema"]
    assert "obstacle_threshold_cm" in params_schema  # type: ignore[operator]
    assert "cliff_detection" in params_schema  # type: ignore[operator]
