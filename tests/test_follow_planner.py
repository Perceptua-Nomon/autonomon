"""Tests for FollowPlanner — camera tracking, coupled steering, distance, search."""

from __future__ import annotations

import asyncio

import pytest

from autonomon import ActionPlan, FollowPlanner, WorldStateUpdate


def _state(
    visible: bool,
    bearing: float | None = None,
    vertical_bearing: float | None = None,
    distance: float | None = None,
    seq: int | None = None,
) -> WorldStateUpdate:
    return WorldStateUpdate(
        timestamp="t",
        device_id="d",
        state={
            "target_visible": visible,
            "target_bearing_deg": bearing,
            "target_vertical_bearing_deg": vertical_bearing,
            "target_distance_cm": distance,
            "vision_seq": seq,
        },
    )


def _action(plan: ActionPlan, method: str) -> dict:
    return next(a for a in plan.actions if a["method"] == method)


def _methods(plan: ActionPlan) -> list[str]:
    return [a["method"] for a in plan.actions]


async def _run_script(
    planner: FollowPlanner, script: list[tuple[WorldStateUpdate | None, float]]
) -> list[ActionPlan]:
    """Run the planner, applying ``(state, pause_s)`` steps, then drain emitted plans."""
    q_in: asyncio.Queue[WorldStateUpdate] = asyncio.Queue()
    q_out: asyncio.Queue[ActionPlan] = asyncio.Queue()
    task = asyncio.create_task(planner.run(q_in, q_out))
    for state, pause in script:
        if state is not None:
            await q_in.put(state)
        await asyncio.sleep(pause)
    await planner.stop()
    await task
    plans = []
    while not q_out.empty():
        plans.append(q_out.get_nowait())
    return plans


# --- Tracking ------------------------------------------------------------


@pytest.mark.asyncio
async def test_visible_offcentre_emits_pan_tilt_steer_drive() -> None:
    planner = FollowPlanner(
        "d",
        target_distance_cm=60.0,
        pan_gain=0.5,
        tilt_gain=0.5,
        steer_gain=2.0,
        speed_kp=1.0,
        camera_steer_comp_gain=0.0,
    )
    plans = await _run_script(planner, [(_state(True, 20.0, 10.0, 200.0), 0.05)])

    assert plans, "expected a tracking plan"
    plan = plans[-1]
    assert plan.plan_id.startswith("track-")
    assert set(_methods(plan)) == {"pan", "tilt", "steer", "drive"}
    # Camera pans toward the right-of-centre target: 90 + 0.5*20 = 100.
    assert _action(plan, "pan")["params"]["angle_deg"] == pytest.approx(100.0)
    # Camera tilts toward the below-centre target: 90 + 0.5*10 = 95.
    assert _action(plan, "tilt")["params"]["angle_deg"] == pytest.approx(95.0)
    # Target steer is 90 + 2*20 = 130 (clamped to 120), but the slew limit moves it
    # only one 10° step from centre this frame → 100.
    assert _action(plan, "steer")["params"]["angle_deg"] == pytest.approx(100.0)
    # Far target → drive forward (clamped).
    assert _action(plan, "drive")["params"]["speed_pct"] > 0


@pytest.mark.asyncio
async def test_camera_pan_recentres_as_body_turns_in() -> None:
    """As the body turns toward the target, the in-frame bearing flips sign and the
    camera pan decays back toward forward (90)."""
    planner = FollowPlanner("d", pan_gain=0.5, camera_steer_comp_gain=0.0)
    plans = await _run_script(
        planner,
        [
            (_state(True, 20.0, 0.0, 120.0), 0.05),  # target right → pan to 100
            (_state(True, -10.0, 0.0, 120.0), 0.05),  # body turned in → target now left
        ],
    )
    pans = [_action(p, "pan")["params"]["angle_deg"] for p in plans if "pan" in _methods(p)]
    assert pans[0] == pytest.approx(100.0)
    assert pans[-1] == pytest.approx(95.0)  # 100 + 0.5*(-10) → recentred toward 90


@pytest.mark.asyncio
async def test_centred_target_holds_camera_still() -> None:
    """A roughly-centred target (within the centring deadband) must not move the
    camera — otherwise detection jitter walks it off the lock."""
    planner = FollowPlanner(
        "d", pan_gain=0.5, tilt_gain=0.5, center_deadband_deg=4.0, camera_steer_comp_gain=0.0
    )
    plans = await _run_script(planner, [(_state(True, 3.0, -2.0, 120.0), 0.05)])
    # bearing 3° and vbearing -2° are both inside the 4° deadband → camera stays at 90/90.
    assert _action(plans[-1], "pan")["params"]["angle_deg"] == pytest.approx(90.0)
    assert _action(plans[-1], "tilt")["params"]["angle_deg"] == pytest.approx(90.0)


@pytest.mark.asyncio
async def test_small_distance_error_floored_to_min_drive_speed() -> None:
    """Outside the distance deadband, a small proportional speed is floored so the
    robot actually moves (clears motor stiction)."""
    planner = FollowPlanner(
        "d",
        target_distance_cm=60.0,
        distance_deadband_cm=15.0,
        speed_kp=1.0,
        min_drive_speed_pct=35.0,
    )
    # distance 80 → error +20 (outside deadband); raw speed 20 < 35 → floored to 35.
    plans = await _run_script(planner, [(_state(True, 0.0, 0.0, 80.0), 0.05)])
    assert _action(plans[-1], "drive")["params"]["speed_pct"] == pytest.approx(35.0)


@pytest.mark.asyncio
async def test_steer_capped_at_max_steer_deg() -> None:
    """Steering is clamped to ±max_steer_deg around centre (slew lifted to isolate)."""
    planner = FollowPlanner(
        "d",
        pan_gain=0.5,
        center_deadband_deg=0.0,
        steer_gain=2.0,
        max_steer_deg=30.0,
        max_steer_step_deg=90.0,
    )
    plans = await _run_script(planner, [(_state(True, 40.0, 0.0, 120.0), 0.05)])
    # 90 + 2*40 = 170, capped to 90 + 30 = 120.
    assert _action(plans[-1], "steer")["params"]["angle_deg"] == pytest.approx(120.0)


@pytest.mark.asyncio
async def test_steer_ramps_in_small_steps() -> None:
    """The steer angle ramps toward the target by at most max_steer_step_deg each
    frame, rather than snapping."""
    planner = FollowPlanner(
        "d", pan_gain=0.5, center_deadband_deg=0.0, steer_gain=2.0, max_steer_step_deg=10.0
    )
    plans = await _run_script(
        planner,
        [
            (_state(True, 40.0, 0.0, 120.0, seq=1), 0.05),  # target steer 120 (capped)
            (_state(True, 40.0, 0.0, 120.0, seq=2), 0.05),  # next frame ramps further
        ],
    )
    steers = [_action(p, "steer")["params"]["angle_deg"] for p in plans if "steer" in _methods(p)]
    assert steers[0] == pytest.approx(100.0)  # 90 + one 10° step
    assert steers[1] == pytest.approx(110.0)  # + another 10°


@pytest.mark.asyncio
async def test_coupled_steer_points_body_toward_panned_target() -> None:
    """With an inverted pan servo (negative gain), after the camera pans to follow
    a right-side target the body must steer the SAME way (toward it), not opposite."""
    planner = FollowPlanner("d", pan_gain=-0.5, center_deadband_deg=0.0, steer_gain=2.0)
    plans = await _run_script(
        planner,
        [
            (_state(True, 30.0, 0.0, 120.0), 0.05),  # target right → camera pans toward it
            (_state(True, 0.0, 0.0, 120.0), 0.05),  # now centred in frame; body still off-axis
        ],
    )
    # Camera panned right (to <90 on the inverted servo); body steer stays to the
    # right (>90). The pre-fix formula produced <90 here (steered away).
    assert _action(plans[-1], "steer")["params"]["angle_deg"] > 90.0
    planner = FollowPlanner("d", target_distance_cm=60.0, distance_deadband_cm=15.0, speed_kp=1.0)
    close = await _run_script(planner, [(_state(True, 0.0, 0.0, 30.0), 0.05)])
    assert _action(close[-1], "drive")["params"]["speed_pct"] < 0  # back off

    planner2 = FollowPlanner("d", target_distance_cm=60.0, distance_deadband_cm=15.0, speed_kp=1.0)
    hold = await _run_script(planner2, [(_state(True, 0.0, 0.0, 65.0), 0.05)])
    assert _action(hold[-1], "drive")["params"]["speed_pct"] == 0  # within deadband


@pytest.mark.asyncio
async def test_distance_only_update_does_not_recentre_camera() -> None:
    """A repeated vision_seq = a distance-only (ultrasonic) update; the camera must
    not re-integrate the pan on it, or it walks off a stale bearing (runaway)."""
    planner = FollowPlanner(
        "d", pan_gain=-0.5, center_deadband_deg=0.0, drive_burst_s=5.0, camera_steer_comp_gain=0.0
    )
    plans = await _run_script(
        planner,
        [
            (_state(True, 30.0, 0.0, 120.0, seq=1), 0.05),  # fresh frame → pans once
            (_state(True, 30.0, 0.0, 80.0, seq=1), 0.05),  # same seq → no re-centre
        ],
    )
    pans = [_action(p, "pan")["params"]["angle_deg"] for p in plans if "pan" in _methods(p)]
    # Exactly one re-centre (pan 90 - 0.5*30 = 75); the stale-seq update does not move it.
    assert pans == [pytest.approx(75.0)]


@pytest.mark.asyncio
async def test_camera_counter_steers_when_wheels_turn() -> None:
    """When the wheels steer right the body rotates right, sweeping the camera off the
    user.  The pan servo should pre-offset left (opposite to the turn) to compensate."""
    # Inverted pan servo (PicarX default): lower angle = camera right.
    # User right of centre → pan_angle decreases (e.g. 90→75).
    # Wheels steer right (+deflection) → pan_comp = -(-1) * gain * deflection > 0 → effective_pan > pan_angle.
    planner = FollowPlanner(
        "d",
        pan_gain=-0.5,
        center_deadband_deg=0.0,
        steer_gain=2.0,
        max_steer_step_deg=90.0,
        camera_steer_comp_gain=0.5,
    )
    plans = await _run_script(planner, [(_state(True, 30.0, 0.0, 120.0), 0.05)])
    assert plans, "expected a tracking plan"
    track_plan = plans[-1]
    pan_cmd = _action(track_plan, "pan")["params"]["angle_deg"]
    steer_cmd = _action(track_plan, "steer")["params"]["angle_deg"]
    # pan_angle (tracking) = 90 + (-0.5)*30 = 75
    # steer_deflection = steer_cmd - 90 (right turn, > 0)
    # comp = -(-1) * 0.5 * deflection = +0.5 * deflection (increases pan → camera left)
    steer_deflection = steer_cmd - 90.0
    expected_pan = 75.0 + 0.5 * steer_deflection
    assert pan_cmd == pytest.approx(expected_pan)
    assert pan_cmd > 75.0  # compensation pushed pan above the raw tracking angle


@pytest.mark.asyncio
async def test_steer_burst_then_straight_within_drive_burst() -> None:
    """After steer_burst_s elapses mid-burst, wheels return to straight while drive continues."""
    planner = FollowPlanner(
        "d",
        drive_burst_s=0.3,
        steer_burst_s=0.05,
        target_distance_cm=60.0,
        min_drive_speed_pct=35.0,
    )
    # One detection (off-centre → steered drive), then wait long enough for steer
    # window to close but burst to still be running.
    plans = await _run_script(planner, [(_state(True, 20.0, 0.0, 200.0), 0.2)])
    kinds = [p.plan_id.rsplit("-", 1)[0] for p in plans]
    assert "track" in kinds, "expected an initial track plan"
    assert "track-straight" in kinds, "expected wheels-straight mid-burst plan"
    straight = next(p for p in plans if p.plan_id.startswith("track-straight-"))
    assert _action(straight, "steer")["params"]["angle_deg"] == pytest.approx(90.0)
    assert _action(straight, "drive")["params"]["speed_pct"] > 0  # still driving


@pytest.mark.asyncio
async def test_drive_pauses_after_burst_without_fresh_detection() -> None:
    """After a detection's drive burst elapses with no new detection, the motor is
    paused (drive 0) so the robot re-aims rather than running on a stale heading."""
    planner = FollowPlanner(
        "d", drive_burst_s=0.1, target_distance_cm=60.0, min_drive_speed_pct=40.0
    )
    # One detection (far → drives), then ~0.3 s idle with no further detections.
    plans = await _run_script(planner, [(_state(True, 0.0, 0.0, 200.0), 0.3)])
    kinds = [p.plan_id.rsplit("-", 1)[0] for p in plans]
    assert "track" in kinds
    assert kinds[-1] == "hold"  # burst elapsed → paused
    assert _action(plans[-1], "drive")["params"]["speed_pct"] == 0


# --- Searching -----------------------------------------------------------


@pytest.mark.asyncio
async def test_not_visible_sweeps_camera_with_motor_stopped() -> None:
    planner = FollowPlanner(
        "d", search_interval_s=0.0, search_step_deg=10.0, pan_min_deg=20.0, pan_max_deg=160.0
    )
    plans = await _run_script(planner, [(_state(False), 0.3)])

    sweeps = [p for p in plans if p.plan_id.startswith("search-sweep-")]
    assert len(sweeps) >= 2, "the camera should step across several positions"
    for p in sweeps:
        assert set(_methods(p)) == {"pan", "tilt", "stop"}  # camera scans, motor idle
    pans = [_action(p, "pan")["params"]["angle_deg"] for p in sweeps]
    assert len(set(pans)) >= 2  # pan actually moves between steps
    assert max(pans) > 90.0  # rolls past forward


@pytest.mark.asyncio
async def test_search_rolls_pan_and_tilt_together() -> None:
    """The look-around 'roll' moves pan AND tilt (an ellipse), not just pan."""
    planner = FollowPlanner(
        "d",
        search_interval_s=0.0,
        search_step_deg=30.0,
        pan_min_deg=20.0,
        pan_max_deg=160.0,
        search_tilt_offset_deg=15.0,
    )
    plans = await _run_script(planner, [(_state(False), 0.3)])
    sweeps = [p for p in plans if p.plan_id.startswith("search-sweep-")]
    pans = {_action(p, "pan")["params"]["angle_deg"] for p in sweeps}
    tilts = {_action(p, "tilt")["params"]["angle_deg"] for p in sweeps}
    assert len(pans) >= 2  # pan rolls
    assert len(tilts) >= 2  # tilt rolls too — the gaze traces a loop


@pytest.mark.asyncio
async def test_exhausted_sweep_pivots_body() -> None:
    planner = FollowPlanner(
        "d",
        search_interval_s=0.0,
        search_step_deg=90.0,  # 4 steps = one full roll → rotate quickly
        body_rotate_speed_pct=40.0,
        body_rotate_duration_s=5.0,  # stay in the rotate phase for the test
    )
    plans = await _run_script(planner, [(_state(False), 0.8)])

    rotates = [p for p in plans if p.plan_id.startswith("search-rotate-")]
    assert rotates, "a full roll without a target should pivot the body"
    assert set(_methods(rotates[-1])) == {"steer", "drive"}
    assert _action(rotates[-1], "drive")["params"]["speed_pct"] == pytest.approx(40.0)


@pytest.mark.asyncio
async def test_reacquire_switches_back_to_tracking() -> None:
    planner = FollowPlanner("d", search_interval_s=0.0, search_step_deg=10.0)
    plans = await _run_script(
        planner,
        [
            (_state(False), 0.2),  # search
            (_state(True, 10.0, 0.0, 100.0), 0.05),  # reacquired → track
        ],
    )
    kinds = [p.plan_id.rsplit("-", 1)[0] for p in plans]
    assert "search-sweep" in kinds
    assert kinds[-1] == "track"
