# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Role

`autonomon` is the **brain** of the nomon fleet: a single Python package that adds
autonomous capabilities by driving a device through a four-layer cognitive pipeline
(Perception → World Model → Planning → Action) over the nomothetic REST API. It is a
standalone project — separate venv, never imported by nomothetic (ADR-005). All
perception, fusion, vision, modeling, and planning live here; nomothetic is a thin
raw-I/O gateway (ADR-004).

## Layout

Single `src`-layout package (`src/autonomon/`):

| Module | Role |
|--------|------|
| `messages.py` | `PerceptionEvent`, `WorldStateUpdate`, `ActionPlan`, `ActionResult` dataclasses (`to_dict`/`from_dict` at boundaries) |
| `pipeline.py` | `Pipeline` — wires the four layers with bounded asyncio queues |
| `slot.py` | `LayerSlot`, `SlotState` — owns one layer's asyncio task + queues |
| `fan_in.py` | `FanInSlot` — multi-source perception fan-in (pass-through) |
| `plugin_auth.py` | Ed25519 challenge-response device-JWT auth (nomothetic ADR-019) |
| `perception/` | `PerceptionBase`, `Perceptron` (configurable single-sensor) |
| `world_model/` | `WorldModelBase`, `ObstacleWorldModel` |
| `planning/` | `PlannerBase`, `AvoidancePlanner` |
| `action/` | `ActionBase`, `VehicleAction` |
| `routines/` | registry, the `explore` factory, the `nomon-autonomon` CLI, catalogue publish, status reporting |

## Commands

```bash
cd autonomon
make install-dev         # uv sync --all-extras
make test                # uv run pytest tests/ -v
uv run pytest tests/test_pipeline.py::test_name   # single test
make lint                # ruff check + black --check
make format              # black + ruff --fix
make type-check          # mypy src/ tests/
make check               # lint + type-check + test

# Run a routine against a device (manual):
NOMON_DEVICE_URL=https://<pi-host>:8443 \
NOMON_PLUGIN_TOKEN=<device-jwt> \
NOMON_PLUGIN_PARAMS='{"routine": "explore", "forward_speed_pct": 40}' \
nomon-autonomon
```

Run everything via `wsl.exe` when working from the Windows mount (the toolchain — `uv`,
`make` — lives in WSL).

## Four-Layer Architecture

```
nomothetic REST API (HTTPS :8443)
  │ poll                  ▲ execute
  ▼                       │
Perception ──► World Model ──► Planning ──► Action
  PerceptionEvent   WorldStateUpdate   ActionPlan   ActionResult
```

Each layer is an asyncio coroutine connected by **typed**, bounded
`asyncio.Queue` channels carrying message **instances** (not dicts) — e.g.
`asyncio.Queue[PerceptionEvent]` — with back-pressure by design. `to_dict()`/
`from_dict()` are used only at serialisation boundaries (telemetry, NDJSON, tests).
See ADR-006.

**Layer contract:**
- `PerceptionBase.run(queue_out)` — poll sensors, emit `PerceptionEvent`s
- `WorldModelBase.run(queue_in, queue_out)` — fuse, emit `WorldStateUpdate`s on change
- `PlannerBase.run(queue_in, queue_out)` — pure logic, emit `ActionPlan`s on plan change
- `ActionBase.run(queue_in)` — execute plans via httpx; produce `ActionResult`s

## Routines

A **routine** is a named factory `build_<name>(client, device_id, params) -> Pipeline`
registered in `autonomon.routines.registry.ROUTINES`. This is the catalogue of what the
robot can do, and the mechanism for **swappable models**: each routine wires the layer
implementations it needs.

- `explore` — obstacle/cliff avoidance: `Perceptron.ultrasonic` (+ `grayscale` via a
  `FanInSlot`) → `ObstacleWorldModel` → `AvoidancePlanner` → `VehicleAction`.
- `follow-user` — vision person-following: `VisionPerception` (polls
  `GET /api/camera/frame`, detects a person) → `TargetWorldModel` → `FollowPlanner` →
  reused `VehicleAction`. `FollowPlanner` pans/tilts the camera to keep the person
  centred, steers the body toward the camera (so it re-centres forward as the body
  turns in), holds a `target_distance_cm` standoff (≈ 2 ft default), and sweeps the
  camera (then pivots the body) to search when no one is visible. (`PursuitPlanner`
  is the earlier drive/steer-only follower, retained but superseded for this routine.)

**Swappable detectors (`follow-user`):** the detector backend is chosen by *kind* via
the `detector` param or `NOMON_VISION_DETECTOR` env var (`_build_detector` in
`routines/follow_user.py`):
- `yolo-onnx` (default) — `YoloOnnxDetector`, YOLOv8n via onnxruntime (`vision` extra +
  a `yolov8n.onnx` at `NOMON_VISION_MODEL_PATH`). Most accurate.
- `opencv-dnn` — `OpenCvDnnDetector`, MobileNet-SSD via `cv2.dnn` (`vision-opencv` extra +
  a ~23 MB caffemodel at `NOMON_VISION_MODEL_PATH` and prototxt at
  `NOMON_VISION_MODEL_CONFIG`). Robust and light — the recommended OpenCV option.
- `opencv-hog` — `OpenCvHogDetector`, OpenCV's built-in HOG+SVM (`vision-opencv` extra).
  **No model file**, but brittle (architectural edges fool it) — a last resort.
- `fake` — `FakeDetector` (no deps). `NOMON_VISION_FAKE_DETECTIONS` (a JSON array)
  forces a scripted `FakeDetector` regardless of kind — the dev/CI hook.

**Multi-source perception fan-in:** pass a `FanInSlot` as `perception` to run several
sensor sources onto one queue (Perception position only). There is no runtime layer
hot-swap and no planner arbitration — both were removed as speculative (ADR-006); the
registry/factory provides the swappability routines actually use.

## Plugin System & Catalogue Handoff

One generic CLI entry point, `nomon-autonomon` (`routines/cli.py`), runs any routine by
name. It reads `NOMON_DEVICE_URL`, `NOMON_PLUGIN_PARAMS` (JSON; selects the routine via a
`routine`/`name` key), and credentials from the env, builds the `Pipeline`, runs it, and
emits NDJSON lifecycle events (`starting`/`running`/`stopping`/`error`) to stdout.

**autonomon owns its own runtime config (ADR-004/005).** At startup the CLI loads
`/etc/autonomon/autonomon.env` (override `NOMON_AUTONOMON_ENV_FILE`; written by
`scripts/deploy.sh`) into the environment **non-overriding** — so autonomon-only settings
(`NOMON_VISION_DETECTOR`, `NOMON_VISION_MODEL_PATH`, …) reach *every* run, including
routines launched by nomothetic. nomothetic keeps a deliberately minimal subprocess env
and carries **none** of the brain's config; values it does set (device URL, id, creds)
still win because the file load never overrides an existing var.

At deploy time autonomon publishes its catalogue (`nomon_manifest` + its venv's
`nomon-autonomon` path) to `NOMON_ROUTINE_CATALOG_PATH` (default
`/var/lib/nomon/routine_catalog.json`) via `python -m autonomon.routines.publish`.
nomothetic reads that file to list and launch routines. The two projects keep
**separate venvs and never import each other** (ADR-005).

**Auth:** prefer `NOMON_PLUGIN_KEY` (Ed25519 private key) → challenge-response device JWT
via `plugin_auth.PluginTokenAuth` (refresh on 401, no token on disk); fall back to a
static `NOMON_PLUGIN_TOKEN`. Secrets are never logged.

## Coding Conventions

Same toolchain as nomothetic (`black` line length 100, `ruff`, `mypy`, `pytest`):
- NumPy-style docstrings on public classes/methods; full type hints
- `asyncio.to_thread()` for blocking I/O inside async methods
- Exception chaining: `raise NewError("...") from e`
- No direct I2C/GPIO — all device I/O via the nomothetic REST API (`httpx`,
  `verify=False` on device connections per nomothetic ADR-001)
- Heavy/optional deps (vision) go in a `pyproject.toml` extra and are lazy-imported
  (`try/except ImportError`), so the core install and CI stay light

## Testing

- All layers testable without a device: inject a mock `httpx.AsyncClient` in
  Perception/Action; push typed messages into queues for World Model/Planning
- Vision: inject a `FakeDetector` and a mock frame client — CI never needs onnxruntime
- `pytest-asyncio` with `asyncio_mode = "auto"`; mark async tests `@pytest.mark.asyncio`
- No Pi hardware required for any test

## Key Docs

- `docs/architecture.md` — layer diagram, message schemas, routines, nomothetic API surface
- `docs/roadmap.md` — phase status (3, 4, 7 deferred; 5/6b/6c active)
- `docs/adr/001-layered-architecture.md` — the four-layer design
- `docs/adr/003-routine-registry.md` — routines as pipeline factories
- `docs/adr/004-autonomon-is-the-brain.md` — all cognition here, nomothetic is a gateway
- `docs/adr/005-file-based-catalog-handoff.md` — standalone venvs, file catalogue
- `docs/adr/006-lean-core-no-hot-swap-typed-queues.md` — removed hot-swap/arbitration; typed queues
