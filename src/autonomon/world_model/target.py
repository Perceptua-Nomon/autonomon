"""TargetWorldModel: tracks a followed target's relative position over time.

Consumes vision ``PerceptionEvent``s (from
:class:`~autonomon.perception.vision.VisionPerception`) and maintains a smoothed
estimate of the target's bearing and range, plus whether the target is currently
visible. Brief detection dropouts are bridged by a ``lost_target_timeout_s`` hold,
so a single missed frame does not stop a pursuit. Emits ``WorldStateUpdate``s
delta-style (only on meaningful change) for the pursuit planner.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from autonomon.messages import PerceptionEvent, WorldStateUpdate
from autonomon.world_model.base import WorldModelBase

logger = logging.getLogger(__name__)

_QUEUE_GET_TIMEOUT_S = 0.05


class TargetWorldModel(WorldModelBase):
    """Fuses vision events into a smoothed, time-aware target state.

    State shape::

        {"target_visible": bool,
         "target_bearing_deg": float | None,
         "target_distance_cm": float | None}

    Parameters
    ----------
    device_id : str
        Device identifier included in every ``WorldStateUpdate``.
    lost_target_timeout_s : float
        Seconds to keep ``target_visible`` True after the last positive detection,
        bridging brief dropouts. Default 1.5 s.
    smoothing : float
        EMA weight in ``[0, 1]`` for new measurements (``new = smoothing*meas +
        (1-smoothing)*prev``). Higher tracks faster but is noisier. Default 0.5.
    emit_bearing_epsilon_deg : float
        Minimum bearing change (deg) that triggers a new update while visible.
        Default 2.0.
    emit_distance_epsilon_cm : float
        Minimum distance change (cm) that triggers a new update while visible.
        Default 5.0.
    """

    def __init__(
        self,
        device_id: str,
        lost_target_timeout_s: float = 1.5,
        smoothing: float = 0.5,
        emit_bearing_epsilon_deg: float = 2.0,
        emit_distance_epsilon_cm: float = 5.0,
    ) -> None:
        self._device_id = device_id
        self._lost_target_timeout_s = lost_target_timeout_s
        self._smoothing = smoothing
        self._emit_bearing_epsilon_deg = emit_bearing_epsilon_deg
        self._emit_distance_epsilon_cm = emit_distance_epsilon_cm
        self._visible = False
        self._bearing: float | None = None
        self._distance: float | None = None
        self._last_seen: float | None = None
        self._last_emitted: dict[str, Any] | None = None
        self._stop = asyncio.Event()

    async def run(
        self,
        queue_in: asyncio.Queue[PerceptionEvent],
        queue_out: asyncio.Queue[WorldStateUpdate],
    ) -> None:
        """Consume vision events and emit target-state updates until stopped.

        Also re-checks the lost-target timeout while the queue is idle, so the
        target is marked lost on schedule even if perception stops emitting.
        """
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            try:
                event = await asyncio.wait_for(queue_in.get(), timeout=_QUEUE_GET_TIMEOUT_S)
            except asyncio.TimeoutError:
                event = None
            now = loop.time()
            if event is not None:
                self._observe(event, now)
            self._age_out(now)
            await self._maybe_emit(queue_out)

    def _observe(self, event: PerceptionEvent, now: float) -> None:
        """Fold one vision event into the tracked state."""
        if not event.data.get("detected"):
            return
        bearing = event.data.get("target_bearing_deg")
        distance = event.data.get("target_distance_cm")
        self._bearing = self._ema(self._bearing, bearing)
        self._distance = self._ema(self._distance, distance)
        self._visible = True
        self._last_seen = now

    def _age_out(self, now: float) -> None:
        """Drop visibility once the target has not been seen within the timeout."""
        if not self._visible:
            return
        if self._last_seen is None or now - self._last_seen > self._lost_target_timeout_s:
            self._visible = False
            self._bearing = None
            self._distance = None

    def _ema(self, prev: float | None, measurement: Any) -> float | None:
        if measurement is None:
            return prev
        value = float(measurement)
        if prev is None:
            return value
        return self._smoothing * value + (1.0 - self._smoothing) * prev

    async def _maybe_emit(self, queue_out: asyncio.Queue[WorldStateUpdate]) -> None:
        state: dict[str, Any] = {
            "target_visible": self._visible,
            "target_bearing_deg": self._bearing if self._visible else None,
            "target_distance_cm": self._distance if self._visible else None,
        }
        if not self._should_emit(state):
            return
        delta = {} if self._last_emitted is None else self._diff(self._last_emitted, state)
        self._last_emitted = state
        await queue_out.put(
            WorldStateUpdate(
                timestamp=datetime.now(timezone.utc).isoformat(),
                device_id=self._device_id,
                state=dict(state),
                delta=delta,
            )
        )

    def _should_emit(self, state: dict[str, Any]) -> bool:
        if self._last_emitted is None:
            return True  # baseline
        if state["target_visible"] != self._last_emitted["target_visible"]:
            return True
        if not state["target_visible"]:
            return False  # both not-visible: nothing new
        return self._moved(state, self._last_emitted)

    def _moved(self, new: dict[str, Any], old: dict[str, Any]) -> bool:
        bearing_moved = self._delta_exceeds(
            new["target_bearing_deg"], old["target_bearing_deg"], self._emit_bearing_epsilon_deg
        )
        distance_moved = self._delta_exceeds(
            new["target_distance_cm"], old["target_distance_cm"], self._emit_distance_epsilon_cm
        )
        return bearing_moved or distance_moved

    @staticmethod
    def _delta_exceeds(new: float | None, old: float | None, epsilon: float) -> bool:
        if new is None or old is None:
            return new is not old
        return abs(new - old) >= epsilon

    @staticmethod
    def _diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in new.items() if old.get(k) != v}

    async def stop(self) -> None:
        self._stop.set()
