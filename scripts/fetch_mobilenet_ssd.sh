#!/usr/bin/env bash
# Fetch the MobileNet-SSD model used by the `follow-user` vision routine's
# `opencv-dnn` detector (OpenCvDnnDetector, run via cv2.dnn).
#
# This is a small Caffe MobileNet-SSD (Pascal VOC, person = class 15): a ~29 KB
# prototxt + a ~23 MB caffemodel. No Python deps beyond opencv-python-headless
# (the `vision-opencv` extra), which already provides cv2.dnn — unlike the YOLO
# path there is no onnxruntime and no model export step.
#
# Output (default ./models):
#   $MODEL_DIR/MobileNetSSD_deploy.prototxt
#   $MODEL_DIR/MobileNetSSD_deploy.caffemodel
# Point the routine at them with NOMON_VISION_MODEL_PATH (caffemodel) and
# NOMON_VISION_MODEL_CONFIG (prototxt), or the model_path / model_config params.
#
# Override the source URLs with NOMON_VISION_DNN_PROTO_URL / NOMON_VISION_DNN_MODEL_URL.
#
# Usage:
#   scripts/fetch_mobilenet_ssd.sh
set -euo pipefail

MODEL_DIR="${MODEL_DIR:-$(cd "$(dirname "$0")/.." && pwd)/models}"
PROTO_PATH="${PROTO_PATH:-$MODEL_DIR/MobileNetSSD_deploy.prototxt}"
MODEL_PATH="${MODEL_PATH:-$MODEL_DIR/MobileNetSSD_deploy.caffemodel}"

PROTO_URL="${NOMON_VISION_DNN_PROTO_URL:-https://raw.githubusercontent.com/djmv/MobilNet_SSD_opencv/master/MobileNetSSD_deploy.prototxt}"
MODEL_URL="${NOMON_VISION_DNN_MODEL_URL:-https://github.com/djmv/MobilNet_SSD_opencv/raw/master/MobileNetSSD_deploy.caffemodel}"

mkdir -p "$MODEL_DIR"

if [ -f "$PROTO_PATH" ] && [ -f "$MODEL_PATH" ]; then
  echo "MobileNet-SSD already present: $PROTO_PATH, $MODEL_PATH"
  exit 0
fi

echo "Downloading MobileNet-SSD prototxt -> $PROTO_PATH"
curl -fSL "$PROTO_URL" -o "$PROTO_PATH"

echo "Downloading MobileNet-SSD caffemodel (~23 MB) -> $MODEL_PATH"
curl -fSL "$MODEL_URL" -o "$MODEL_PATH"

# Sanity check: the caffemodel must be a binary blob, not an HTML error page.
if head -c 64 "$MODEL_PATH" | grep -qi "<!DOCTYPE\|<html"; then
  echo "Error: downloaded caffemodel looks like HTML (bad URL?). Removing." >&2
  rm -f "$MODEL_PATH"
  exit 1
fi

echo "Done. Set:"
echo "  NOMON_VISION_MODEL_PATH=$MODEL_PATH"
echo "  NOMON_VISION_MODEL_CONFIG=$PROTO_PATH"
