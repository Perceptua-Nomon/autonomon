"""Tests for VehicleAction — endpoint mapping, results, and error handling."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from autonomon import ActionPlan, VehicleAction


def _ok_response(json_body: dict[str, Any] | None = None) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = json_body or {}
    resp.raise_for_status = MagicMock()
    return resp


def _mock_client() -> AsyncMock:
    client = AsyncMock(spec=httpx.AsyncClient)
    client.post.return_value = _ok_response()
    return client


def _plan(actions: list[dict[str, Any]], plan_id: str = "p1") -> dict[str, Any]:
    return ActionPlan(timestamp="t", device_id="d", plan_id=plan_id, actions=actions).to_dict()


async def _run_plan(
    action: VehicleAction, plan: dict[str, Any], results: asyncio.Queue, expect: int
) -> list[dict[str, Any]]:
    """Feed one plan, drain ``expect`` results, then stop."""
    q_in: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    task = asyncio.create_task(action.run(q_in))
    await q_in.put(plan)
    out = [await asyncio.wait_for(results.get(), timeout=1.0) for _ in range(expect)]
    await action.stop()
    await task
    return out


@pytest.mark.asyncio
async def test_drive_maps_to_api_drive() -> None:
    client = _mock_client()
    results: asyncio.Queue = asyncio.Queue()
    action = VehicleAction(client, device_id="nomon-test", ttl_ms=500, results=results)

    out = await _run_plan(
        action, _plan([{"method": "drive", "params": {"speed_pct": 30}, "priority": 0}]), results, 1
    )

    client.post.assert_awaited_once_with("/api/drive", json={"speed_pct": 30, "ttl_ms": 500})
    assert out[0]["success"] is True
    assert out[0]["type"] == "action_result"


@pytest.mark.asyncio
async def test_steer_maps_to_api_steer() -> None:
    client = _mock_client()
    results: asyncio.Queue = asyncio.Queue()
    action = VehicleAction(client, device_id="nomon-test", ttl_ms=300, results=results)

    await _run_plan(
        action,
        _plan([{"method": "steer", "params": {"angle_deg": 135}, "priority": 0}]),
        results,
        1,
    )

    client.post.assert_awaited_once_with("/api/steer", json={"angle_deg": 135, "ttl_ms": 300})


@pytest.mark.asyncio
async def test_stop_maps_to_motor_stop_with_no_body() -> None:
    client = _mock_client()
    results: asyncio.Queue = asyncio.Queue()
    action = VehicleAction(client, device_id="nomon-test", results=results)

    await _run_plan(action, _plan([{"method": "stop", "params": {}, "priority": 0}]), results, 1)

    client.post.assert_awaited_once_with("/api/hat/motor/stop", json=None)


@pytest.mark.asyncio
async def test_actions_execute_in_priority_order() -> None:
    client = _mock_client()
    results: asyncio.Queue = asyncio.Queue()
    action = VehicleAction(client, device_id="nomon-test", results=results)

    # Deliberately out of order; stop(0) should run before drive(1) before steer(2).
    plan = _plan(
        [
            {"method": "steer", "params": {"angle_deg": 135}, "priority": 2},
            {"method": "stop", "params": {}, "priority": 0},
            {"method": "drive", "params": {"speed_pct": -30}, "priority": 1},
        ]
    )
    await _run_plan(action, plan, results, 3)

    called_endpoints = [c.args[0] for c in client.post.await_args_list]
    assert called_endpoints == ["/api/hat/motor/stop", "/api/drive", "/api/steer"]


@pytest.mark.asyncio
async def test_unknown_method_yields_failure_result_no_post() -> None:
    client = _mock_client()
    results: asyncio.Queue = asyncio.Queue()
    action = VehicleAction(client, device_id="nomon-test", results=results)

    out = await _run_plan(
        action, _plan([{"method": "teleport", "params": {}, "priority": 0}]), results, 1
    )

    assert out[0]["success"] is False
    assert "unknown method" in out[0]["error"]
    client.post.assert_not_called()


@pytest.mark.asyncio
async def test_known_method_missing_param_reported_not_dropped() -> None:
    """A 'drive' with no speed_pct must report a missing-param error, not 'unknown method'."""
    client = _mock_client()
    results: asyncio.Queue = asyncio.Queue()
    action = VehicleAction(client, device_id="nomon-test", results=results)

    out = await _run_plan(
        action, _plan([{"method": "drive", "params": {}, "priority": 0}]), results, 1
    )

    assert out[0]["success"] is False
    assert "missing param" in out[0]["error"]
    assert "speed_pct" in out[0]["error"]
    assert "unknown method" not in out[0]["error"]
    client.post.assert_not_called()


@pytest.mark.asyncio
async def test_http_error_recorded_and_loop_continues() -> None:
    client = AsyncMock(spec=httpx.AsyncClient)
    err_resp = MagicMock(spec=httpx.Response)
    err_resp.status_code = 503
    err_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "503", request=MagicMock(), response=err_resp
    )
    client.post.return_value = err_resp

    results: asyncio.Queue = asyncio.Queue()
    action = VehicleAction(client, device_id="nomon-test", results=results)

    out = await _run_plan(
        action, _plan([{"method": "drive", "params": {"speed_pct": 30}, "priority": 0}]), results, 1
    )

    assert out[0]["success"] is False
    assert "HTTP 503" in out[0]["error"]


@pytest.mark.asyncio
async def test_request_error_recorded() -> None:
    client = AsyncMock(spec=httpx.AsyncClient)
    client.post.side_effect = httpx.ConnectError("connection refused")

    results: asyncio.Queue = asyncio.Queue()
    action = VehicleAction(client, device_id="nomon-test", results=results)

    out = await _run_plan(
        action, _plan([{"method": "drive", "params": {"speed_pct": 30}, "priority": 0}]), results, 1
    )

    assert out[0]["success"] is False
    assert "connection refused" in out[0]["error"]


@pytest.mark.asyncio
async def test_results_queue_optional() -> None:
    """Without a results queue, execution still works (results are logged only)."""
    client = _mock_client()
    action = VehicleAction(client, device_id="nomon-test")
    q_in: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(action.run(q_in))
    await q_in.put(_plan([{"method": "stop", "params": {}, "priority": 0}]))
    # Give it time to process, then assert the POST happened.
    for _ in range(50):
        if client.post.await_count >= 1:
            break
        await asyncio.sleep(0.01)
    await action.stop()
    await task
    client.post.assert_awaited_once_with("/api/hat/motor/stop", json=None)
