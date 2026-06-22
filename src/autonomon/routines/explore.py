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

# Routine-level defaults for the behaviours an operator most often tunes. These
# are the *effective* defaults applied by build_explore() when the param is
# absent; they intentionally override the underlying layer constructor defaults
# to give a wider trigger margin, faster cruising, and a committed (non-twitchy)
# avoid maneuver suited to open-floor exploration.
_DEFAULT_OBSTACLE_THRESHOLD_CM = 40.0
_DEFAULT_FORWARD_SPEED_PCT = 60.0
_DEFAULT_REVERSE_SPEED_PCT = -60.0
_DEFAULT_AVOID_DURATION_S = 2.5

# Parameter schema for the ``explore`` routine. Declared here so the plugin
# manifest can advertise it (see ``autonomon.routines.__init__``); applying the
# params onto layer constructor args is this factory's job (ADR-003 D3).
EXPLORE_PARAMS_SCHEMA: dict[str, dict[str, Any]] = {
    "obstacle_threshold_cm": {
        "type": "number",
        "description": "Distance (cm) at or below which an obstacle is detected.",
        "default": _DEFAULT_OBSTACLE_THRESHOLD_CM,
    },
    "forward_speed_pct": {
        "type": "number",
        "description": "Cruise drive speed (0–100) when the path is clear.",
        "default": _DEFAULT_FORWARD_SPEED_PCT,
    },
    "turn_angle_deg": {
        "type": "number",
        "description": "Steering angle (0–180, 90 = straight) used to turn away when avoiding.",
        "default": 135.0,
    },
    "avoid_duration_s": {
        "type": "number",
        "description": (
            "Seconds to commit to a back-up-and-turn maneuver once an obstacle or "
            "cliff is detected, before re-checking the path. Larger values give a "
            "longer, less twitchy avoidance."
        ),
        "default": _DEFAULT_AVOID_DURATION_S,
    },
    "reverse_speed_pct": {
        "type": "number",
        "description": "Drive speed (negative, -100–0) used when backing away from an obstacle.",
        "default": _DEFAULT_REVERSE_SPEED_PCT,
    },
    "cliff_threshold": {
        "type": "number",
        "description": (
            "Normalised grayscale value (0.0–1.0) at or above which a cliff edge is "
            "detected (0.0 = reflective surface present, 1.0 = no surface / edge). "
            "Only used when 'cliff_detection' is enabled."
        ),
        "default": 0.7,
    },
    "cliff_detection": {
        "type": "boolean",
        "description": (
            "Enable grayscale cliff detection. Adds a Perceptron.grayscale source via a "
            "FanInSlot alongside the ultrasonic sensor so the robot backs away from edges "
            "(and when lifted off the floor). Enabled by default; set false to drive on "
            "the ultrasonic sensor alone."
        ),
        "default": True,
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
            Forwarded to :class:`ObstacleWorldModel`. Defaults to
            ``40.0`` cm for this routine when absent.
        ``forward_speed_pct`` : float
            Forwarded to :class:`AvoidancePlanner`. Cruise speed; defaults to
            ``60.0`` for this routine when absent.
        ``reverse_speed_pct`` : float
            Forwarded to :class:`AvoidancePlanner`. Speed used when backing away;
            defaults to ``-60.0`` for this routine when absent.
        ``turn_angle_deg`` : float
            Forwarded to :class:`AvoidancePlanner`.
        ``avoid_duration_s`` : float
            Forwarded to :class:`AvoidancePlanner`. Seconds the back-up-and-turn
            maneuver is held before re-checking the path. Defaults to ``2.5`` s
            for this routine when absent.
        ``cliff_threshold`` : float
            Forwarded to :class:`ObstacleWorldModel` (only meaningful when cliff
            detection is enabled).
        ``cliff_detection`` : bool
            When truthy (**the default**), a ``Perceptron.grayscale`` source is
            added beside the ultrasonic source via a ``FanInSlot`` (PASS_THROUGH),
            enabling the world model's cliff fusion. Set false to drive on the
            ultrasonic sensor alone.

    Returns
    -------
    Pipeline
        A fully wired pipeline ready to ``run()``.
    """
    ultrasonic = Perceptron.ultrasonic(client, device_id)
    perception: Perceptron | FanInSlot
    # Cliff detection is on by default: a wandering robot must back away from
    # edges (and stop when lifted off the floor). Pass cliff_detection=False to
    # run on the ultrasonic sensor alone.
    if params.get("cliff_detection", True):
        perception = FanInSlot(
            "perception",
            [ultrasonic, Perceptron.grayscale(client, device_id)],
            MergeStrategy.PASS_THROUGH,
        )
    else:
        perception = ultrasonic

    world_model_kwargs: dict[str, Any] = {"device_id": device_id}
    # Applied unconditionally so the routine's wider default trigger margin takes
    # effect when the param is absent (it overrides the layer constructor default).
    world_model_kwargs["obstacle_threshold_cm"] = params.get(
        "obstacle_threshold_cm", _DEFAULT_OBSTACLE_THRESHOLD_CM
    )
    if "cliff_threshold" in params:
        world_model_kwargs["cliff_threshold"] = params["cliff_threshold"]

    planner_kwargs: dict[str, Any] = {"device_id": device_id}
    # Applied unconditionally so the routine's faster default drive speeds take
    # effect when the params are absent (they override the layer defaults).
    planner_kwargs["forward_speed_pct"] = params.get(
        "forward_speed_pct", _DEFAULT_FORWARD_SPEED_PCT
    )
    planner_kwargs["reverse_speed_pct"] = params.get(
        "reverse_speed_pct", _DEFAULT_REVERSE_SPEED_PCT
    )
    if "turn_angle_deg" in params:
        planner_kwargs["turn_angle_deg"] = params["turn_angle_deg"]
    # Applied unconditionally so the routine commits to a longer avoid maneuver
    # by default (the planner's own default is 0.0 = re-evaluate immediately).
    planner_kwargs["avoid_duration_s"] = params.get("avoid_duration_s", _DEFAULT_AVOID_DURATION_S)

    return Pipeline(
        perception=perception,
        world_model=ObstacleWorldModel(**world_model_kwargs),
        planner=AvoidancePlanner(**planner_kwargs),
        action=VehicleAction(client, device_id=device_id),
    )
