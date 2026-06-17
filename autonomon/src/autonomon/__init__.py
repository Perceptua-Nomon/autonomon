"""autonomon — four-layer autonomous capabilities framework for the nomon fleet."""

from autonomon.action.base import ActionBase
from autonomon.fan_in import FanInSlot, MergeStrategy
from autonomon.messages import ActionPlan, ActionResult, PerceptionEvent, WorldStateUpdate
from autonomon.perception.base import PerceptionBase
from autonomon.pipeline import Pipeline
from autonomon.planning.base import PlannerBase
from autonomon.slot import LayerSlot, SlotState
from autonomon.world_model.base import WorldModelBase

__version__ = "0.1.0"

__all__ = [
    # Layer base classes
    "PerceptionBase",
    "WorldModelBase",
    "PlannerBase",
    "ActionBase",
    # Pipeline and slot primitives
    "Pipeline",
    "LayerSlot",
    "SlotState",
    "FanInSlot",
    "MergeStrategy",
    # Message types
    "PerceptionEvent",
    "WorldStateUpdate",
    "ActionPlan",
    "ActionResult",
]
