"""The ``follow-user`` routine: vision-based person following.

The proof that the routine registry generalises beyond wiring (ADR-003): it reuses
``VehicleAction`` unchanged but introduces three net-new layers — a vision
perception layer (person detection in autonomon, ADR-004), a target world model,
and a pursuit planner::

    VisionPerception -> TargetWorldModel -> PursuitPlanner -> VehicleAction

The detector is chosen at build time by *kind* — ``detector`` param or
``NOMON_VISION_DETECTOR`` env var (default ``yolo-onnx``):

* ``yolo-onnx`` — :class:`YoloOnnxDetector` over a YOLOv8n ONNX model
  (``model_path`` param or ``NOMON_VISION_MODEL_PATH``). Most accurate; needs the
  ``vision`` extra and a downloaded model.
* ``opencv-dnn`` — :class:`OpenCvDnnDetector`, a MobileNet-SSD via ``cv2.dnn``
  (``model_path`` = caffemodel, ``model_config`` = prototxt, or the
  ``NOMON_VISION_MODEL_PATH``/``NOMON_VISION_MODEL_CONFIG`` env vars). Robust and
  light: only a ~23 MB model on top of the ``vision-opencv`` extra.
* ``opencv-hog`` — :class:`OpenCvHogDetector`, OpenCV's built-in HOG+SVM people
  detector. **No model file**, lightest install; brittle (architectural edges fool
  it), so it is a last resort rather than the default.
* ``fake`` — :class:`FakeDetector` returning no detections.

Setting ``NOMON_VISION_FAKE_DETECTIONS`` to a JSON array of detections forces a
scripted :class:`FakeDetector` regardless of kind — the dev/CI hook for exercising
the pipeline without any detector backend.

These are deploy/runtime concerns; autonomon's CLI layers them in from its own env
file (``/etc/autonomon/autonomon.env``), so nomothetic never carries them
(ADR-004/005).
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from autonomon.action.vehicle import VehicleAction
from autonomon.perception.detector import (
    Detector,
    FakeDetector,
    OpenCvDnnDetector,
    OpenCvHogDetector,
    YoloOnnxDetector,
)
from autonomon.perception.vision import VisionPerception
from autonomon.pipeline import Pipeline
from autonomon.planning.pursuit import PursuitPlanner
from autonomon.world_model.target import TargetWorldModel

_DEFAULT_TARGET_DISTANCE_CM = 80.0
_DEFAULT_MAX_SPEED_PCT = 60.0
_DEFAULT_CONFIDENCE_THRESHOLD = 0.5
_DEFAULT_CAMERA_HFOV_DEG = 70.0
_DEFAULT_LOST_TARGET_TIMEOUT_S = 1.5
_DEFAULT_DETECTOR = "yolo-onnx"

# Env hooks (deploy/runtime concerns, not behaviour params).
_ENV_DETECTOR = "NOMON_VISION_DETECTOR"
_ENV_MODEL_PATH = "NOMON_VISION_MODEL_PATH"
_ENV_MODEL_CONFIG = "NOMON_VISION_MODEL_CONFIG"
_ENV_FAKE_DETECTIONS = "NOMON_VISION_FAKE_DETECTIONS"

FOLLOW_USER_PARAMS_SCHEMA: dict[str, dict[str, Any]] = {
    "target_distance_cm": {
        "type": "number",
        "description": "Standoff distance (cm) to hold from the followed person.",
        "default": _DEFAULT_TARGET_DISTANCE_CM,
    },
    "max_speed_pct": {
        "type": "number",
        "description": "Maximum drive speed magnitude (0–100) while pursuing.",
        "default": _DEFAULT_MAX_SPEED_PCT,
    },
    "confidence_threshold": {
        "type": "number",
        "description": "Minimum detector confidence (0–1) to treat a person as the target.",
        "default": _DEFAULT_CONFIDENCE_THRESHOLD,
    },
    "camera_hfov_deg": {
        "type": "number",
        "description": "Camera horizontal field of view (deg), used to map box offset to bearing.",
        "default": _DEFAULT_CAMERA_HFOV_DEG,
    },
    "lost_target_timeout_s": {
        "type": "number",
        "description": "Seconds the target is held visible after the last detection (dropout bridge).",
        "default": _DEFAULT_LOST_TARGET_TIMEOUT_S,
    },
    "detector": {
        "type": "string",
        "description": (
            "Detector backend: 'yolo-onnx' (YOLOv8n, needs a model), 'opencv-dnn' "
            "(MobileNet-SSD via cv2.dnn, small model), 'opencv-hog' (OpenCV HOG+SVM, "
            "no model file), or 'fake'. Falls back to the NOMON_VISION_DETECTOR "
            "environment variable, then 'yolo-onnx'."
        ),
        "default": _DEFAULT_DETECTOR,
    },
    "model_path": {
        "type": "string",
        "description": (
            "Path to the detector weights — YOLOv8n ONNX (yolo-onnx) or the "
            "MobileNet-SSD .caffemodel (opencv-dnn). Falls back to the "
            "NOMON_VISION_MODEL_PATH environment variable when absent."
        ),
        "default": "",
    },
    "model_config": {
        "type": "string",
        "description": (
            "Path to the MobileNet-SSD .prototxt (opencv-dnn detector only). Falls "
            "back to the NOMON_VISION_MODEL_CONFIG environment variable when absent."
        ),
        "default": "",
    },
}


def _build_detector(params: dict[str, Any]) -> Detector:
    """Choose the detector backend by kind, honouring the fake-detections dev hook.

    Precedence: the ``NOMON_VISION_FAKE_DETECTIONS`` scripted hook wins outright;
    otherwise the kind is the ``detector`` param, then ``NOMON_VISION_DETECTOR``,
    then the default (``yolo-onnx``).

    Raises
    ------
    ValueError
        If the resolved detector kind is unknown.
    """
    fake = os.environ.get(_ENV_FAKE_DETECTIONS)
    if fake:
        return FakeDetector.from_json(fake)

    kind = (params.get("detector") or os.environ.get(_ENV_DETECTOR) or _DEFAULT_DETECTOR).strip()
    if kind == "yolo-onnx":
        model_path = params.get("model_path") or os.environ.get(_ENV_MODEL_PATH, "")
        return YoloOnnxDetector(model_path)
    if kind == "opencv-dnn":
        model_path = params.get("model_path") or os.environ.get(_ENV_MODEL_PATH, "")
        config_path = params.get("model_config") or os.environ.get(_ENV_MODEL_CONFIG, "")
        return OpenCvDnnDetector(model_path, config_path)
    if kind == "opencv-hog":
        return OpenCvHogDetector()
    if kind == "fake":
        return FakeDetector()
    raise ValueError(
        f"unknown vision detector {kind!r}; expected 'yolo-onnx', 'opencv-dnn', "
        "'opencv-hog', or 'fake'"
    )


def build_follow_user(
    client: httpx.AsyncClient,
    device_id: str,
    params: dict[str, Any],
) -> Pipeline:
    """Build the ``follow-user`` (vision person-following) pipeline.

    Parameters
    ----------
    client : httpx.AsyncClient
        Shared device client per ADR-002, injected into the vision and action layers.
    device_id : str
        Device identifier stamped on every emitted message.
    params : dict
        Routine parameters (see :data:`FOLLOW_USER_PARAMS_SCHEMA`).

    Returns
    -------
    Pipeline
        A fully wired pipeline ready to ``run()``.
    """
    detector = _build_detector(params)
    perception = VisionPerception(
        client,
        device_id,
        detector,
        camera_hfov_deg=params.get("camera_hfov_deg", _DEFAULT_CAMERA_HFOV_DEG),
        confidence_threshold=params.get("confidence_threshold", _DEFAULT_CONFIDENCE_THRESHOLD),
    )
    world_model = TargetWorldModel(
        device_id=device_id,
        lost_target_timeout_s=params.get("lost_target_timeout_s", _DEFAULT_LOST_TARGET_TIMEOUT_S),
    )
    planner = PursuitPlanner(
        device_id=device_id,
        target_distance_cm=params.get("target_distance_cm", _DEFAULT_TARGET_DISTANCE_CM),
        max_speed_pct=params.get("max_speed_pct", _DEFAULT_MAX_SPEED_PCT),
    )
    return Pipeline(
        perception=perception,
        world_model=world_model,
        planner=planner,
        action=VehicleAction(client, device_id=device_id),
    )
