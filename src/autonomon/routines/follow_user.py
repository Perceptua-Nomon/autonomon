"""The ``follow-user`` routine: vision-based person following.

The proof that the routine registry generalises beyond wiring (ADR-003): it reuses
``VehicleAction`` unchanged but introduces three net-new layers — a vision
perception layer (person detection in autonomon, ADR-004), a target world model,
and a pursuit planner::

    VisionPerception -> TargetWorldModel -> PursuitPlanner -> VehicleAction

The detector is chosen at build time: by default a :class:`YoloOnnxDetector` over a
YOLOv8n ONNX model (``model_path`` param or ``NOMON_VISION_MODEL_PATH``). Setting
``NOMON_VISION_FAKE_DETECTIONS`` to a JSON array of detections selects a
:class:`FakeDetector` instead — the dev/CI hook for exercising the pipeline without
a model.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from autonomon.action.vehicle import VehicleAction
from autonomon.perception.detector import Detector, FakeDetector, YoloOnnxDetector
from autonomon.perception.vision import VisionPerception
from autonomon.pipeline import Pipeline
from autonomon.planning.pursuit import PursuitPlanner
from autonomon.world_model.target import TargetWorldModel

_DEFAULT_TARGET_DISTANCE_CM = 80.0
_DEFAULT_MAX_SPEED_PCT = 60.0
_DEFAULT_CONFIDENCE_THRESHOLD = 0.5
_DEFAULT_CAMERA_HFOV_DEG = 70.0
_DEFAULT_LOST_TARGET_TIMEOUT_S = 1.5

# Env hooks (deploy/runtime concerns, not behaviour params).
_ENV_MODEL_PATH = "NOMON_VISION_MODEL_PATH"
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
    "model_path": {
        "type": "string",
        "description": (
            "Path to the YOLOv8n ONNX model. Falls back to the NOMON_VISION_MODEL_PATH "
            "environment variable when absent."
        ),
        "default": "",
    },
}


def _build_detector(params: dict[str, Any]) -> Detector:
    """Choose the detector: the fake-detections dev hook, else the YOLOv8n model."""
    fake = os.environ.get(_ENV_FAKE_DETECTIONS)
    if fake:
        return FakeDetector.from_json(fake)
    model_path = params.get("model_path") or os.environ.get(_ENV_MODEL_PATH, "")
    return YoloOnnxDetector(model_path)


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
