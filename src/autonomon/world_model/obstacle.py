"""ObstacleWorldModel: minimal threshold-based obstacle/cliff world model."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from autonomon.messages import PerceptionEvent, WorldStateUpdate
from autonomon.world_model.base import WorldModelBase

logger = logging.getLogger(__name__)

_QUEUE_GET_TIMEOUT_S = 0.05


class ObstacleWorldModel(WorldModelBase):
    """Fuses ultrasonic and grayscale events into a small boolean world state.

    This is the minimal world model needed to close the autonomy loop: it does
    not build an occupancy grid (Phase 3 full), it only tracks whether an
    obstacle is ahead and whether a cliff is detected. State is emitted only
    when it changes (delta-based), so the planner is not flooded with no-op
    updates.

    Parameters
    ----------
    device_id : str
        Device identifier included in every ``WorldStateUpdate``.
    obstacle_threshold_cm : float
        Distance at or below which ``obstacle_ahead`` becomes True. A ``None``
        ultrasonic reading (no echo / out of range) is treated as "clear".
    cliff_threshold : float
        Raw grayscale ADC value at or **below** which a channel is considered a
        cliff edge. On this hardware a reflective surface under the downward sensor
        reads *high* (~400-900 ADC) and a drop-off / no surface reads *low* (~30),
        so a cliff is a low reading. Defaults to ``200`` — comfortably between the
        floor (~400+) and an edge (~30). This consumes the **raw** ADC counts
        (``/api/sensor/grayscale``), not the normalised endpoint, whose calibration
        assumes the opposite sensor polarity. Ignored if no grayscale events arrive.
    """

    def __init__(
        self,
        device_id: str,
        obstacle_threshold_cm: float = 20.0,
        cliff_threshold: float = 200.0,
    ) -> None:
        self._device_id = device_id
        self._obstacle_threshold_cm = obstacle_threshold_cm
        self._cliff_threshold = cliff_threshold
        self._state: dict[str, Any] = {"obstacle_ahead": False, "cliff_detected": False}
        self._stop = asyncio.Event()

    async def run(self, queue_in: asyncio.Queue, queue_out: asyncio.Queue) -> None:  # type: ignore[type-arg]
        """Consume PerceptionEvents and emit WorldStateUpdates on state change.

        Parameters
        ----------
        queue_in : asyncio.Queue
            Source of ``PerceptionEvent.to_dict()`` items.
        queue_out : asyncio.Queue
            Receives ``WorldStateUpdate.to_dict()`` items. The first observation
            is always emitted as a baseline (``delta`` empty) so the planner has
            an initial world state; subsequent updates are emitted only when the
            tracked state changes.
        """
        emitted = False
        while not self._stop.is_set():
            try:
                msg = await asyncio.wait_for(queue_in.get(), timeout=_QUEUE_GET_TIMEOUT_S)
            except asyncio.TimeoutError:
                continue
            delta = self._apply(PerceptionEvent.from_dict(msg))
            if not emitted or delta:
                emitted = True
                await queue_out.put(self._build_update(delta).to_dict())

    def _apply(self, event: PerceptionEvent) -> dict[str, Any]:
        """Update state from one event; return the changed fields (empty if none)."""
        if event.sensor_type == "ultrasonic":
            distance = event.data.get("distance_cm")
            obstacle = distance is not None and distance <= self._obstacle_threshold_cm
            return self._set("obstacle_ahead", obstacle)
        if event.sensor_type == "grayscale":
            values = event.data.get("values") or []
            # Low raw reading = no reflective surface under the sensor = drop-off /
            # edge. A cliff is therefore a LOW reading: any channel at or below the
            # threshold (a reflective floor reads high, ~400-900; an edge ~30).
            cliff = any(v is not None and v <= self._cliff_threshold for v in values)
            return self._set("cliff_detected", cliff)
        return {}

    def _set(self, key: str, value: Any) -> dict[str, Any]:
        if self._state.get(key) == value:
            return {}
        self._state[key] = value
        return {key: value}

    def _build_update(self, delta: dict[str, Any]) -> WorldStateUpdate:
        return WorldStateUpdate(
            timestamp=datetime.now(timezone.utc).isoformat(),
            device_id=self._device_id,
            state=dict(self._state),
            delta=delta,
        )

    async def stop(self) -> None:
        self._stop.set()
