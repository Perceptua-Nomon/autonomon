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

    Once selected, an **avoid** maneuver is held for ``avoid_duration_s`` before
    the planner re-evaluates: the robot commits to backing up and turning
    instead of darting forward again the instant the front sensor clears (which
    otherwise produces a brief, jittery twitch near obstacles).

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
    avoid_duration_s : float
        Minimum seconds to commit to an avoid maneuver (keep backing up and
        turning) once triggered, even if the obstacle clears meanwhile. ``0.0``
        (the default) re-evaluates on every world update, releasing to cruise as
        soon as the path reads clear. A larger value yields a longer, less
        twitchy back-up-and-turn before forward motion resumes.
    """

    def __init__(
        self,
        device_id: str,
        forward_speed_pct: float = 30.0,
        reverse_speed_pct: float = -30.0,
        turn_angle_deg: float = 135.0,
        avoid_duration_s: float = 0.0,
    ) -> None:
        self._device_id = device_id
        self._forward_speed_pct = forward_speed_pct
        self._reverse_speed_pct = reverse_speed_pct
        self._turn_angle_deg = turn_angle_deg
        self._avoid_duration_s = avoid_duration_s
        self._last_kind: str | None = None
        self._last_state: dict[str, Any] | None = None
        self._avoid_until: float | None = None
        self._plan_counter = 0
        self._stop = asyncio.Event()

    async def run(
        self,
        queue_in: asyncio.Queue[WorldStateUpdate],
        queue_out: asyncio.Queue[ActionPlan],
    ) -> None:
        """Evaluate world state and emit ActionPlans on strategy change.

        Parameters
        ----------
        queue_in : asyncio.Queue[WorldStateUpdate]
            Source of ``WorldStateUpdate`` instances.
        queue_out : asyncio.Queue[ActionPlan]
            Receives ``ActionPlan`` instances, emitted only when the selected
            strategy changes.

        An active avoid maneuver is also re-checked while the queue is idle, so
        the planner can release back to cruise when ``avoid_duration_s`` elapses
        without waiting for a new world update.
        """
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            try:
                update = await asyncio.wait_for(queue_in.get(), timeout=_QUEUE_GET_TIMEOUT_S)
            except asyncio.TimeoutError:
                update = None
            if update is not None:
                self._last_state = update.state
            await self._tick(queue_out, loop.time())

    async def _tick(self, queue_out: asyncio.Queue[ActionPlan], now: float) -> None:
        """Select a strategy for the latest state and emit it on change.

        While an avoid maneuver is still within its committed ``avoid_duration_s``
        window, re-evaluation is suppressed so the robot keeps backing up and
        turning even if the obstacle has already cleared.

        Parameters
        ----------
        queue_out : asyncio.Queue[ActionPlan]
            Receives ``ActionPlan`` instances on strategy change.
        now : float
            Current event-loop monotonic time (``loop.time()``).
        """
        state = self._last_state
        if state is None:
            return
        if self._avoid_until is not None and now < self._avoid_until:
            return
        kind, actions = self._select(state)
        self._avoid_until = now + self._avoid_duration_s if kind == _KIND_AVOID else None
        if kind != self._last_kind:
            self._last_kind = kind
            await queue_out.put(self._build_plan(kind, actions))

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
