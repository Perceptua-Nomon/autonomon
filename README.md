# autonomon

The **brain** of the nomon fleet: a four-layer cognitive pipeline
(Perception → World Model → Planning → Action) that drives a robot autonomously
over the [nomothetic](../nomothetic) REST API. autonomon owns all sensor fusion,
vision, modeling, and planning; nomothetic is a thin raw-I/O gateway (ADR-004).
It is a standalone project — its own virtualenv, never imported by nomothetic
(ADR-005).

## Routines

A **routine** is a named behaviour that wires the four layers into a pipeline:

| Routine | What it does | Layers |
|---------|--------------|--------|
| `explore` | Obstacle/cliff-avoidance wandering | ultrasonic (+ grayscale) → obstacle world model → avoidance planner → vehicle action |
| `follow-user` | Vision person-following with camera tracking, look-around search, and ~2 ft distance-keeping | camera-frame person detection → target world model → follow planner (camera pan/tilt + drive/steer) → vehicle action |

## Develop

```bash
cd autonomon
make install-dev     # uv sync --all-extras (includes the vision stack)
make check           # ruff + black + mypy + pytest
make test            # pytest only
```

Python ≥ 3.9; dev/CI is x86_64 Linux with all hardware mocked (no Pi needed). On
the Windows/WSL mount, run these via `wsl.exe` (the toolchain lives in WSL).

## Run a routine locally (against a device)

```bash
NOMON_DEVICE_URL=https://<pi-host>:8443 \
NOMON_PLUGIN_TOKEN=<device-jwt> \
NOMON_PLUGIN_PARAMS='{"routine": "explore", "forward_speed_pct": 40}' \
nomon-autonomon
```

The CLI emits NDJSON lifecycle events (`starting` / `running` / `stopping` /
`error`) to stdout. Prefer key-based auth in production: set `NOMON_PLUGIN_KEY`
(an Ed25519 private key) instead of `NOMON_PLUGIN_TOKEN` — see
`autonomon.plugin_auth`. Copy `.env.device.example` → `.env.device` for the full
list of variables.

## Vision model (`follow-user`)

The vision routine runs a YOLOv8n ONNX model via onnxruntime. Fetch the model
once and point the routine at it:

```bash
scripts/fetch_model.sh                       # export yolov8n.onnx into ./models
export NOMON_VISION_MODEL_PATH=$PWD/models/yolov8n.onnx
NOMON_PLUGIN_PARAMS='{"routine": "follow-user", "target_distance_cm": 100}' nomon-autonomon
```

`ultralytics` (AGPL) is used only by `fetch_model.sh` to export the model — it is
**not** a runtime dependency (the runtime stack is `onnxruntime` + `numpy` +
`pillow`, the `vision` extra). For dev/CI without a model, set
`NOMON_VISION_FAKE_DETECTIONS` to a JSON array of detections to use a fake
detector (see `tests/test_integration_subprocess.py`).

## Deploy to a Pi

`scripts/deploy.sh` deploys over SSH (or locally when already on the Pi),
creating autonomon's **own** virtualenv (separate from nomothetic per ADR-005),
verifying the CLI, generating + registering the device key, and publishing the
routine catalogue for nomothetic to read.

```bash
make deploy-local PI_HOST=perceptua@perceptua   # rsync the local tree + install
make deploy PI_HOST=perceptua@perceptua         # deploy the latest semver tag
# or directly:
./scripts/deploy.sh --local perceptua@perceptua
```

Configure deployment via `.env.device` (`NOMON_PI_HOST`, `NOMON_SSH_KEY`, …). At
deploy time autonomon publishes its catalogue (manifest + its venv's
`nomon-autonomon` path) to `NOMON_ROUTINE_CATALOG_PATH` (default
`/var/lib/nomon/routine_catalog.json`, **must match** nomothetic's
`.env.device`); nomothetic reads that file to list and launch routines without
importing autonomon (ADR-005).

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — layers, message types, routines, the nomothetic API surface
- [`docs/roadmap.md`](docs/roadmap.md) — phase status
- `docs/adr/` — architecture decisions (001 layers, 003 routines, 004 the brain, 005 catalogue handoff, 006 lean core)
