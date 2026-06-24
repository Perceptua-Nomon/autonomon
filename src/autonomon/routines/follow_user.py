"""The ``follow-user`` routine: vision-based person following.

The proof that the routine registry generalises beyond wiring (ADR-003): it reuses
``VehicleAction`` unchanged but introduces three net-new layers — a vision
perception layer (person detection in autonomon, ADR-004), a target world model,
and a pursuit planner::

    VisionPerception -> TargetWorldModel -> FollowPlanner -> VehicleAction

The :class:`FollowPlanner` pans/tilts the camera to keep the person centred
capture-to-capture, steers the body toward the camera so the camera re-centres
forward as the body turns in, holds a ``target_distance_cm`` standoff (backing up
if the person comes closer), and — when no person is visible — sweeps the camera
to "look around", pivoting the body once a sweep is exhausted.

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
from autonomon.planning.follow import FollowPlanner
from autonomon.world_model.target import TargetWorldModel

_DEFAULT_TARGET_DISTANCE_CM = 60.0  # ≈ 2 ft
_DEFAULT_MAX_SPEED_PCT = 60.0
_DEFAULT_MIN_DRIVE_SPEED_PCT = 40.0
_DEFAULT_MAX_STEER_DEG = 30.0
_DEFAULT_DRIVE_BURST_S = 0.6
_DEFAULT_CONFIDENCE_THRESHOLD = 0.5
_DEFAULT_CAMERA_HFOV_DEG = 70.0
_DEFAULT_CAMERA_VFOV_DEG = 43.0
# Held visible this long after the last detection. On the Pi Zero 2W the person
# detector runs at ~0.8 Hz, so a generous bridge lets a single detection sustain
# tracking across the gaps between successful frames (confirmed on-device).
_DEFAULT_LOST_TARGET_TIMEOUT_S = 4.0
_DEFAULT_DETECTOR = "yolo-onnx"

# Camera-tracking gains. Verified on-device 2026-06-24 by commanding the servos:
#   pan  — lower angle aims to the robot's RIGHT, so a right-of-centre (+bearing)
#          target needs pan to DECREASE → gain is NEGATIVE (the PicarX pan servo
#          is inverted vs the "+angle = +bearing" assumption).
#   tilt — higher angle aims DOWN, so a below-centre (+vertical-bearing) target
#          needs tilt to INCREASE → gain is POSITIVE (tilt happens to align).
# A unit with differently-oriented servos can override these per launch.
_DEFAULT_PAN_GAIN = -0.5
_DEFAULT_TILT_GAIN = 0.5
_DEFAULT_CENTER_DEADBAND_DEG = 4.0
_DEFAULT_PAN_MIN_DEG = 20.0
_DEFAULT_PAN_MAX_DEG = 160.0
_DEFAULT_TILT_MIN_DEG = 60.0
_DEFAULT_TILT_MAX_DEG = 120.0
_DEFAULT_SEARCH_STEP_DEG = 10.0
# Per-glance dwell (~one detector period) so each position yields a sharp, settled
# frame the detector can use, rather than motion-blurred ones.
_DEFAULT_SEARCH_INTERVAL_S = 1.2
_DEFAULT_SEARCH_TILT_OFFSET_DEG = 15.0
_DEFAULT_BODY_ROTATE_SPEED_PCT = 60.0
_DEFAULT_BODY_ROTATE_DURATION_S = 1.5

# Env hooks (deploy/runtime concerns, not behaviour params).
_ENV_DETECTOR = "NOMON_VISION_DETECTOR"
_ENV_MODEL_PATH = "NOMON_VISION_MODEL_PATH"
_ENV_MODEL_CONFIG = "NOMON_VISION_MODEL_CONFIG"
_ENV_FAKE_DETECTIONS = "NOMON_VISION_FAKE_DETECTIONS"

FOLLOW_USER_PARAMS_SCHEMA: dict[str, dict[str, Any]] = {
    "target_distance_cm": {
        "type": "number",
        "description": "Standoff distance (cm) to hold from the followed person (default ≈ 2 ft).",
        "default": _DEFAULT_TARGET_DISTANCE_CM,
    },
    "max_speed_pct": {
        "type": "number",
        "description": "Maximum drive speed magnitude (0–100) while pursuing.",
        "default": _DEFAULT_MAX_SPEED_PCT,
    },
    "min_drive_speed_pct": {
        "type": "number",
        "description": (
            "Floor for a non-zero drive speed so it clears motor stiction and has "
            "enough momentum to actually turn the vehicle. Default 50."
        ),
        "default": _DEFAULT_MIN_DRIVE_SPEED_PCT,
    },
    "max_steer_deg": {
        "type": "number",
        "description": (
            "Hard cap on steering deflection from centre (deg); all steer commands "
            "are clamped to ±this. Default 30."
        ),
        "default": _DEFAULT_MAX_STEER_DEG,
    },
    "drive_burst_s": {
        "type": "number",
        "description": (
            "Max seconds to keep driving after a detection; the motor pauses until "
            "the next detection re-aims, so the robot drives in short closed-loop "
            "bursts rather than running on a stale heading. Default 0.6."
        ),
        "default": _DEFAULT_DRIVE_BURST_S,
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
    "camera_vfov_deg": {
        "type": "number",
        "description": "Camera vertical field of view (deg), used to map box offset to tilt bearing.",
        "default": _DEFAULT_CAMERA_VFOV_DEG,
    },
    "pan_gain": {
        "type": "number",
        "description": (
            "Camera pan degrees per degree of in-frame horizontal error. Negative "
            "by default because the PicarX pan servo is inverted; flip the sign for "
            "a unit with correctly-oriented servos."
        ),
        "default": _DEFAULT_PAN_GAIN,
    },
    "tilt_gain": {
        "type": "number",
        "description": (
            "Camera tilt degrees per degree of in-frame vertical error. Positive by "
            "default (higher tilt angle aims down, matching a below-centre target)."
        ),
        "default": _DEFAULT_TILT_GAIN,
    },
    "center_deadband_deg": {
        "type": "number",
        "description": (
            "In-frame bearing within which the camera holds still while tracking, "
            "so a centred target is not nudged out of frame by jitter. Default 4."
        ),
        "default": _DEFAULT_CENTER_DEADBAND_DEG,
    },
    "pan_min_deg": {
        "type": "number",
        "description": "Camera pan servo lower limit (deg; 90 = forward).",
        "default": _DEFAULT_PAN_MIN_DEG,
    },
    "pan_max_deg": {
        "type": "number",
        "description": "Camera pan servo upper limit (deg; 90 = forward).",
        "default": _DEFAULT_PAN_MAX_DEG,
    },
    "tilt_min_deg": {
        "type": "number",
        "description": "Camera tilt servo lower limit (deg; 90 = level).",
        "default": _DEFAULT_TILT_MIN_DEG,
    },
    "tilt_max_deg": {
        "type": "number",
        "description": "Camera tilt servo upper limit (deg; 90 = level).",
        "default": _DEFAULT_TILT_MAX_DEG,
    },
    "search_step_deg": {
        "type": "number",
        "description": (
            "Phase advance (deg) per search step as the camera 'rolls' around to "
            "look for a user; one full roll is 360°. Default 10."
        ),
        "default": _DEFAULT_SEARCH_STEP_DEG,
    },
    "search_interval_s": {
        "type": "number",
        "description": "Seconds between search steps (per-position dwell) while looking around.",
        "default": _DEFAULT_SEARCH_INTERVAL_S,
    },
    "search_tilt_offset_deg": {
        "type": "number",
        "description": "Tilt amplitude of the search roll; the camera bobs ±this about level.",
        "default": _DEFAULT_SEARCH_TILT_OFFSET_DEG,
    },
    "body_rotate_speed_pct": {
        "type": "number",
        "description": "Drive speed for the body-pivot arc once a camera sweep finds nobody.",
        "default": _DEFAULT_BODY_ROTATE_SPEED_PCT,
    },
    "body_rotate_duration_s": {
        "type": "number",
        "description": "Seconds to commit to the body-pivot arc before resuming the camera sweep.",
        "default": _DEFAULT_BODY_ROTATE_DURATION_S,
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
        camera_vfov_deg=params.get("camera_vfov_deg", _DEFAULT_CAMERA_VFOV_DEG),
        confidence_threshold=params.get("confidence_threshold", _DEFAULT_CONFIDENCE_THRESHOLD),
    )
    world_model = TargetWorldModel(
        device_id=device_id,
        lost_target_timeout_s=params.get("lost_target_timeout_s", _DEFAULT_LOST_TARGET_TIMEOUT_S),
    )
    planner = FollowPlanner(
        device_id=device_id,
        target_distance_cm=params.get("target_distance_cm", _DEFAULT_TARGET_DISTANCE_CM),
        max_speed_pct=params.get("max_speed_pct", _DEFAULT_MAX_SPEED_PCT),
        min_drive_speed_pct=params.get("min_drive_speed_pct", _DEFAULT_MIN_DRIVE_SPEED_PCT),
        max_steer_deg=params.get("max_steer_deg", _DEFAULT_MAX_STEER_DEG),
        drive_burst_s=params.get("drive_burst_s", _DEFAULT_DRIVE_BURST_S),
        pan_gain=params.get("pan_gain", _DEFAULT_PAN_GAIN),
        tilt_gain=params.get("tilt_gain", _DEFAULT_TILT_GAIN),
        center_deadband_deg=params.get("center_deadband_deg", _DEFAULT_CENTER_DEADBAND_DEG),
        pan_min_deg=params.get("pan_min_deg", _DEFAULT_PAN_MIN_DEG),
        pan_max_deg=params.get("pan_max_deg", _DEFAULT_PAN_MAX_DEG),
        tilt_min_deg=params.get("tilt_min_deg", _DEFAULT_TILT_MIN_DEG),
        tilt_max_deg=params.get("tilt_max_deg", _DEFAULT_TILT_MAX_DEG),
        search_step_deg=params.get("search_step_deg", _DEFAULT_SEARCH_STEP_DEG),
        search_interval_s=params.get("search_interval_s", _DEFAULT_SEARCH_INTERVAL_S),
        search_tilt_offset_deg=params.get(
            "search_tilt_offset_deg", _DEFAULT_SEARCH_TILT_OFFSET_DEG
        ),
        body_rotate_speed_pct=params.get("body_rotate_speed_pct", _DEFAULT_BODY_ROTATE_SPEED_PCT),
        body_rotate_duration_s=params.get(
            "body_rotate_duration_s", _DEFAULT_BODY_ROTATE_DURATION_S
        ),
    )
    return Pipeline(
        perception=perception,
        world_model=world_model,
        planner=planner,
        action=VehicleAction(client, device_id=device_id),
    )
