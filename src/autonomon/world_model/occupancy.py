"""OccupancyWorldModel: a robot-centric local occupancy grid with time decay.

This is the Phase-3 "occupancy-grid world model". Unlike
:class:`~autonomon.world_model.obstacle.ObstacleWorldModel` — which keeps only
two booleans and forgets immediately — this model maintains a small **spatial
grid** of where obstacles were recently seen and **decays** each cell after
``decay_s`` seconds. That short-term memory is the value it adds: a planner can
tell "an obstacle was here moments ago" (``recently_blocked``) and stay cautious
after the front sensor clears, rather than oscillating.

**Scope (honest).** The grid is **robot-centric** and, with the fleet's single
forward ultrasonic and no odometry/heading source, obstacle observations are
placed along the forward axis only (``ix == 0``). It is therefore effectively a
decaying forward range-profile, not a world-frame map. The 2-D cell key and the
``occupancy`` snapshot are kept so the same model upgrades cleanly to off-axis
placement once a sweep or odometry source exists (see ADR-007). It is introduced
with a concrete consumer — the ``patrol`` routine — per ADR-006.

It stays backward-compatible with the boolean planners by always emitting
``obstacle_ahead`` and ``cliff_detected`` alongside the new memory fields, so it
is a drop-in world model for any routine.
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

# Emission is triggered only when one of these salient fields changes, so the
# planner is not flooded as individual grid cells churn or decay. The full grid
# snapshot rides along in the emitted state for telemetry/inspection.
_SALIENT_FIELDS = ("obstacle_ahead", "cliff_detected", "recently_blocked")


class OccupancyWorldModel(WorldModelBase):
    """Maintains a decaying robot-centric obstacle grid plus boolean obstacle/cliff state.

    Ultrasonic readings mark a forward cell at the measured range; cells expire
    ``decay_s`` after they were last seen. Grayscale low readings set the cliff
    boolean (cliffs are an immediate condition, not mapped into the grid). The
    emitted ``state`` carries both the backward-compatible booleans and the
    memory fields derived from the grid.

    State shape::

        {"obstacle_ahead": bool,
         "cliff_detected": bool,
         "recently_blocked": bool,          # any non-decayed obstacle cell
         "occupied_cells": int,             # grid memory depth
         "nearest_obstacle_cm": float|None, # nearest remembered obstacle range
         "occupancy": [{"x": int, "y": int}, ...]}  # decayed cell snapshot

    Parameters
    ----------
    device_id : str
        Device identifier included in every ``WorldStateUpdate``.
    cell_size_cm : float
        Grid resolution: the forward range is quantised into cells this many cm
        deep. Default 10 cm.
    grid_radius_cm : float
        Maximum range (cm) recorded in the grid; readings beyond it are treated
        as clear and not mapped. Default 100 cm.
    decay_s : float
        Seconds an obstacle cell persists after its last sighting before it ages
        out (the configurable state-decay deliverable). Default 3.0 s.
    obstacle_threshold_cm : float
        Distance at or below which ``obstacle_ahead`` is True. A ``None`` reading
        (no echo / out of range) is treated as clear. Default 20 cm.
    cliff_threshold : float
        Raw grayscale ADC value at or **below** which a channel is a cliff edge.
        Matches :class:`ObstacleWorldModel`'s polarity (a reflective floor reads
        high ~400-900; a drop-off reads low ~30). Default 200.
    """

    def __init__(
        self,
        device_id: str,
        cell_size_cm: float = 10.0,
        grid_radius_cm: float = 100.0,
        decay_s: float = 3.0,
        obstacle_threshold_cm: float = 20.0,
        cliff_threshold: float = 200.0,
    ) -> None:
        self._device_id = device_id
        self._cell_size_cm = cell_size_cm
        self._grid_radius_cm = grid_radius_cm
        self._decay_s = decay_s
        self._obstacle_threshold_cm = obstacle_threshold_cm
        self._cliff_threshold = cliff_threshold
        self._cells: dict[tuple[int, int], float] = {}  # (ix, iy) -> last-seen monotonic
        self._obstacle_ahead = False
        self._cliff_detected = False
        self._last_emitted: dict[str, Any] | None = None
        self._stop = asyncio.Event()

    async def run(
        self,
        queue_in: asyncio.Queue[PerceptionEvent],
        queue_out: asyncio.Queue[WorldStateUpdate],
    ) -> None:
        """Fold perception events into the grid and emit updates on salient change.

        The grid is also decayed on the idle tick, so ``recently_blocked`` falls
        back to False on schedule (and emits) even if perception goes quiet.

        Parameters
        ----------
        queue_in : asyncio.Queue[PerceptionEvent]
            Source of ``PerceptionEvent`` instances (ultrasonic, grayscale).
        queue_out : asyncio.Queue[WorldStateUpdate]
            Receives ``WorldStateUpdate`` instances. The first observation is
            emitted as a baseline (empty ``delta``); subsequent updates only when
            a salient field changes.
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
            self._decay(now)
            await self._maybe_emit(queue_out, now)

    def _observe(self, event: PerceptionEvent, now: float) -> None:
        """Fold one perception event into the grid and the boolean state."""
        if event.sensor_type == "ultrasonic":
            distance = event.data.get("distance_cm")
            self._obstacle_ahead = distance is not None and distance <= self._obstacle_threshold_cm
            if distance is not None and distance <= self._grid_radius_cm:
                # Forward axis only (ix == 0) absent a heading source; iy is the
                # quantised range, clamped to >= 1 so the robot's own cell (0,0)
                # is never marked occupied.
                iy = max(1, int(round(distance / self._cell_size_cm)))
                self._cells[(0, iy)] = now
        elif event.sensor_type == "grayscale":
            values = event.data.get("values") or []
            self._cliff_detected = any(v is not None and v <= self._cliff_threshold for v in values)

    def _decay(self, now: float) -> None:
        """Drop obstacle cells not refreshed within ``decay_s``."""
        expired = [cell for cell, seen in self._cells.items() if now - seen > self._decay_s]
        for cell in expired:
            del self._cells[cell]

    def _snapshot(self) -> dict[str, Any]:
        """Build the current world-state dict from the (already-decayed) grid."""
        nearest_cm: float | None = None
        if self._cells:
            nearest_iy = min(iy for (_, iy) in self._cells)
            nearest_cm = nearest_iy * self._cell_size_cm
        return {
            "obstacle_ahead": self._obstacle_ahead,
            "cliff_detected": self._cliff_detected,
            "recently_blocked": bool(self._cells),
            "occupied_cells": len(self._cells),
            "nearest_obstacle_cm": nearest_cm,
            "occupancy": [{"x": ix, "y": iy} for (ix, iy) in sorted(self._cells)],
        }

    async def _maybe_emit(self, queue_out: asyncio.Queue[WorldStateUpdate], now: float) -> None:
        state = self._snapshot()
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
        return any(state[k] != self._last_emitted[k] for k in _SALIENT_FIELDS)

    @staticmethod
    def _diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in new.items() if old.get(k) != v}

    async def stop(self) -> None:
        self._stop.set()
