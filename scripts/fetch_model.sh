#!/usr/bin/env bash
# Fetch the YOLOv8n ONNX model used by the `follow-user` vision routine.
#
# The runtime stack (the `vision` extra) is onnxruntime + numpy + pillow only;
# `ultralytics` (AGPL-3.0) is used *here, offline* to export the model and is NOT
# a runtime dependency. Two ways to obtain the model:
#
#   1. Set NOMON_VISION_MODEL_URL to a pre-exported yolov8n.onnx and we download it.
#   2. Otherwise we export it locally with ultralytics into a throwaway venv.
#
# Output: $MODEL_DIR/yolov8n.onnx (default ./models). Point the routine at it with
# NOMON_VISION_MODEL_PATH (or the `model_path` param).
#
# Usage:
#   scripts/fetch_model.sh                 # export via ultralytics (default)
#   NOMON_VISION_MODEL_URL=<url> scripts/fetch_model.sh   # download instead
set -euo pipefail

MODEL_DIR="${MODEL_DIR:-$(cd "$(dirname "$0")/.." && pwd)/models}"
MODEL_PATH="${MODEL_PATH:-$MODEL_DIR/yolov8n.onnx}"
mkdir -p "$MODEL_DIR"

if [ -f "$MODEL_PATH" ]; then
  echo "Model already present: $MODEL_PATH"
  exit 0
fi

if [ -n "${NOMON_VISION_MODEL_URL:-}" ]; then
  echo "Downloading model from \$NOMON_VISION_MODEL_URL -> $MODEL_PATH"
  curl -fSL "$NOMON_VISION_MODEL_URL" -o "$MODEL_PATH"
  echo "Done. Set NOMON_VISION_MODEL_PATH=$MODEL_PATH"
  exit 0
fi

echo "Exporting yolov8n.onnx with ultralytics (offline, AGPL — build-time only)..."
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
python3 -m venv "$WORK/venv"
# shellcheck disable=SC1091
. "$WORK/venv/bin/activate"
pip install --quiet --upgrade pip
pip install --quiet ultralytics onnx
( cd "$WORK" && yolo export model=yolov8n.pt format=onnx imgsz=640 )
cp "$WORK/yolov8n.onnx" "$MODEL_PATH"
echo "Done. Set NOMON_VISION_MODEL_PATH=$MODEL_PATH"
