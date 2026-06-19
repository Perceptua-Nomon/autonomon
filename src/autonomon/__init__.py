"""autonomon — four-layer autonomous capabilities framework for the nomon fleet."""

from autonomon.action.base import ActionBase
from autonomon.action.vehicle import VehicleAction
from autonomon.fan_in import FanInSlot, MergeStrategy
from autonomon.messages import ActionPlan, ActionResult, PerceptionEvent, WorldStateUpdate
from autonomon.perception.base import PerceptionBase
from autonomon.perception.perceptron import Perceptron
from autonomon.pipeline import Pipeline
from autonomon.planning.avoidance import AvoidancePlanner
from autonomon.planning.base import PlannerBase
from autonomon.routines import (
    ROUTINES,
    RoutineFactory,
    UnknownRoutineError,
    available_routines,
    build_explore,
    get_routine,
)
from autonomon.slot import LayerSlot, SlotState
from autonomon.world_model.base import WorldModelBase
from autonomon.world_model.obstacle import ObstacleWorldModel

__version__ = "0.1.0"

__all__ = [
    # Layer base classes
    "PerceptionBase",
    "WorldModelBase",
    "PlannerBase",
    "ActionBase",
    # Layer implementations
    "Perceptron",
    "ObstacleWorldModel",
    "AvoidancePlanner",
    "VehicleAction",
    # Pipeline and slot primitives
    "Pipeline",
    "LayerSlot",
    "SlotState",
    "FanInSlot",
    "MergeStrategy",
    # Routine registry
    "ROUTINES",
    "RoutineFactory",
    "UnknownRoutineError",
    "available_routines",
    "get_routine",
    "build_explore",
    # Message types
    "PerceptionEvent",
    "WorldStateUpdate",
    "ActionPlan",
    "ActionResult",
]
