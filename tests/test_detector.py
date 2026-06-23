"""Tests for the vision detector abstraction (FakeDetector, Detection, YOLO guards)."""

from __future__ import annotations

import pytest

from autonomon import (
    Detection,
    Detector,
    FakeDetector,
    OpenCvDnnDetector,
    OpenCvHogDetector,
    YoloOnnxDetector,
)


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


# OpenCvHogDetector -----------------------------------------------------------


def test_opencv_hog_constructs_lazily_without_deps() -> None:
    # Construction sets no descriptor and must not import opencv: a model-free
    # detector that can be wired in the factory regardless of the extra.
    det = OpenCvHogDetector()
    assert isinstance(det, Detector)  # satisfies the runtime-checkable Protocol
    assert det._hog is None


def test_opencv_hog_undecodable_frame_returns_empty() -> None:
    pytest.importorskip("cv2")
    det = OpenCvHogDetector()
    # imdecode returns None for non-image bytes → no detections, no error.
    assert det.detect(b"definitely not a jpeg") == []


def test_opencv_hog_returns_detection_list_for_a_valid_frame() -> None:
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    # A blank frame: HOG finds nobody, but detect() must still return a
    # (possibly empty) list of Detection objects with normalised coords.
    frame = np.zeros((128, 96, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", frame)
    assert ok
    out = OpenCvHogDetector(detect_width=96).detect(buf.tobytes())
    assert isinstance(out, list)
    assert all(isinstance(d, Detection) for d in out)
    assert all(0.0 <= d.cx <= 1.0 and 0.0 <= d.cy <= 1.0 for d in out)


# OpenCvDnnDetector -----------------------------------------------------------


def test_opencv_dnn_constructs_lazily_without_deps() -> None:
    # Construction loads no network and must not import opencv: it can be wired
    # in the factory whether or not the model files or the extra are present.
    det = OpenCvDnnDetector("/models/mnssd.caffemodel", "/models/deploy.prototxt")
    assert isinstance(det, Detector)  # satisfies the runtime-checkable Protocol
    assert det._net is None


def test_opencv_dnn_without_model_paths_raises_clearly() -> None:
    # _ensure_net reports a clear error before importing opencv, so this is
    # deterministic whether or not the vision-opencv extra is installed.
    det = OpenCvDnnDetector("", "")
    with pytest.raises(RuntimeError, match="no vision model configured"):
        det._ensure_net()
