"""Tests for the vision detector abstraction (FakeDetector, Detection, YOLO guards)."""

from __future__ import annotations

import pytest

from autonomon import Detection, Detector, FakeDetector, YoloOnnxDetector


def test_detection_fields() -> None:
    d = Detection(cx=0.5, cy=0.4, w=0.2, h=0.6, confidence=0.9)
    assert (d.cx, d.cy, d.w, d.h, d.confidence) == (0.5, 0.4, 0.2, 0.6, 0.9)


def test_fake_detector_returns_scripted_detections() -> None:
    dets = [Detection(0.5, 0.5, 0.2, 0.6, 0.8)]
    det = FakeDetector(dets)
    assert isinstance(det, Detector)  # satisfies the runtime-checkable Protocol
    out = det.detect(b"ignored frame bytes")
    assert out == dets
    # Returns a copy each call — callers can't mutate internal state.
    out.clear()
    assert det.detect(b"") == dets


def test_fake_detector_default_is_empty() -> None:
    assert FakeDetector().detect(b"x") == []


def test_fake_detector_from_json() -> None:
    det = FakeDetector.from_json('[{"cx": 0.25, "cy": 0.5, "w": 0.1, "h": 0.4, "confidence": 0.7}]')
    out = det.detect(b"")
    assert len(out) == 1
    assert out[0].cx == 0.25
    assert out[0].confidence == 0.7


def test_yolo_detector_constructs_without_model_or_deps() -> None:
    # Construction must not load the model or require onnxruntime/numpy.
    det = YoloOnnxDetector("/nonexistent/model.onnx")
    assert det._model_path == "/nonexistent/model.onnx"


def test_yolo_detector_without_model_path_raises_clearly() -> None:
    # _ensure_session reports a clear error before importing onnxruntime, so this
    # is deterministic whether or not the vision extra is installed.
    det = YoloOnnxDetector("")
    with pytest.raises(RuntimeError, match="no vision model configured"):
        det._ensure_session()
