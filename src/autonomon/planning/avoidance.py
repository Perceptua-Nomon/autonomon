"""AvoidancePlanner: minimal rule-based obstacle-avoidance planner."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from autonomon.messages import ActionPlan, WorldStateUpdate
from autonomon.planning.base import PlannerBase

logger = logging.getLogger(__name__)

_QUEUE_GET_TIMEOUT_S = 0.05

# Plan "kinds" — the planner debounces on these, not on the generated plan_id.
_KIND_AVOID = "avoid"
_KIND_CRUISE = "cruise"


class AvoidancePlanner(PlannerBase):
    """Selects a drive strategy from the obstacle/cliff world state.

    Minimal rule set (priority-ordered, first match wins):

    1. ``obstacle_ahead`` or ``cliff_detected`` → **avoid**: stop, reverse,
       then steer away.
    2. otherwise → **cruise**: steer straight and drive forward.

    Pure logic with no I/O, so it is fully testable by pushing
    ``WorldStateUpdate`` dicts into a queue. A new ``ActionPlan`` is emitted
    only when the selected strategy changes (debounce), so the action layer is
    not re-commanded every world-state tick.

    Parameters
    ----------
    device_id : str
        Device identifier included in every ``ActionPlan``.
    forward_speed_pct : float
        Drive speed (0–100) used when cruising.
    reverse_speed_pct : float
        Drive speed (negative, -100–0) used when backing away from an obstacle.
    turn_angle_deg : float
        Steering angle (0–180, 90 = straight) used to turn away when avoiding.
    """

    def __init__(
        self,
        device_id: str,
        forward_speed_pct: float = 30.0,
        reverse_speed_pct: float = -30.0,
        turn_angle_deg: float = 135.0,
    ) -> None:
        self._device_id = device_id
        self._forward_speed_pct = forward_speed_pct
        self._reverse_speed_pct = reverse_speed_pct
        self._turn_angle_deg = turn_angle_deg
        self._last_kind: str | None = None
        self._plan_counter = 0
        self._stop = asyncio.Event()

    async def run(self, queue_in: asyncio.Queue, queue_out: asyncio.Queue) -> None:  # type: ignore[type-arg]
        """Evaluate world state and emit ActionPlans on strategy change.

        Parameters
        ----------
        queue_in : asyncio.Queue
            Source of ``WorldStateUpdate.to_dict()`` items.
        queue_out : asyncio.Queue
            Receives ``ActionPlan.to_dict()`` items, emitted only when the
            selected strategy changes.
        """
        while not self._stop.is_set():
            try:
                msg = await asyncio.wait_for(queue_in.get(), timeout=_QUEUE_GET_TIMEOUT_S)
            except asyncio.TimeoutError:
                continue
            update = WorldStateUpdate.from_dict(msg)
            kind, actions = self._select(update.state)
            if kind != self._last_kind:
                self._last_kind = kind
                await queue_out.put(self._build_plan(kind, actions).to_dict())

    def _select(self, state: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        """Return the (kind, actions) for the current world state."""
        if state.get("obstacle_ahead") or state.get("cliff_detected"):
            return _KIND_AVOID, [
                {"method": "stop", "params": {}, "priority": 0},
                {
                    "method": "drive",
                    "params": {"speed_pct": self._reverse_speed_pct},
                    "priority": 1,
                },
                {"method": "steer", "params": {"angle_deg": self._turn_angle_deg}, "priority": 2},
            ]
        return _KIND_CRUISE, [
            {"method": "steer", "params": {"angle_deg": 90.0}, "priority": 0},
            {"method": "drive", "params": {"speed_pct": self._forward_speed_pct}, "priority": 1},
        ]

    def _build_plan(self, kind: str, actions: list[dict[str, Any]]) -> ActionPlan:
        self._plan_counter += 1
        return ActionPlan(
            timestamp=datetime.now(timezone.utc).isoformat(),
            device_id=self._device_id,
            plan_id=f"{kind}-{self._plan_counter}",
            actions=actions,
        )

    async def stop(self) -> None:
        self._stop.set()
