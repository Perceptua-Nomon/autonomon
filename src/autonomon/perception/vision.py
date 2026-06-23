"""VisionPerception: person-detection perception for the ``follow-user`` routine.

Polls nomothetic's raw-frame endpoint (``GET /api/camera/frame`` → ``image/jpeg``),
runs a :class:`~autonomon.perception.detector.Detector` on each frame **inside
autonomon** (ADR-004), and emits a ``PerceptionEvent`` describing the target's
bearing and estimated range. nomothetic only serves the raw frame; all detection
and geometry live here.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx

from autonomon.messages import PerceptionEvent
from autonomon.perception.base import PerceptionBase
from autonomon.perception.detector import Detection, Detector

logger = logging.getLogger(__name__)

_MIN_BOX_HEIGHT = 1e-3  # guard against divide-by-zero in the range estimate


class VisionPerception(PerceptionBase):
    """Detects a person in raw camera frames and emits target bearing/range.

    Each poll fetches one JPEG frame, runs the injected ``detector``, keeps the
    highest-confidence person above ``confidence_threshold``, and emits a
    ``PerceptionEvent(sensor_type="vision")`` with::

        {"detected": bool,
         "target_bearing_deg": float | None,   # +ve = target to the right of centre
         "target_distance_cm": float | None,   # rough estimate from box height
         "confidence": float | None}

    When no person clears the threshold, ``detected`` is False and the other
    fields are None. Transient frame/detector errors are absorbed; the loop
    continues (mirroring :class:`~autonomon.perception.perceptron.Perceptron`).

    Parameters
    ----------
    client : httpx.AsyncClient
        Shared device client (base URL, auth, ``verify=False``) per ADR-002.
    device_id : str
        Device identifier stamped on every ``PerceptionEvent``.
    detector : Detector
        Person detector (injected so CI can use a fake; ADR-004).
    frame_endpoint : str
        Path of the raw-frame endpoint. Default ``/api/camera/frame``.
    poll_interval_s : float
        Seconds between frames. Default 0.3 (detection is CPU-heavy on a Pi).
    timeout_s : float
        Per-request wall-clock timeout. Default 2.0 s.
    camera_hfov_deg : float
        Camera horizontal field of view in degrees, used to map a box's
        horizontal offset to a bearing. Default 70.0.
    confidence_threshold : float
        Minimum detection confidence to treat a person as the target. Default 0.5.
    range_ref_distance_cm : float
        Reference distance for the range estimate (see ``range_ref_box_height``).
        Default 150.0.
    range_ref_box_height : float
        Normalised person-box height observed at ``range_ref_distance_cm``. The
        estimate is ``range_ref_distance_cm * range_ref_box_height / box_height``.
        Default 0.5.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        device_id: str,
        detector: Detector,
        frame_endpoint: str = "/api/camera/frame",
        poll_interval_s: float = 0.3,
        timeout_s: float = 2.0,
        camera_hfov_deg: float = 70.0,
        confidence_threshold: float = 0.5,
        range_ref_distance_cm: float = 150.0,
        range_ref_box_height: float = 0.5,
    ) -> None:
        self._client = client
        self._device_id = device_id
        self._detector = detector
        self._frame_endpoint = frame_endpoint
        self._poll_interval_s = poll_interval_s
        self._timeout_s = timeout_s
        self._camera_hfov_deg = camera_hfov_deg
        self._confidence_threshold = confidence_threshold
        self._range_ref_distance_cm = range_ref_distance_cm
        self._range_ref_box_height = range_ref_box_height
        self._stop = asyncio.Event()

    async def run(self, queue_out: asyncio.Queue[PerceptionEvent]) -> None:
        """Poll frames, detect a person, and emit vision PerceptionEvents until stopped."""
        while not self._stop.is_set():
            await self._poll(queue_out)
            await self._interruptible_sleep(self._poll_interval_s)

    async def _poll(self, queue_out: asyncio.Queue[PerceptionEvent]) -> None:
        try:
            resp = await asyncio.wait_for(
                self._client.get(self._frame_endpoint), timeout=self._timeout_s
            )
            resp.raise_for_status()
            frame = resp.content
            # Detection can be CPU-heavy; keep the event loop responsive.
            detections = await asyncio.to_thread(self._detector.detect, frame)
        except asyncio.TimeoutError:
            logger.warning("vision frame poll timed out after %.1f s", self._timeout_s)
            return
        except httpx.RequestError as exc:
            logger.warning("vision frame request error: %s", exc)
            return
        except httpx.HTTPStatusError as exc:
            logger.warning("vision frame HTTP %d", exc.response.status_code)
            return
        except Exception as exc:  # noqa: BLE001 — a detector failure must not kill the layer
            logger.warning("vision detection failed: %s", exc)
            return

        await queue_out.put(self._build_event(self._select(detections)))

    def _select(self, detections: list[Detection]) -> Detection | None:
        """Return the highest-confidence person above the threshold, or None."""
        candidates = [d for d in detections if d.confidence >= self._confidence_threshold]
        if not candidates:
            return None
        return max(candidates, key=lambda d: d.confidence)

    def _build_event(self, target: Detection | None) -> PerceptionEvent:
        if target is None:
            data: dict[str, object] = {
                "detected": False,
                "target_bearing_deg": None,
                "target_distance_cm": None,
                "confidence": None,
            }
        else:
            bearing = (target.cx - 0.5) * self._camera_hfov_deg
            box_h = max(target.h, _MIN_BOX_HEIGHT)
            distance = self._range_ref_distance_cm * self._range_ref_box_height / box_h
            data = {
                "detected": True,
                "target_bearing_deg": bearing,
                "target_distance_cm": distance,
                "confidence": target.confidence,
            }
        return PerceptionEvent(
            timestamp=datetime.now(timezone.utc).isoformat(),
            device_id=self._device_id,
            sensor_type="vision",
            data=data,
        )

    async def _interruptible_sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def stop(self) -> None:
        self._stop.set()
