"""Person detectors for the vision perception layer.

A :class:`Detector` turns a raw JPEG frame into a list of :class:`Detection`
boxes in **normalised** coordinates (all in ``[0, 1]``), so downstream layers need
no image dimensions. Two implementations:

* :class:`YoloOnnxDetector` — runs a YOLOv8n ONNX model via onnxruntime (person
  class only). Heavy deps (``onnxruntime``, ``numpy``, ``pillow`` — the ``vision``
  extra) are **lazy-imported** so importing this module never requires them; the
  detector raises a clear error only when first used without them.
* :class:`FakeDetector` — returns scripted detections; used by tests/CI and by the
  ``NOMON_VISION_FAKE_DETECTIONS`` dev hook. No heavy deps.

Per ADR-004 the detector is an *autonomon* dependency: nomothetic only serves the
raw frame (``GET /api/camera/frame``); all interpretation lives here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass
class Detection:
    """One detected person, in normalised image coordinates (all in ``[0, 1]``).

    Parameters
    ----------
    cx, cy : float
        Box centre, as a fraction of image width/height (``0`` left/top,
        ``1`` right/bottom).
    w, h : float
        Box width/height, as a fraction of image width/height.
    confidence : float
        Detector confidence in ``[0, 1]``.
    """

    cx: float
    cy: float
    w: float
    h: float
    confidence: float


@runtime_checkable
class Detector(Protocol):
    """Turns a raw JPEG frame into normalised person detections."""

    def detect(self, frame_jpeg: bytes) -> list[Detection]:
        """Return the person detections in ``frame_jpeg`` (may be empty)."""
        ...


class FakeDetector:
    """A detector that returns a fixed list of detections, ignoring the frame.

    Used by tests/CI (no heavy deps) and by the ``NOMON_VISION_FAKE_DETECTIONS``
    dev hook so the ``follow-user`` pipeline can be exercised end-to-end without a
    model.

    Parameters
    ----------
    detections : list of Detection, optional
        Returned verbatim from every :meth:`detect`. Defaults to empty (no target).
    """

    def __init__(self, detections: list[Detection] | None = None) -> None:
        self._detections = list(detections or [])

    def detect(self, frame_jpeg: bytes) -> list[Detection]:
        return list(self._detections)

    @classmethod
    def from_json(cls, payload: str) -> FakeDetector:
        """Build from a JSON array of ``{cx, cy, w, h, confidence}`` objects."""
        rows = json.loads(payload)
        return cls([Detection(**row) for row in rows])


# Lazy-import helpers for the optional vision stack ---------------------------


def _require_numpy() -> Any:
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise RuntimeError(
            "numpy is required for YoloOnnxDetector; install the 'vision' extra "
            "(pip install 'autonomon[vision]')"
        ) from exc
    return np


def _require_pil():
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise RuntimeError(
            "pillow is required for YoloOnnxDetector; install the 'vision' extra "
            "(pip install 'autonomon[vision]')"
        ) from exc
    return Image


def _require_cv2() -> Any:
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise RuntimeError(
            "opencv is required for OpenCvHogDetector; install the 'vision-opencv' "
            "extra (pip install 'autonomon[vision-opencv]')"
        ) from exc
    return cv2


class YoloOnnxDetector:
    """Person detector backed by a YOLOv8n ONNX model run via onnxruntime.

    The model is loaded lazily on first :meth:`detect`, so constructing this (e.g.
    in the ``follow-user`` factory) does not require the model file or onnxruntime
    to be present — only running it does.

    Parameters
    ----------
    model_path : str
        Filesystem path to a YOLOv8n ONNX export. Provide a pre-exported model
        (see ``scripts/fetch_model.sh``); ``ultralytics`` is not a runtime dep.
    input_size : int
        Square model input edge in pixels. Default 640 (YOLOv8 default).
    score_threshold : float
        Minimum confidence kept during post-processing. Default 0.25; the routine
        applies its own (higher) ``confidence_threshold`` on top of this.
    iou_threshold : float
        IoU threshold for non-max suppression. Default 0.45.
    """

    PERSON_CLASS_ID = 0  # COCO "person"

    def __init__(
        self,
        model_path: str,
        *,
        input_size: int = 640,
        score_threshold: float = 0.25,
        iou_threshold: float = 0.45,
    ) -> None:
        self._model_path = model_path
        self._input_size = input_size
        self._score_threshold = score_threshold
        self._iou_threshold = iou_threshold
        self._session: object | None = None
        self._input_name: str | None = None

    def _ensure_session(self) -> None:
        if self._session is not None:
            return
        if not self._model_path:
            raise RuntimeError(
                "no vision model configured; set 'model_path' or NOMON_VISION_MODEL_PATH"
            )
        try:
            import onnxruntime as ort
        except ImportError as exc:  # pragma: no cover - only without the extra
            raise RuntimeError(
                "onnxruntime is required for YoloOnnxDetector; install the 'vision' "
                "extra (pip install 'autonomon[vision]')"
            ) from exc
        session = ort.InferenceSession(self._model_path, providers=["CPUExecutionProvider"])
        self._session = session
        self._input_name = session.get_inputs()[0].name

    def detect(self, frame_jpeg: bytes) -> list[Detection]:
        np = _require_numpy()
        self._ensure_session()
        tensor = self._preprocess(frame_jpeg, np)
        assert self._session is not None and self._input_name is not None
        outputs = self._session.run(None, {self._input_name: tensor})  # type: ignore[attr-defined]
        return self._postprocess(outputs[0], np)

    def _preprocess(self, frame_jpeg: bytes, np: Any) -> Any:
        import io

        Image = _require_pil()
        img = (
            Image.open(io.BytesIO(frame_jpeg))
            .convert("RGB")
            .resize((self._input_size, self._input_size))
        )
        arr = np.asarray(img, dtype=np.float32) / 255.0  # HWC
        arr = arr.transpose(2, 0, 1)[np.newaxis, ...]  # 1CHW
        return np.ascontiguousarray(arr)

    def _postprocess(self, output: Any, np: Any) -> list[Detection]:
        # YOLOv8 output is (1, 4+num_classes, num_boxes); transpose to per-box rows.
        preds = np.squeeze(output, axis=0).transpose(1, 0)  # (num_boxes, 4+num_classes)
        person_scores = preds[:, 4 + self.PERSON_CLASS_ID]
        keep = person_scores >= self._score_threshold
        preds, person_scores = preds[keep], person_scores[keep]
        if preds.shape[0] == 0:
            return []
        # Boxes are (cx, cy, w, h) in input-pixel space; normalise to [0, 1].
        boxes = preds[:, :4] / float(self._input_size)
        order = self._nms(boxes, person_scores, np)
        return [
            Detection(
                cx=float(boxes[i, 0]),
                cy=float(boxes[i, 1]),
                w=float(boxes[i, 2]),
                h=float(boxes[i, 3]),
                confidence=float(person_scores[i]),
            )
            for i in order
        ]

    def _nms(self, boxes: Any, scores: Any, np: Any) -> list[int]:
        # boxes are (cx, cy, w, h) normalised; convert to corners for IoU.
        x1 = boxes[:, 0] - boxes[:, 2] / 2
        y1 = boxes[:, 1] - boxes[:, 3] / 2
        x2 = boxes[:, 0] + boxes[:, 2] / 2
        y2 = boxes[:, 1] + boxes[:, 3] / 2
        areas = (x2 - x1) * (y2 - y1)
        order = scores.argsort()[::-1]
        keep: list[int] = []
        while order.size > 0:
            i = int(order[0])
            keep.append(i)
            xx1 = np.maximum(x1[i], x1[order[1:]])
            yy1 = np.maximum(y1[i], y1[order[1:]])
            xx2 = np.minimum(x2[i], x2[order[1:]])
            yy2 = np.minimum(y2[i], y2[order[1:]])
            inter = np.clip(xx2 - xx1, 0, None) * np.clip(yy2 - yy1, 0, None)
            iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-9)
            order = order[1:][iou < self._iou_threshold]
        return keep


class OpenCvHogDetector:
    """Person detector using OpenCV's built-in HOG + linear-SVM people detector.

    Unlike :class:`YoloOnnxDetector`, this needs **no model file** — the trained
    SVM ships inside OpenCV — so it deploys without downloading any weights. It is
    lighter to install (``opencv-python-headless`` only) and quicker to bring up,
    at some cost in accuracy. ``detectMultiScale`` is CPU-heavy, so frames are
    decoded and downscaled to ``detect_width`` before detection (a Pi Zero 2W
    cannot run HOG on a full-resolution frame at any useful rate).

    OpenCV/numpy are **lazy-imported** (the ``vision-opencv`` extra); importing
    this module never requires them. The HOG descriptor is built lazily on first
    :meth:`detect`.

    Parameters
    ----------
    detect_width : int
        Frame is downscaled to this width (keeping aspect) before detection.
        Default 320. Smaller is faster but misses smaller/farther people.
    hit_threshold : float
        SVM decision threshold passed to ``detectMultiScale``. Default 0.0.
        Higher rejects weak hits.
    win_stride : int
        Sliding-window stride in pixels (square). Default 8. Larger is faster,
        coarser.
    scale : float
        Image-pyramid scale factor. Default 1.05.
    confidence_scale : float
        Multiplier applied to each SVM weight before it is squashed to a
        ``[0, 1]`` confidence via a logistic. HOG weights are unbounded decision
        values, **not** probabilities; the squash only needs to be monotonic so
        the routine can pick the strongest detection and apply its own
        ``confidence_threshold``. Default 1.0.
    """

    def __init__(
        self,
        *,
        detect_width: int = 320,
        hit_threshold: float = 0.0,
        win_stride: int = 8,
        scale: float = 1.05,
        confidence_scale: float = 1.0,
    ) -> None:
        self._detect_width = detect_width
        self._hit_threshold = hit_threshold
        self._win_stride = win_stride
        self._scale = scale
        self._confidence_scale = confidence_scale
        self._hog: Any | None = None

    def _ensure_hog(self) -> None:
        if self._hog is not None:
            return
        cv2 = _require_cv2()
        hog = cv2.HOGDescriptor()
        hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
        self._hog = hog

    def detect(self, frame_jpeg: bytes) -> list[Detection]:
        np = _require_numpy()
        cv2 = _require_cv2()
        self._ensure_hog()
        buf = np.frombuffer(frame_jpeg, dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)  # BGR; None if undecodable
        if img is None:
            return []
        h0, w0 = img.shape[:2]
        if w0 > self._detect_width:
            factor = self._detect_width / float(w0)
            img = cv2.resize(img, (self._detect_width, max(1, int(round(h0 * factor)))))
        sh, sw = img.shape[:2]
        assert self._hog is not None
        rects, weights = self._hog.detectMultiScale(
            img,
            hitThreshold=self._hit_threshold,
            winStride=(self._win_stride, self._win_stride),
            scale=self._scale,
        )
        detections: list[Detection] = []
        for (x, y, w, h), weight in zip(rects, np.asarray(weights).reshape(-1)):
            conf = 1.0 / (1.0 + float(np.exp(-float(weight) * self._confidence_scale)))
            detections.append(
                Detection(
                    cx=(x + w / 2.0) / sw,
                    cy=(y + h / 2.0) / sh,
                    w=w / sw,
                    h=h / sh,
                    confidence=conf,
                )
            )
        return detections
