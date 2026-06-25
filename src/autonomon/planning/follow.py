"""FollowPlanner: camera-tracking, searching, distance-keeping person follower.

Supersedes :class:`~autonomon.planning.pursuit.PursuitPlanner` for the
``follow-user`` routine. It consumes the ``TargetWorldModel`` state and emits
plans that:

* **track** — when the target is visible, pan/tilt the camera to re-centre the
  person (proportional control on the in-frame bearings), steer the body toward
  the *body-relative* bearing (camera pan offset + in-frame bearing) so the
  camera self-recentres toward forward as the body turns in, and drive to hold a
  ``target_distance_cm`` standoff (backing up if too close);
* **search** — when the target is not visible, sweep the camera pan/tilt to
  "look around"; once a full sweep finds nobody, pivot the body (a steer-and-drive
  arc — Ackermann steering cannot rotate truly in place) to bring new heading into
  view, then resume sweeping.

Pure logic, no I/O: fully testable by pushing ``WorldStateUpdate``s into a queue
and stepping the monotonic clock. Like ``AvoidancePlanner`` the planner is
**time-driven** — ``run`` ticks on every loop iteration (message or idle timeout)
so the search sweep advances even while the world model is silent. Camera
centring is integrated once per *new* world state (not on idle ticks) so a stale
state cannot make the pan integrator run away; the action layer's lease renewal
holds the last command between updates.

Known limitation: distance is estimated from the person's box height, so tilting
the camera (which re-frames the person) can shift that estimate. Acceptable here —
tilt's job is keeping the user in frame, not ranging.
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timezone
from typing import Any

from autonomon.messages import ActionPlan, WorldStateUpdate
from autonomon.planning.base import PlannerBase

logger = logging.getLogger(__name__)

_QUEUE_GET_TIMEOUT_S = 0.05
_STRAIGHT_ANGLE_DEG = 90.0
_CAM_QUANTUM_DEG = 2.0  # debounce step for pan/tilt commands

_PHASE_SWEEP = "sweep"
_PHASE_ROTATE = "rotate"


class FollowPlanner(PlannerBase):
    """Tracks, searches for, and holds a standoff to a followed person.

    Parameters
    ----------
    device_id : str
        Device identifier included in every ``ActionPlan``.
    target_distance_cm : float
        Standoff distance to hold from the target. Default 60.0 (≈ 2 ft).
    max_speed_pct : float
        Magnitude cap on drive speed (0–100). Default 60.0.
    min_drive_speed_pct : float
        Floor applied to a *non-zero* drive speed so commands clear the motors'
        stiction threshold (a tiny proportional speed cannot stall the robot).
        Speeds inside the distance deadband stay 0. Default 35.0.
    steer_gain : float
        Servo deflection (deg) per degree of body-relative target bearing.
        Default 2.0.
    max_steer_deg : float
        Hard cap on steering deflection from centre (deg); every steer command —
        tracking and the search pivot — is clamped to ``[90 - max_steer_deg,
        90 + max_steer_deg]``. Default 30.0.
    max_steer_step_deg : float
        Max change in the tracking steer angle per adjustment (deg). The wheels
        ramp toward the target angle in steps this size rather than snapping, so a
        target far to one side is approached over several frames. Default 10.0.
    speed_kp : float
        Drive percent per cm of distance error. Default 1.0.
    drive_burst_s : float
        Maximum seconds to keep driving after a fresh detection. If no new
        detection arrives within this window the motor is paused (steer/camera
        hold) until the next one, so the robot drives in short bursts and re-aims
        each detection instead of running open-loop on a stale heading between the
        sparse (~0.8 Hz) detections. Default 0.6.
    steer_burst_s : float
        Seconds into a drive burst to hold the steered angle before returning the
        wheels to straight. After this window the robot continues driving forward
        (not steered) for the rest of the burst, limiting how far the camera drifts
        from the bearing where the user was detected. Must be < drive_burst_s to
        have any effect; ignored when steer_burst_s ≥ drive_burst_s. Default 0.3.
    distance_deadband_cm : float
        No drive while within this many cm of the standoff — wide enough that
        smoothed ultrasonic noise near the setpoint does not make the drive hunt.
        Default 20.0.
    steer_quantum_deg, speed_quantum_pct : float
        Steering angle / drive speed are rounded to these steps for debouncing.
        Defaults 5.0 / 5.0.
    camera_steer_comp_gain : float
        Feed-forward pan compensation applied when a steer command is issued:
        ``effective_pan = pan_angle - pan_dir × gain × (steer - 90)``.  When the
        wheels turn right the body physically rotates right, sweeping the camera
        off the user; this offsets the pan servo in the opposite direction so the
        user stays in frame during the steer burst.  The sign is derived
        automatically from ``pan_gain`` (positive or negative servo).  Default 0.5.
    pan_gain, tilt_gain : float
        Camera pan/tilt degrees commanded per degree of in-frame bearing error
        (proportional centring). Default 0.5 each. **Set negative to invert** a
        servo whose direction is reversed on a given unit (the camera would
        otherwise drive the target out of frame).
    center_deadband_deg : float
        In-frame bearing magnitude within which the camera is left still while
        tracking. Stops a roughly-centred target from being nudged out of frame
        by detection jitter (the cause of a lock that never settles). Default 4.0.
    pan_min_deg, pan_max_deg : float
        Camera pan servo limits. Default 20.0 / 160.0 (90 = forward).
    tilt_min_deg, tilt_max_deg : float
        Camera tilt servo limits. Default 60.0 / 120.0 (90 = level).
    search_step_deg : float
        Phase advance (deg) per search step around the roll; one full roll is
        360°, so ``360 / search_step_deg`` steps per roll. Default 10.0.
    search_interval_s : float
        Seconds between search steps (also the per-position dwell). Default 0.4.
    search_tilt_offset_deg : float
        Tilt amplitude of the roll — the camera bobs ``±search_tilt_offset_deg``
        about level as it sweeps, clamped to the tilt limits. Default 15.0.
    body_rotate_speed_pct : float
        Drive speed for the body-pivot arc when a sweep is exhausted. Default 60.0
        (a pivot must overcome stiction to actually rotate the chassis).
    body_rotate_duration_s : float
        Seconds to commit to the body-pivot arc before resuming the sweep.
        Default 1.5.
    body_rotate_steer_deg : float
        Steering angle held during the body-pivot arc (also clamped by
        ``max_steer_deg``). Default 120.0.
    """

    def __init__(
        self,
        device_id: str,
        target_distance_cm: float = 60.0,
        max_speed_pct: float = 60.0,
        min_drive_speed_pct: float = 35.0,
        steer_gain: float = 2.0,
        max_steer_deg: float = 30.0,
        max_steer_step_deg: float = 10.0,
        speed_kp: float = 1.0,
        drive_burst_s: float = 0.6,
        steer_burst_s: float = 0.3,
        distance_deadband_cm: float = 20.0,
        steer_quantum_deg: float = 5.0,
        speed_quantum_pct: float = 5.0,
        camera_steer_comp_gain: float = 0.5,
        pan_gain: float = 0.5,
        tilt_gain: float = 0.5,
        center_deadband_deg: float = 4.0,
        pan_min_deg: float = 20.0,
        pan_max_deg: float = 160.0,
        tilt_min_deg: float = 60.0,
        tilt_max_deg: float = 120.0,
        search_step_deg: float = 10.0,
        search_interval_s: float = 0.4,
        search_tilt_offset_deg: float = 15.0,
        body_rotate_speed_pct: float = 60.0,
        body_rotate_duration_s: float = 1.5,
        body_rotate_steer_deg: float = 120.0,
    ) -> None:
        self._device_id = device_id
        self._target_distance_cm = target_distance_cm
        self._max_speed_pct = max_speed_pct
        self._min_drive_speed_pct = min_drive_speed_pct
        self._steer_gain = steer_gain
        self._max_steer_deg = max_steer_deg
        self._max_steer_step_deg = max_steer_step_deg
        self._speed_kp = speed_kp
        self._drive_burst_s = drive_burst_s
        self._steer_burst_s = steer_burst_s
        self._distance_deadband_cm = distance_deadband_cm
        self._steer_quantum_deg = steer_quantum_deg
        self._speed_quantum_pct = speed_quantum_pct
        self._camera_steer_comp_gain = camera_steer_comp_gain
        self._pan_gain = pan_gain
        self._tilt_gain = tilt_gain
        self._center_deadband_deg = center_deadband_deg
        self._pan_min_deg = pan_min_deg
        self._pan_max_deg = pan_max_deg
        self._tilt_min_deg = tilt_min_deg
        self._tilt_max_deg = tilt_max_deg
        self._search_step_deg = search_step_deg
        self._search_interval_s = search_interval_s
        self._body_rotate_speed_pct = body_rotate_speed_pct
        self._body_rotate_duration_s = body_rotate_duration_s
        self._body_rotate_steer_deg = body_rotate_steer_deg

        # Search "roll": pan and tilt trace an ellipse (90° out of phase) as the
        # phase advances, so the camera sweeps *around* — like a person looking
        # about to get their bearings — instead of panning along one flat line.
        # Pan spans the full [min, max]; tilt bobs ±search_tilt_offset_deg about level.
        self._pan_center = (pan_min_deg + pan_max_deg) / 2.0
        self._pan_amp = (pan_max_deg - pan_min_deg) / 2.0
        self._tilt_amp = search_tilt_offset_deg

        # Commanded camera angles (carried across captures for tracking + sweeping).
        self._pan_angle = _STRAIGHT_ANGLE_DEG
        self._tilt_angle = _STRAIGHT_ANGLE_DEG

        # Search state machine.
        self._search_phase = _PHASE_SWEEP
        self._sweep_phase = 0.0  # degrees around the roll; one full roll = 360°
        self._last_step_at: float | None = None
        self._phase_started_at: float | None = None

        self._last_state: dict[str, Any] | None = None
        self._state_dirty = False
        # Drive-burst state: drive only briefly after each fresh detection, then
        # hold until the next one — so the robot re-aims rather than careening on a
        # stale heading between sparse detections.
        self._last_detect_at: float | None = None
        self._driving = False
        self._straightened = False
        self._last_steer = _STRAIGHT_ANGLE_DEG
        self._last_speed = 0.0
        # Effective (compensation-applied) pan last sent to the servo; held by _hold/_straighten.
        self._last_commanded_pan = self._pan_angle
        # Vision frame last acted on. Tracking keys off a *new vision frame*
        # (vision_seq), not every world-state update — the ultrasonic emits
        # frequent distance-only updates carrying a stale bearing, and
        # re-integrating the pan on those makes the camera run away.
        self._last_vision_seq: int | None = None
        self._last_command: tuple[Any, ...] | None = None
        self._plan_counter = 0
        self._stop = asyncio.Event()

    async def run(
        self,
        queue_in: asyncio.Queue[WorldStateUpdate],
        queue_out: asyncio.Queue[ActionPlan],
    ) -> None:
        """Track/search/approach, emitting ActionPlans on command change.

        Ticks on every loop iteration (message or idle timeout) so the search
        sweep advances even while the world model emits nothing.
        """
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            try:
                update = await asyncio.wait_for(queue_in.get(), timeout=_QUEUE_GET_TIMEOUT_S)
            except asyncio.TimeoutError:
                update = None
            if update is not None:
                self._last_state = update.state
                self._state_dirty = True
            await self._tick(queue_out, loop.time())

    async def _tick(self, queue_out: asyncio.Queue[ActionPlan], now: float) -> None:
        state = self._last_state
        if state is None:
            return
        if state.get("target_visible"):
            # Only a *new vision frame* drives tracking (camera/steer/drive).
            # Distance-only updates (frequent, from the ultrasonic) carry a stale
            # bearing; acting on them would walk the camera off the target.
            # A state without ``vision_seq`` (e.g. unit tests) is always fresh;
            # the real world model stamps a per-frame seq so distance-only updates
            # are skipped.
            vision_seq = state.get("vision_seq")
            fresh_frame = self._state_dirty and (
                vision_seq is None or vision_seq != self._last_vision_seq
            )
            self._state_dirty = False
            if fresh_frame:
                self._last_vision_seq = vision_seq
                command, actions = self._track(state)
                self._last_detect_at = now
                self._driving = self._last_speed != 0.0
                self._straightened = False
            elif self._driving and self._last_detect_at is not None:
                elapsed = now - self._last_detect_at
                if elapsed >= self._drive_burst_s:
                    # Burst elapsed: pause the motor and hold until the next detection.
                    self._driving = False
                    command, actions = self._hold()
                elif elapsed >= self._steer_burst_s and not self._straightened:
                    # Steer window closed: finish the burst going straight so the
                    # camera angle doesn't drift far from where the user was detected.
                    self._straightened = True
                    command, actions = self._straighten()
                else:
                    return  # mid-burst, lease renewal continues the current command
            else:
                return  # already paused
        else:
            self._state_dirty = False
            # Re-arm so the first frame on reacquire counts as fresh.
            self._last_vision_seq = None
            self._advance_search(now)
            command, actions = self._search_actions()
        if command != self._last_command:
            self._last_command = command
            await queue_out.put(self._build_plan(command[0], actions))

    def _track(self, state: dict[str, Any]) -> tuple[tuple[Any, ...], list[dict[str, Any]]]:
        """Centre the camera, steer the body toward the target, and close distance."""
        frame_bearing = float(state.get("target_bearing_deg") or 0.0)
        frame_vbearing = float(state.get("target_vertical_bearing_deg") or 0.0)
        distance = state.get("target_distance_cm")

        # Camera centring (proportional), integrated once per new world state.
        # A deadband around centre leaves the camera still for a roughly-centred
        # target, so detection jitter cannot walk it off the locked target.
        pan_before = self._pan_angle
        if abs(frame_bearing) > self._center_deadband_deg:
            self._pan_angle = _clamp(
                self._pan_angle + self._pan_gain * frame_bearing,
                self._pan_min_deg,
                self._pan_max_deg,
            )
        if abs(frame_vbearing) > self._center_deadband_deg:
            self._tilt_angle = _clamp(
                self._tilt_angle + self._tilt_gain * frame_vbearing,
                self._tilt_min_deg,
                self._tilt_max_deg,
            )

        # Body steers toward the true body-relative bearing so the camera can
        # re-centre toward forward as the body turns in. The camera's physical
        # pan direction follows the sign of pan_gain (negative on the inverted
        # PicarX pan servo, where a higher angle aims left), so the pan offset's
        # contribution to the body bearing flips with it — otherwise the wheels
        # steer away from a target the camera has panned toward.
        pan_dir = 1.0 if self._pan_gain >= 0 else -1.0
        body_bearing = pan_dir * (pan_before - _STRAIGHT_ANGLE_DEG) + frame_bearing
        target_steer = self._clamp_steer(_STRAIGHT_ANGLE_DEG + self._steer_gain * body_bearing)
        # Slew-limit the steer change so the wheels ramp toward the target in small
        # steps instead of snapping to the cap in one adjustment.
        step = _clamp(
            target_steer - self._last_steer, -self._max_steer_step_deg, self._max_steer_step_deg
        )
        steer_angle = self._quantise(self._last_steer + step, self._steer_quantum_deg)
        speed = self._quantise(self._speed_for(distance), self._speed_quantum_pct)
        self._last_steer = steer_angle
        self._last_speed = speed

        # Re-arm the search state machine for the next time the target is lost.
        self._reset_search()

        # Feed-forward steer compensation: when the wheels turn, the body physically
        # rotates and the camera (body-fixed) sweeps the user out of frame.  Pre-pan
        # the camera opposite to the turn so the rotation keeps the user centred.
        # _pan_angle stays the clean tracking target; effective_pan is what the servo
        # actually receives.  The sign flips with pan_dir so it works for both normal
        # and inverted pan servos.
        steer_deflection = steer_angle - _STRAIGHT_ANGLE_DEG
        pan_comp = -pan_dir * self._camera_steer_comp_gain * steer_deflection
        effective_pan = _clamp(self._pan_angle + pan_comp, self._pan_min_deg, self._pan_max_deg)
        self._last_commanded_pan = effective_pan

        logger.info(
            "track: bearing=%.1f° vbearing=%.1f° pan %.0f→%.0f (eff %.0f) tilt→%.0f steer=%.0f speed=%.0f",
            frame_bearing,
            frame_vbearing,
            pan_before,
            self._pan_angle,
            effective_pan,
            self._tilt_angle,
            steer_angle,
            speed,
        )

        command = (
            "track",
            self._quantise(effective_pan, _CAM_QUANTUM_DEG),
            self._quantise(self._tilt_angle, _CAM_QUANTUM_DEG),
            steer_angle,
            speed,
        )
        actions = [
            {"method": "pan", "params": {"angle_deg": effective_pan}, "priority": 0},
            {"method": "tilt", "params": {"angle_deg": self._tilt_angle}, "priority": 1},
            {"method": "steer", "params": {"angle_deg": steer_angle}, "priority": 2},
            {"method": "drive", "params": {"speed_pct": speed}, "priority": 3},
        ]
        return command, actions

    def _hold(self) -> tuple[tuple[Any, ...], list[dict[str, Any]]]:
        """Pause the drive between detection bursts, holding the camera and steer.

        Keeps the camera and steering where the last detection left them and zeroes
        the drive, so the robot waits (re-aiming on the next detection) instead of
        continuing on a stale heading.
        """
        logger.info("hold: pausing drive (no fresh detection within burst window)")
        command = (
            "hold",
            self._quantise(self._last_commanded_pan, _CAM_QUANTUM_DEG),
            self._quantise(self._tilt_angle, _CAM_QUANTUM_DEG),
            self._last_steer,
        )
        actions = [
            {"method": "pan", "params": {"angle_deg": self._last_commanded_pan}, "priority": 0},
            {"method": "tilt", "params": {"angle_deg": self._tilt_angle}, "priority": 1},
            {"method": "steer", "params": {"angle_deg": self._last_steer}, "priority": 2},
            {"method": "drive", "params": {"speed_pct": 0.0}, "priority": 3},
        ]
        return command, actions

    def _straighten(self) -> tuple[tuple[Any, ...], list[dict[str, Any]]]:
        """Return wheels to straight mid-burst while continuing to drive.

        Steer deflection becomes zero so the feed-forward compensation is also zero:
        effective_pan == _pan_angle.  Updates _last_steer so the next detection's
        slew ramp starts from straight.
        """
        self._last_steer = _STRAIGHT_ANGLE_DEG
        self._last_commanded_pan = self._pan_angle
        logger.info("track-straight: centring wheels mid-burst (speed=%.0f)", self._last_speed)
        command = (
            "track-straight",
            self._quantise(self._pan_angle, _CAM_QUANTUM_DEG),
            self._quantise(self._tilt_angle, _CAM_QUANTUM_DEG),
            _STRAIGHT_ANGLE_DEG,
            self._last_speed,
        )
        actions = [
            {"method": "pan", "params": {"angle_deg": self._pan_angle}, "priority": 0},
            {"method": "tilt", "params": {"angle_deg": self._tilt_angle}, "priority": 1},
            {"method": "steer", "params": {"angle_deg": _STRAIGHT_ANGLE_DEG}, "priority": 2},
            {"method": "drive", "params": {"speed_pct": self._last_speed}, "priority": 3},
        ]
        return command, actions

    def _advance_search(self, now: float) -> None:
        """Step the search state machine using monotonic time."""
        if self._search_phase == _PHASE_ROTATE:
            if (
                self._phase_started_at is not None
                and now - self._phase_started_at >= self._body_rotate_duration_s
            ):
                self._search_phase = _PHASE_SWEEP
                self._sweep_phase = 0.0
                self._last_step_at = None
            return

        # Sweep phase: roll the gaze around an ellipse, one phase step per interval.
        # pan = centre + amp·sin(phase); tilt = level - amp·cos(phase), so the
        # camera traces a loop rather than scanning a single horizontal line. The
        # minus on tilt makes the roll *start looking up* (higher tilt angle aims
        # down on this hardware, so subtracting raises the gaze at phase 0).
        if self._last_step_at is not None and now - self._last_step_at < self._search_interval_s:
            return
        self._last_step_at = now
        self._sweep_phase += self._search_step_deg
        rad = math.radians(self._sweep_phase)
        self._pan_angle = _clamp(
            self._pan_center + self._pan_amp * math.sin(rad),
            self._pan_min_deg,
            self._pan_max_deg,
        )
        self._tilt_angle = _clamp(
            _STRAIGHT_ANGLE_DEG - self._tilt_amp * math.cos(rad),
            self._tilt_min_deg,
            self._tilt_max_deg,
        )
        # After a full roll with no target, pivot the body to look elsewhere.
        if self._sweep_phase >= 360.0:
            self._search_phase = _PHASE_ROTATE
            self._phase_started_at = now

    def _search_actions(self) -> tuple[tuple[Any, ...], list[dict[str, Any]]]:
        """Build the plan for the current search phase."""
        if self._search_phase == _PHASE_ROTATE:
            steer = self._quantise(
                self._clamp_steer(self._body_rotate_steer_deg), self._steer_quantum_deg
            )
            speed = self._quantise(self._body_rotate_speed_pct, self._speed_quantum_pct)
            command = ("search-rotate", steer, speed)
            actions = [
                {"method": "steer", "params": {"angle_deg": steer}, "priority": 0},
                {"method": "drive", "params": {"speed_pct": speed}, "priority": 1},
            ]
            return command, actions
        command = (
            "search-sweep",
            self._quantise(self._pan_angle, _CAM_QUANTUM_DEG),
            self._quantise(self._tilt_angle, _CAM_QUANTUM_DEG),
        )
        actions = [
            {"method": "pan", "params": {"angle_deg": self._pan_angle}, "priority": 0},
            {"method": "tilt", "params": {"angle_deg": self._tilt_angle}, "priority": 1},
            {"method": "stop", "params": {}, "priority": 2},
        ]
        return command, actions

    def _reset_search(self) -> None:
        """Reset the search state machine (not the camera angles) on reacquire."""
        self._search_phase = _PHASE_SWEEP
        self._sweep_phase = 0.0
        self._last_step_at = None
        self._phase_started_at = None

    def _speed_for(self, distance: Any) -> float:
        """Proportional approach speed with a deadband around the standoff.

        Outside the deadband the speed is floored to ``min_drive_speed_pct`` (sign
        preserved) so a small distance error still produces enough drive to move
        the robot rather than a stalled, sub-stiction command.
        """
        if distance is None:
            return 0.0
        error = float(distance) - self._target_distance_cm
        if abs(error) <= self._distance_deadband_cm:
            return 0.0
        speed = _clamp(self._speed_kp * error, -self._max_speed_pct, self._max_speed_pct)
        if 0.0 < abs(speed) < self._min_drive_speed_pct:
            speed = self._min_drive_speed_pct if speed > 0 else -self._min_drive_speed_pct
        return speed

    def _clamp_steer(self, angle: float) -> float:
        """Clamp a steering angle to ``±max_steer_deg`` around centre (90)."""
        return _clamp(
            angle,
            _STRAIGHT_ANGLE_DEG - self._max_steer_deg,
            _STRAIGHT_ANGLE_DEG + self._max_steer_deg,
        )

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
