"""Message types for inter-layer communication.

The four layers pass these dataclass **instances** directly through their typed
``asyncio.Queue`` channels (e.g. ``asyncio.Queue[PerceptionEvent]``) — the
in-process pipeline keeps full typing and skips a per-hop serialisation round-trip.

``to_dict()`` / ``from_dict()`` remain for the **serialisation boundaries**:
forwarding lifecycle/telemetry to nomothetic, NDJSON logging, and test fixtures.
They are JSON-serialisable so any message can still be logged or sent verbatim.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class PerceptionEvent:
    """Emitted by the Perception layer for each normalised sensor reading.

    Parameters
    ----------
    timestamp : str
        UTC ISO 8601 timestamp of the sensor read.
    device_id : str
        Device identifier (e.g. "nomon-ab12").
    sensor_type : str
        Sensor category: "ultrasonic", "grayscale", "battery", "encoder", etc.
    data : dict
        Sensor-specific payload (normalised values, not raw register reads).
    """

    timestamp: str
    device_id: str
    sensor_type: str
    data: dict[str, Any] = field(default_factory=dict)
    type: str = field(default="perception_event", init=False)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PerceptionEvent:
        d = dict(d)
        d.pop("type", None)
        return cls(**d)


@dataclass
class WorldStateUpdate:
    """Emitted by the World Model layer when tracked state changes.

    Parameters
    ----------
    timestamp : str
        UTC ISO 8601 timestamp of the update.
    device_id : str
        Device identifier.
    state : dict
        Full current world state snapshot.
    delta : dict
        Only the fields that changed in this update (empty if first emission).
    """

    timestamp: str
    device_id: str
    state: dict[str, Any] = field(default_factory=dict)
    delta: dict[str, Any] = field(default_factory=dict)
    type: str = field(default="world_state_update", init=False)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> WorldStateUpdate:
        d = dict(d)
        d.pop("type", None)
        return cls(**d)


@dataclass
class ActionPlan:
    """Emitted by the Planning layer when a new action sequence is selected.

    Parameters
    ----------
    timestamp : str
        UTC ISO 8601 timestamp.
    device_id : str
        Device identifier.
    plan_id : str
        Unique identifier for this plan (for correlating with ActionResult).
    actions : list
        Ordered list of action dicts: {"method": str, "params": dict, "priority": int}.
        Actions with lower priority values execute first.
    """

    timestamp: str
    device_id: str
    plan_id: str
    actions: list[dict[str, Any]] = field(default_factory=list)
    type: str = field(default="action_plan", init=False)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ActionPlan:
        d = dict(d)
        d.pop("type", None)
        return cls(**d)


@dataclass
class ActionResult:
    """Emitted by the Action layer after attempting each action.

    Parameters
    ----------
    timestamp : str
        UTC ISO 8601 timestamp of the execution attempt.
    device_id : str
        Device identifier.
    plan_id : str
        Plan this action belongs to.
    action : dict
        The action that was executed ({"method", "params", "priority"}).
    success : bool
        True if the nomothetic call returned 2xx.
    data : dict
        Response body from nomothetic (may be empty on error).
    error : str or None
        Error message if success is False.
    """

    timestamp: str
    device_id: str
    plan_id: str
    action: dict[str, Any] = field(default_factory=dict)
    success: bool = True
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    type: str = field(default="action_result", init=False)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ActionResult:
        d = dict(d)
        d.pop("type", None)
        return cls(**d)
