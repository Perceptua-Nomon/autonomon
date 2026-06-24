"""End-to-end autonomy test: full pipeline driven by a mock nomothetic device.

Wires the real Pipeline with all four concrete layers — Perceptron,
ObstacleWorldModel, AvoidancePlanner, VehicleAction — against a single mock
httpx.AsyncClient. A near ultrasonic reading should propagate
Perception -> WorldModel -> Planner -> Action and result in an avoidance
command (a motor stop) being POSTed to the device.

No Pi, no network.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from autonomon import ActionResult, Pipeline, VehicleAction, get_routine


def _response(json_body: dict[str, Any]) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = json_body
    resp.raise_for_status = MagicMock()
    return resp


def _device_client(distance_cm: float, gray_values: list[int] | None = None) -> AsyncMock:
    """Mock device: ultrasonic + grayscale GETs return the given readings; POSTs 200.

    ``gray_values`` are raw grayscale ADC counts; they default to a reflective floor
    (``[500, 500, 500]``, no cliff) so obstacle/cruise tests exercise the ultrasonic
    path alone. Cliff detection is on by default in the ``explore`` factory, so the
    raw grayscale endpoint must return a well-formed body.
    """
    client = AsyncMock(spec=httpx.AsyncClient)
    gray = gray_values if gray_values is not None else [500, 500, 500]

    async def _get(path: str) -> MagicMock:
        if path == "/api/sensor/ultrasonic":
            return _response({"distance_cm": distance_cm, "timestamp": "t"})
        if path == "/api/sensor/grayscale":
            return _response({"channels": [0, 1, 2], "values": gray, "timestamp": "t"})
        return _response({"timestamp": "t"})

    client.get.side_effect = _get
    client.post.return_value = _response({"timestamp": "t"})
    return client


def _build_pipeline(client: AsyncMock, results: asyncio.Queue) -> Pipeline:  # type: ignore[type-arg]
    """Build the production ``explore`` routine via the registry.

    The factory wires the same four layers the test used to assemble by hand. We
    then attach the test's ``results`` queue to the action layer (the factory
    builds it without a telemetry sink) so the driver can wait for the first
    ActionResult.
    """
    pipeline = get_routine("explore")(
        client,
        "nomon-test",
        {"obstacle_threshold_cm": 20.0},
    )
    action_impl = cast(VehicleAction, pipeline._slots["action"].impl)  # type: ignore[union-attr]
    action_impl._results = results
    return pipeline


async def _run_until_first_result(pipeline: Pipeline, results: asyncio.Queue) -> None:  # type: ignore[type-arg]
    """Run the pipeline until the first ActionResult, then always stop it.

    The ``finally`` guarantees the pipeline is stopped even if no result arrives
    within the timeout, so a missing command surfaces as a failed assertion in
    the caller rather than a hung test.
    """

    async def _driver() -> None:
        try:
            await asyncio.wait_for(results.get(), timeout=2.0)
        finally:
            await pipeline.stop()

    await asyncio.gather(pipeline.run(), _driver(), return_exceptions=True)


@pytest.mark.asyncio
async def test_near_obstacle_triggers_avoidance_commands() -> None:
    client = _device_client(distance_cm=10.0)  # well below the 20 cm threshold
    results: asyncio.Queue[ActionResult] = asyncio.Queue()

    await _run_until_first_result(_build_pipeline(client, results), results)

    # The avoid plan is stop -> reverse -> steer; assert the device was commanded.
    posted = [c.args[0] for c in client.post.await_args_list]
    assert "/api/hat/motor/stop" in posted
    assert "/api/drive" in posted


@pytest.mark.asyncio
async def test_cliff_triggers_avoidance_with_clear_path() -> None:
    # The path ahead is clear (ultrasonic far), but a downward sensor reads low
    # (30 ADC) — an edge / no surface. Cliff detection (on by default) must still
    # command an avoidance stop. Regression guard for the sensor-polarity bug,
    # where holding the robot over an edge (a LOW reading) produced no stop.
    client = _device_client(distance_cm=200.0, gray_values=[500, 30, 500])
    results: asyncio.Queue[ActionResult] = asyncio.Queue()

    await _run_until_first_result(_build_pipeline(client, results), results)

    posted = [c.args[0] for c in client.post.await_args_list]
    assert "/api/hat/motor/stop" in posted


@pytest.mark.asyncio
async def test_clear_path_cruises_forward() -> None:
    client = _device_client(distance_cm=200.0)  # far away → no obstacle
    results: asyncio.Queue[ActionResult] = asyncio.Queue()

    await _run_until_first_result(_build_pipeline(client, results), results)

    posted = [c.args[0] for c in client.post.await_args_list]
    bodies = [c.kwargs.get("json") for c in client.post.await_args_list]
    # Cruise plan is steer-straight -> drive-forward.
    assert "/api/drive" in posted
    drive_bodies = [b for b in bodies if b and "speed_pct" in b]
    assert any(b["speed_pct"] > 0 for b in drive_bodies)


def _vision_client() -> AsyncMock:
    """Mock device for ``follow-user``: a raw JPEG frame on GET, 200 on every POST."""
    client = AsyncMock(spec=httpx.AsyncClient)

    async def _get(path: str) -> MagicMock:
        resp = MagicMock(spec=httpx.Response)
        resp.content = b"\xff\xd8jpeg"
        resp.json.return_value = {"timestamp": "t"}
        resp.raise_for_status = MagicMock()
        return resp

    client.get.side_effect = _get
    client.post.return_value = _response({"timestamp": "t"})
    return client


def _build_follow_pipeline(
    client: AsyncMock, results: asyncio.Queue, params: dict[str, Any] | None = None  # type: ignore[type-arg]
) -> Pipeline:
    pipeline = get_routine("follow-user")(client, "nomon-test", params or {})
    action_impl = cast(VehicleAction, pipeline._slots["action"].impl)  # type: ignore[union-attr]
    action_impl._results = results
    return pipeline


@pytest.mark.asyncio
async def test_follow_user_tracks_offcentre_person(monkeypatch: pytest.MonkeyPatch) -> None:
    # A person to the right of frame centre (cx=0.8), far away (small box height).
    monkeypatch.setenv(
        "NOMON_VISION_FAKE_DETECTIONS",
        '[{"cx": 0.8, "cy": 0.5, "w": 0.2, "h": 0.2, "confidence": 0.9}]',
    )
    client = _vision_client()
    results: asyncio.Queue[ActionResult] = asyncio.Queue()

    await _run_until_first_result(_build_follow_pipeline(client, results), results)

    posted = [c.args[0] for c in client.post.await_args_list]
    assert "/api/camera/pan" in posted  # camera tracks toward the person
    assert "/api/drive" in posted  # and the robot closes distance


@pytest.mark.asyncio
async def test_follow_user_searches_when_no_person(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOMON_VISION_FAKE_DETECTIONS", "[]")  # no detections
    client = _vision_client()
    results: asyncio.Queue[ActionResult] = asyncio.Queue()

    await _run_until_first_result(_build_follow_pipeline(client, results), results)

    posted = [c.args[0] for c in client.post.await_args_list]
    assert "/api/camera/pan" in posted  # camera sweeps to look around
    assert "/api/hat/motor/stop" in posted  # motor idle while scanning
    assert "/api/drive" not in posted  # not driving during a (non-exhausted) sweep


@pytest.mark.asyncio
async def test_pipeline_shuts_down_cleanly() -> None:
    client = _device_client(distance_cm=10.0)
    results: asyncio.Queue[ActionResult] = asyncio.Queue()
    pipeline = _build_pipeline(client, results)

    await _run_until_first_result(pipeline, results)

    for slot in pipeline._slots.values():
        assert all(t.done() for t in slot.tasks)
