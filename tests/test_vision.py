"""Tests for VisionPerception — frame polling, detection, bearing/range geometry."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from autonomon import Detection, FakeDetector, PerceptionEvent, VisionPerception


def _mock_response(content: bytes = b"\xff\xd8jpeg") -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.content = content
    resp.raise_for_status = MagicMock()
    return resp


def _mock_client(response: MagicMock | None = None) -> AsyncMock:
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.return_value = response or _mock_response()
    return client


async def _collect_one(vision: VisionPerception) -> PerceptionEvent:
    q: asyncio.Queue[PerceptionEvent] = asyncio.Queue()
    task = asyncio.create_task(vision.run(q))
    event = await asyncio.wait_for(q.get(), timeout=1.0)
    await vision.stop()
    await task
    return event


@pytest.mark.asyncio
async def test_detected_person_yields_bearing_and_range() -> None:
    client = _mock_client()
    det = FakeDetector([Detection(cx=0.75, cy=0.5, w=0.2, h=0.5, confidence=0.9)])
    vision = VisionPerception(
        client,
        "nomon-test",
        det,
        camera_hfov_deg=70.0,
        range_ref_distance_cm=150.0,
        range_ref_box_height=0.5,
    )

    event = await _collect_one(vision)

    assert event.sensor_type == "vision"
    assert event.data["detected"] is True
    # bearing = (cx - 0.5) * hfov = 0.25 * 70 = 17.5 (target to the right)
    assert event.data["target_bearing_deg"] == pytest.approx(17.5)
    # distance = ref_distance * ref_box_height / h = 150 * 0.5 / 0.5 = 150
    assert event.data["target_distance_cm"] == pytest.approx(150.0)
    assert event.data["confidence"] == 0.9
    client.get.assert_called_with("/api/camera/frame")


@pytest.mark.asyncio
async def test_detected_person_below_centre_yields_positive_vertical_bearing() -> None:
    client = _mock_client()
    # cy = 0.75 → below frame centre; vbearing = (0.75 - 0.5) * 40 = +10.
    det = FakeDetector([Detection(cx=0.5, cy=0.75, w=0.2, h=0.5, confidence=0.9)])
    vision = VisionPerception(client, "nomon-test", det, camera_vfov_deg=40.0)

    event = await _collect_one(vision)

    assert event.data["target_vertical_bearing_deg"] == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_detected_person_above_centre_yields_negative_vertical_bearing() -> None:
    det = FakeDetector([Detection(cx=0.5, cy=0.25, w=0.2, h=0.5, confidence=0.9)])
    vision = VisionPerception(_mock_client(), "nomon-test", det, camera_vfov_deg=40.0)
    event = await _collect_one(vision)
    # cy = 0.25 → above centre; vbearing = (0.25 - 0.5) * 40 = -10.
    assert event.data["target_vertical_bearing_deg"] == pytest.approx(-10.0)


@pytest.mark.asyncio
async def test_no_person_yields_not_detected() -> None:
    vision = VisionPerception(_mock_client(), "nomon-test", FakeDetector([]))
    event = await _collect_one(vision)
    assert event.data["detected"] is False
    assert event.data["target_bearing_deg"] is None
    assert event.data["target_vertical_bearing_deg"] is None
    assert event.data["target_distance_cm"] is None
    assert event.data["confidence"] is None


@pytest.mark.asyncio
async def test_low_confidence_detection_is_filtered() -> None:
    det = FakeDetector([Detection(0.5, 0.5, 0.2, 0.5, 0.2)])
    vision = VisionPerception(_mock_client(), "nomon-test", det, confidence_threshold=0.5)
    event = await _collect_one(vision)
    assert event.data["detected"] is False


@pytest.mark.asyncio
async def test_highest_confidence_person_selected() -> None:
    det = FakeDetector(
        [
            Detection(0.2, 0.5, 0.2, 0.5, 0.6),  # left, lower conf
            Detection(0.8, 0.5, 0.2, 0.5, 0.95),  # right, higher conf
        ]
    )
    vision = VisionPerception(_mock_client(), "nomon-test", det, camera_hfov_deg=70.0)
    event = await _collect_one(vision)
    assert event.data["confidence"] == 0.95
    assert event.data["target_bearing_deg"] == pytest.approx((0.8 - 0.5) * 70.0)


@pytest.mark.asyncio
async def test_transient_frame_error_is_absorbed_then_emits() -> None:
    client = AsyncMock(spec=httpx.AsyncClient)
    state = {"n": 0}
    good = _mock_response()

    async def _get(path: str) -> MagicMock:
        state["n"] += 1
        if state["n"] == 1:
            raise httpx.ConnectError("camera down")
        return good

    client.get.side_effect = _get
    det = FakeDetector([Detection(0.5, 0.5, 0.2, 0.5, 0.9)])
    vision = VisionPerception(client, "nomon-test", det, poll_interval_s=0.01)

    event = await _collect_one(vision)
    assert event.data["detected"] is True


@pytest.mark.asyncio
async def test_detector_exception_does_not_kill_layer() -> None:
    class _BoomDetector:
        def __init__(self) -> None:
            self.calls = 0

        def detect(self, frame_jpeg: bytes) -> list[Detection]:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("inference exploded")
            return [Detection(0.5, 0.5, 0.2, 0.5, 0.9)]

    vision = VisionPerception(_mock_client(), "nomon-test", _BoomDetector(), poll_interval_s=0.01)
    event = await _collect_one(vision)
    assert event.data["detected"] is True  # recovered on the next poll


@pytest.mark.asyncio
async def test_stop_exits_run_cleanly() -> None:
    vision = VisionPerception(_mock_client(), "nomon-test", FakeDetector([]), poll_interval_s=60.0)
    q: asyncio.Queue[Any] = asyncio.Queue()
    task = asyncio.create_task(vision.run(q))
    await asyncio.sleep(0.05)
    await vision.stop()
    await asyncio.wait_for(task, timeout=1.0)
    assert task.done()
