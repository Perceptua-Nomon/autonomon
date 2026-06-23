"""PursuitPlanner: drive toward a tracked target, holding a standoff distance.

Consumes the ``TargetWorldModel`` state and emits drive/steer plans that turn
toward the target's bearing and close to a ``target_distance_cm`` standoff. When
the target is not visible it commands a stop. Pure logic, no I/O (fully testable
by pushing ``WorldStateUpdate``s into a queue), and debounced on the *quantised*
command so small tracking jitter does not re-emit identical plans.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from autonomon.messages import ActionPlan, WorldStateUpdate
from autonomon.planning.base import PlannerBase

logger = logging.getLogger(__name__)

_QUEUE_GET_TIMEOUT_S = 0.05
_STRAIGHT_ANGLE_DEG = 90.0


class PursuitPlanner(PlannerBase):
    """Closes distance to a moving target, holding a standoff.

    While the target is visible: steer toward its bearing
    (``90 + steer_gain * bearing_deg``, clamped to ``[0, 180]``) and drive at a
    speed proportional to the distance error (``speed_kp * (distance -
    target_distance_cm)``, clamped to ``±max_speed_pct``), with a deadband around
    the standoff so the robot holds station instead of hunting. When the target is
    not visible: stop.

    Parameters
    ----------
    device_id : str
        Device identifier included in every ``ActionPlan``.
    target_distance_cm : float
        Standoff distance to hold from the target. Default 80.0.
    max_speed_pct : float
        Magnitude cap on drive speed (0–100). Default 60.0.
    steer_gain : float
        Steering degrees of servo deflection per degree of target bearing.
        Default 2.0.
    speed_kp : float
        Drive percent per cm of distance error. Default 1.0.
    distance_deadband_cm : float
        No drive while within this many cm of the standoff. Default 15.0.
    steer_quantum_deg : float
        Steering angle is rounded to this step for debouncing. Default 5.0.
    speed_quantum_pct : float
        Drive speed is rounded to this step for debouncing. Default 5.0.
    """

    def __init__(
        self,
        device_id: str,
        target_distance_cm: float = 80.0,
        max_speed_pct: float = 60.0,
        steer_gain: float = 2.0,
        speed_kp: float = 1.0,
        distance_deadband_cm: float = 15.0,
        steer_quantum_deg: float = 5.0,
        speed_quantum_pct: float = 5.0,
    ) -> None:
        self._device_id = device_id
        self._target_distance_cm = target_distance_cm
        self._max_speed_pct = max_speed_pct
        self._steer_gain = steer_gain
        self._speed_kp = speed_kp
        self._distance_deadband_cm = distance_deadband_cm
        self._steer_quantum_deg = steer_quantum_deg
        self._speed_quantum_pct = speed_quantum_pct
        self._last_state: dict[str, Any] | None = None
        self._last_command: tuple[Any, ...] | None = None
        self._plan_counter = 0
        self._stop = asyncio.Event()

    async def run(
        self,
        queue_in: asyncio.Queue[WorldStateUpdate],
        queue_out: asyncio.Queue[ActionPlan],
    ) -> None:
        """Evaluate target state and emit pursuit/stop plans on command change."""
        while not self._stop.is_set():
            try:
                update = await asyncio.wait_for(queue_in.get(), timeout=_QUEUE_GET_TIMEOUT_S)
            except asyncio.TimeoutError:
                continue
            self._last_state = update.state
            await self._tick(queue_out)

    async def _tick(self, queue_out: asyncio.Queue[ActionPlan]) -> None:
        if self._last_state is None:
            return
        command, actions = self._select(self._last_state)
        if command != self._last_command:
            self._last_command = command
            await queue_out.put(self._build_plan(command[0], actions))

    def _select(self, state: dict[str, Any]) -> tuple[tuple[Any, ...], list[dict[str, Any]]]:
        """Return the (debounce-key, actions) for the current target state."""
        if not state.get("target_visible"):
            return ("lost",), [{"method": "stop", "params": {}, "priority": 0}]

        bearing = float(state.get("target_bearing_deg") or 0.0)
        distance = state.get("target_distance_cm")
        steer_angle = self._quantise(
            _clamp(_STRAIGHT_ANGLE_DEG + self._steer_gain * bearing, 0.0, 180.0),
            self._steer_quantum_deg,
        )
        speed = self._quantise(self._speed_for(distance), self._speed_quantum_pct)
        return (
            ("pursue", steer_angle, speed),
            [
                {"method": "steer", "params": {"angle_deg": steer_angle}, "priority": 0},
                {"method": "drive", "params": {"speed_pct": speed}, "priority": 1},
            ],
        )

    def _speed_for(self, distance: Any) -> float:
        """Proportional approach speed with a deadband around the standoff."""
        if distance is None:
            return 0.0
        error = float(distance) - self._target_distance_cm
        if abs(error) <= self._distance_deadband_cm:
            return 0.0
        return _clamp(self._speed_kp * error, -self._max_speed_pct, self._max_speed_pct)

    @staticmethod
    def _quantise(value: float, step: float) -> float:
        if step <= 0:
            return value
        return round(value / step) * step

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


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
