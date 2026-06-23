"""Tests for Perceptron — configurable single-sensor perception implementation."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from autonomon import PerceptionEvent, Perceptron

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_response(json_body: dict[str, Any]) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = json_body
    resp.raise_for_status = MagicMock()
    return resp


def _mock_client(json_body: dict[str, Any]) -> AsyncMock:
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.return_value = _mock_response(json_body)
    return client


async def _collect_one(perceptron: Perceptron) -> PerceptionEvent:
    """Run the perceptron, collect the first event, then stop it."""
    q: asyncio.Queue[PerceptionEvent] = asyncio.Queue()
    task = asyncio.create_task(perceptron.run(q))
    event = await asyncio.wait_for(q.get(), timeout=1.0)
    await perceptron.stop()
    await task
    return event


# ---------------------------------------------------------------------------
# Named constructors emit correct sensor_type and data shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ultrasonic_emits_distance_cm() -> None:
    client = _mock_client({"distance_cm": 24.7, "timestamp": "2026-01-01T00:00:00Z"})
    p = Perceptron.ultrasonic(client, device_id="nomon-test")

    event = await _collect_one(p)

    assert event.type == "perception_event"
    assert event.sensor_type == "ultrasonic"
    assert event.data["distance_cm"] == pytest.approx(24.7)
    assert event.device_id == "nomon-test"
    client.get.assert_called_with("/api/sensor/ultrasonic")


@pytest.mark.asyncio
async def test_ultrasonic_none_distance_when_out_of_range() -> None:
    client = _mock_client({"distance_cm": None, "timestamp": "2026-01-01T00:00:00Z"})
    p = Perceptron.ultrasonic(client, device_id="nomon-test")

    event = await _collect_one(p)

    assert event.data["distance_cm"] is None


@pytest.mark.asyncio
async def test_grayscale_emits_raw_values() -> None:
    client = _mock_client(
        {
            "channels": [0, 1, 2],
            "values": [485, 580, 30],
            "timestamp": "2026-01-01T00:00:00Z",
        }
    )
    p = Perceptron.grayscale(client, device_id="nomon-test")

    event = await _collect_one(p)

    assert event.sensor_type == "grayscale"
    assert event.data["channels"] == [0, 1, 2]
    assert event.data["values"] == [485, 580, 30]
    client.get.assert_called_with("/api/sensor/grayscale")


@pytest.mark.asyncio
async def test_battery_emits_voltage() -> None:
    client = _mock_client({"voltage_v": 7.4, "timestamp": "2026-01-01T00:00:00Z"})
    p = Perceptron.battery(client, device_id="nomon-test")

    event = await _collect_one(p)

    assert event.sensor_type == "battery"
    assert event.data["voltage_v"] == pytest.approx(7.4)
    client.get.assert_called_with("/api/hat/battery")


# ---------------------------------------------------------------------------
# Custom sensor_type + endpoint + interpreter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_custom_sensor_type_and_interpreter() -> None:
    client = _mock_client({"temp_raw": 2750, "timestamp": "2026-01-01T00:00:00Z"})
    p = Perceptron(
        client,
        device_id="nomon-test",
        sensor_type="temperature",
        endpoint="/api/sensor/temperature",
        interpreter=lambda body: {"celsius": body["temp_raw"] / 100.0},
    )

    event = await _collect_one(p)

    assert event.sensor_type == "temperature"
    assert event.data["celsius"] == pytest.approx(27.5)
    client.get.assert_called_with("/api/sensor/temperature")


@pytest.mark.asyncio
async def test_unknown_sensor_type_falls_back_to_full_body() -> None:
    """No interpreter and no built-in → full response body used as data."""
    client = _mock_client({"foo": 42, "timestamp": "2026-01-01T00:00:00Z"})
    p = Perceptron(
        client,
        device_id="nomon-test",
        sensor_type="unknown",
        endpoint="/api/sensor/unknown",
    )

    event = await _collect_one(p)

    assert event.data["foo"] == 42


# ---------------------------------------------------------------------------
# Error handling — transient failures are logged and the loop continues
# ---------------------------------------------------------------------------


async def _raise_connect_error() -> MagicMock:
    """First poll: raise an httpx.RequestError subclass."""
    raise httpx.ConnectError("connection refused")


async def _raise_http_status() -> MagicMock:
    """First poll: return a response whose raise_for_status() raises 503."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 503
    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "503", request=MagicMock(), response=resp
    )
    return resp


async def _hang() -> MagicMock:
    """First poll: block long enough to trip the per-poll wait_for timeout."""
    await asyncio.sleep(10)  # cancelled by the timeout
    raise AssertionError("unreachable")


async def _malformed_body() -> MagicMock:
    """First poll: a 200 whose body lacks the key the interpreter expects.

    The ultrasonic interpreter reads ``body["distance_cm"]``; a body without it
    raises KeyError inside ``_poll``. The loop must absorb it and keep polling.
    """
    return _mock_response({"timestamp": "t"})


def _fail_first_then(first_poll: Any, good_body: dict[str, Any]) -> Any:
    """Return a client.get side-effect: ``first_poll`` once, then ``good_body``."""
    good = _mock_response(good_body)
    state = {"n": 0}

    async def _side_effect(path: str) -> MagicMock:
        state["n"] += 1
        return await first_poll() if state["n"] == 1 else good

    return _side_effect


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_poll", "timeout_s"),
    [
        pytest.param(_raise_connect_error, 1.0, id="request_error"),
        pytest.param(_raise_http_status, 1.0, id="http_error"),
        pytest.param(_hang, 0.05, id="timeout"),
        pytest.param(_malformed_body, 1.0, id="malformed_body"),
    ],
)
async def test_transient_failure_does_not_stop_loop(first_poll: Any, timeout_s: float) -> None:
    """A transient failure on one poll is absorbed; the next poll still emits."""
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.side_effect = _fail_first_then(first_poll, {"distance_cm": 7.0, "timestamp": "t"})
    p = Perceptron.ultrasonic(
        client, device_id="nomon-test", poll_interval_s=0.01, timeout_s=timeout_s
    )

    event = await _collect_one(p)

    assert event.data["distance_cm"] == pytest.approx(7.0)


# ---------------------------------------------------------------------------
# Stop behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_exits_run_cleanly() -> None:
    client = _mock_client({"distance_cm": 1.0, "timestamp": "t"})
    p = Perceptron.ultrasonic(client, device_id="nomon-test", poll_interval_s=60.0)
    q: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(p.run(q))
    await asyncio.sleep(0.05)
    await p.stop()
    await asyncio.wait_for(task, timeout=1.0)
    assert task.done()


# ---------------------------------------------------------------------------
# Poll interval and configurable parameters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_battery_default_poll_interval_is_30s() -> None:
    client = AsyncMock(spec=httpx.AsyncClient)
    p = Perceptron.battery(client, device_id="nomon-test")
    assert p._poll_interval_s == 30.0


def test_custom_poll_interval_respected() -> None:
    client = AsyncMock(spec=httpx.AsyncClient)
    p = Perceptron.ultrasonic(client, device_id="nomon-test", poll_interval_s=0.5)
    assert p._poll_interval_s == 0.5
