"""The ``explore`` routine: obstacle-avoidance wandering.

This is the production form of the integration test's ``_build_pipeline()``
helper. It wires the existing Phase 2–5 layers into a :class:`Pipeline`:
``Perceptron.ultrasonic`` (optionally with ``Perceptron.grayscale`` via a
``FanInSlot`` for cliff detection) -> ``ObstacleWorldModel`` ->
``AvoidancePlanner`` -> ``VehicleAction``.

It is pure wiring — no new layer implementations — per ADR-003.
"""

from __future__ import annotations

from typing import Any

import httpx

from autonomon.action.vehicle import VehicleAction
from autonomon.fan_in import FanInSlot, MergeStrategy
from autonomon.perception.perceptron import Perceptron
from autonomon.pipeline import Pipeline
from autonomon.planning.avoidance import AvoidancePlanner
from autonomon.world_model.obstacle import ObstacleWorldModel

# Parameter schema for the ``explore`` routine. Declared here so the plugin
# manifest can advertise it (see ``autonomon.routines.__init__``); applying the
# params onto layer constructor args is this factory's job (ADR-003 D3).
EXPLORE_PARAMS_SCHEMA: dict[str, dict[str, Any]] = {
    "obstacle_threshold_cm": {
        "type": "number",
        "description": "Distance (cm) at or below which an obstacle is detected.",
        "default": 20.0,
    },
    "forward_speed_pct": {
        "type": "number",
        "description": "Cruise drive speed (0–100) when the path is clear.",
        "default": 30.0,
    },
    "turn_angle_deg": {
        "type": "number",
        "description": "Steering angle (0–180, 90 = straight) used to turn away when avoiding.",
        "default": 135.0,
    },
    "reverse_speed_pct": {
        "type": "number",
        "description": "Drive speed (negative, -100–0) used when backing away from an obstacle.",
        "default": -30.0,
    },
    "cliff_threshold": {
        "type": "number",
        "description": (
            "Normalised grayscale value (0.0–1.0) at or below which a cliff edge is "
            "detected. Only used when 'cliff_detection' is enabled."
        ),
        "default": 0.2,
    },
    "cliff_detection": {
        "type": "boolean",
        "description": (
            "Enable grayscale cliff detection. Adds a Perceptron.grayscale source via a "
            "FanInSlot alongside the ultrasonic sensor."
        ),
        "default": False,
    },
}


def build_explore(
    client: httpx.AsyncClient,
    device_id: str,
    params: dict[str, Any],
) -> Pipeline:
    """Build the ``explore`` (obstacle-avoidance wandering) pipeline.

    Parameters
    ----------
    client : httpx.AsyncClient
        Shared async HTTP client, pre-configured per ADR-002 (base URL, bearer
        token, ``verify=False``). Injected into the perception and action layers.
    device_id : str
        Device identifier stamped on every emitted message.
    params : dict
        Routine parameters (see :data:`EXPLORE_PARAMS_SCHEMA`). Recognised keys,
        each falling back to the underlying layer default when absent:

        ``obstacle_threshold_cm`` : float
            Forwarded to :class:`ObstacleWorldModel`.
        ``forward_speed_pct`` : float
            Forwarded to :class:`AvoidancePlanner`.
        ``turn_angle_deg`` : float
            Forwarded to :class:`AvoidancePlanner`.
        ``cliff_threshold`` : float
            Forwarded to :class:`ObstacleWorldModel` (only meaningful when cliff
            detection is enabled).
        ``cliff_detection`` : bool
            When truthy, a ``Perceptron.grayscale`` source is added beside the
            ultrasonic source via a ``FanInSlot`` (PASS_THROUGH), enabling the
            world model's cliff fusion.

    Returns
    -------
    Pipeline
        A fully wired pipeline ready to ``run()``.
    """
    ultrasonic = Perceptron.ultrasonic(client, device_id)
    perception: Perceptron | FanInSlot
    if params.get("cliff_detection"):
        perception = FanInSlot(
            "perception",
            [ultrasonic, Perceptron.grayscale(client, device_id)],
            MergeStrategy.PASS_THROUGH,
        )
    else:
        perception = ultrasonic

    world_model_kwargs: dict[str, Any] = {"device_id": device_id}
    if "obstacle_threshold_cm" in params:
        world_model_kwargs["obstacle_threshold_cm"] = params["obstacle_threshold_cm"]
    if "cliff_threshold" in params:
        world_model_kwargs["cliff_threshold"] = params["cliff_threshold"]

    planner_kwargs: dict[str, Any] = {"device_id": device_id}
    if "forward_speed_pct" in params:
        planner_kwargs["forward_speed_pct"] = params["forward_speed_pct"]
    if "reverse_speed_pct" in params:
        planner_kwargs["reverse_speed_pct"] = params["reverse_speed_pct"]
    if "turn_angle_deg" in params:
        planner_kwargs["turn_angle_deg"] = params["turn_angle_deg"]

    return Pipeline(
        perception=perception,
        world_model=ObstacleWorldModel(**world_model_kwargs),
        planner=AvoidancePlanner(**planner_kwargs),
        action=VehicleAction(client, device_id=device_id),
    )
