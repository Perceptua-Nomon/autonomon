"""The ``patrol`` routine: memory-aware area patrolling.

``patrol`` is the concrete consumer that justifies building Phase 3 and Phase 4
together (ADR-006): it is the first routine whose behaviour genuinely needs
**both** the occupancy-grid world model and the rule-table planner.

    (ultrasonic + grayscale) -> OccupancyWorldModel -> RulePlanner -> VehicleAction

* :class:`~autonomon.world_model.occupancy.OccupancyWorldModel` adds short-term
  spatial memory (``recently_blocked``) on top of the boolean obstacle/cliff
  state — something :class:`ObstacleWorldModel` cannot express.
* :class:`~autonomon.planning.rule.RulePlanner` reads a TOML rule table so the
  patrol behaviour is **data, not code**: cliff → back-and-turn (committed),
  obstacle → back-and-turn, *recently* blocked → creep cautiously, otherwise
  cruise. The table is swappable via the ``rules_path`` param — the Phase-4
  value proposition in action.

Like ``explore`` this is a brain-side routine over the existing raw nomothetic
I/O (``GET /api/sensor/*`` in, ``POST /api/drive|steer|hat/motor/stop`` out); it
adds no nomothetic endpoints (ADR-004).
"""

from __future__ import annotations

from typing import Any

import httpx

from autonomon.action.vehicle import VehicleAction
from autonomon.fan_in import FanInSlot
from autonomon.perception.perceptron import Perceptron
from autonomon.pipeline import Pipeline
from autonomon.planning.rule import RulePlanner, bundled_rules_path
from autonomon.world_model.occupancy import OccupancyWorldModel

# Routine-level world-model defaults. patrol senses a wider "caution" range than
# explore (grid_radius_cm) so ``recently_blocked`` reflects nearby clutter, and
# uses a modest obstacle trigger plus a multi-second memory so the robot creeps
# through a recently-blocked area instead of darting forward the instant the
# front sensor clears.
_DEFAULT_OBSTACLE_THRESHOLD_CM = 30.0
_DEFAULT_CLIFF_THRESHOLD = 200.0
_DEFAULT_CELL_SIZE_CM = 10.0
_DEFAULT_GRID_RADIUS_CM = 60.0
_DEFAULT_DECAY_S = 4.0

# Parameter schema for the ``patrol`` routine. Motion (speeds/angles/holds) is
# defined by the rule table (RulePlanner), so the tunable params here shape the
# world model (what the robot perceives and remembers); ``rules_path`` swaps the
# behaviour table itself.
PATROL_PARAMS_SCHEMA: dict[str, dict[str, Any]] = {
    "obstacle_threshold_cm": {
        "type": "number",
        "description": "Distance (cm) at or below which an obstacle is 'ahead'.",
        "default": _DEFAULT_OBSTACLE_THRESHOLD_CM,
    },
    "cliff_threshold": {
        "type": "number",
        "description": (
            "Raw grayscale ADC value at or below which a cliff edge is detected (a "
            "reflective floor reads high ~400-900; a drop-off reads low ~30)."
        ),
        "default": _DEFAULT_CLIFF_THRESHOLD,
    },
    "cell_size_cm": {
        "type": "number",
        "description": "Occupancy-grid resolution: forward range is quantised into cells this deep.",
        "default": _DEFAULT_CELL_SIZE_CM,
    },
    "grid_radius_cm": {
        "type": "number",
        "description": (
            "Max range (cm) recorded in the grid; also the 'recently blocked' caution "
            "range. Readings beyond it are treated as clear."
        ),
        "default": _DEFAULT_GRID_RADIUS_CM,
    },
    "decay_s": {
        "type": "number",
        "description": (
            "Seconds an obstacle is remembered after its last sighting before the cell "
            "ages out (how long the robot stays cautious after passing clutter)."
        ),
        "default": _DEFAULT_DECAY_S,
    },
    "rules_path": {
        "type": "string",
        "description": (
            "Path to a TOML rule table for the planner. Empty (default) uses the bundled "
            "patrol.toml. Point this at your own table to redefine patrol behaviour "
            "without code changes."
        ),
        "default": "",
    },
}


def build_patrol(
    client: httpx.AsyncClient,
    device_id: str,
    params: dict[str, Any],
) -> Pipeline:
    """Build the ``patrol`` (memory-aware area patrol) pipeline.

    Parameters
    ----------
    client : httpx.AsyncClient
        Shared async HTTP client, pre-configured per ADR-002 (base URL, bearer
        token, ``verify=False``). Injected into perception and action.
    device_id : str
        Device identifier stamped on every emitted message.
    params : dict
        Routine parameters (see :data:`PATROL_PARAMS_SCHEMA`). World-model keys
        (``obstacle_threshold_cm``, ``cliff_threshold``, ``cell_size_cm``,
        ``grid_radius_cm``, ``decay_s``) tune perception/memory; ``rules_path``
        selects the planner's TOML rule table (defaults to the bundled
        ``patrol.toml``). Each falls back to the routine default when absent.

    Returns
    -------
    Pipeline
        A fully wired pipeline ready to ``run()``.
    """
    perception = FanInSlot(
        "perception",
        [Perceptron.ultrasonic(client, device_id), Perceptron.grayscale(client, device_id)],
    )

    world_model = OccupancyWorldModel(
        device_id,
        cell_size_cm=params.get("cell_size_cm", _DEFAULT_CELL_SIZE_CM),
        grid_radius_cm=params.get("grid_radius_cm", _DEFAULT_GRID_RADIUS_CM),
        decay_s=params.get("decay_s", _DEFAULT_DECAY_S),
        obstacle_threshold_cm=params.get("obstacle_threshold_cm", _DEFAULT_OBSTACLE_THRESHOLD_CM),
        cliff_threshold=params.get("cliff_threshold", _DEFAULT_CLIFF_THRESHOLD),
    )

    rules_path = params.get("rules_path") or bundled_rules_path("patrol.toml")
    planner = RulePlanner.from_toml(rules_path, device_id)

    return Pipeline(
        perception=perception,
        world_model=world_model,
        planner=planner,
        action=VehicleAction(client, device_id=device_id),
    )
