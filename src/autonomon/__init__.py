"""autonomon — four-layer autonomous capabilities framework for the nomon fleet."""

from autonomon.action.base import ActionBase
from autonomon.action.vehicle import VehicleAction
from autonomon.fan_in import FanInSlot
from autonomon.messages import ActionPlan, ActionResult, PerceptionEvent, WorldStateUpdate
from autonomon.perception.base import PerceptionBase
from autonomon.perception.detector import (
    Detection,
    Detector,
    FakeDetector,
    OpenCvDnnDetector,
    OpenCvHogDetector,
    YoloOnnxDetector,
)
from autonomon.perception.perceptron import Perceptron
from autonomon.perception.vision import VisionPerception
from autonomon.pipeline import Pipeline
from autonomon.planning.avoidance import AvoidancePlanner
from autonomon.planning.base import PlannerBase
from autonomon.planning.follow import FollowPlanner
from autonomon.planning.pursuit import PursuitPlanner
from autonomon.planning.rule import RulePlanner
from autonomon.routines import (
    ROUTINES,
    RoutineFactory,
    UnknownRoutineError,
    available_routines,
    build_explore,
    build_follow_user,
    build_patrol,
    get_routine,
)
from autonomon.slot import LayerSlot, SlotState
from autonomon.world_model.base import WorldModelBase
from autonomon.world_model.obstacle import ObstacleWorldModel
from autonomon.world_model.occupancy import OccupancyWorldModel
from autonomon.world_model.target import TargetWorldModel

__version__ = "0.5.0"

__all__ = [
    # Layer base classes
    "PerceptionBase",
    "WorldModelBase",
    "PlannerBase",
    "ActionBase",
    # Layer implementations
    "Perceptron",
    "VisionPerception",
    "ObstacleWorldModel",
    "OccupancyWorldModel",
    "TargetWorldModel",
    "AvoidancePlanner",
    "RulePlanner",
    "PursuitPlanner",
    "FollowPlanner",
    "VehicleAction",
    # Vision detectors
    "Detection",
    "Detector",
    "FakeDetector",
    "OpenCvDnnDetector",
    "OpenCvHogDetector",
    "YoloOnnxDetector",
    # Pipeline and slot primitives
    "Pipeline",
    "LayerSlot",
    "SlotState",
    "FanInSlot",
    # Routine registry
    "ROUTINES",
    "RoutineFactory",
    "UnknownRoutineError",
    "available_routines",
    "get_routine",
    "build_explore",
    "build_follow_user",
    "build_patrol",
    # Message types
    "PerceptionEvent",
    "WorldStateUpdate",
    "ActionPlan",
    "ActionResult",
]
