# autonomon — Development Roadmap

## Status Summary

| Phase | Name | Status |
|-------|------|--------|
| 0 | nomon_explore plugin (pre-framework) | ✅ Complete |
| 1 | Core framework (`autonomon` package) | ✅ Complete |
| 2 | Perception implementations (ultrasonic, grayscale, battery) | ✅ Complete |
| 3 | Occupancy-grid world model | ⏸️ Deferred — speculative (no consumer; the `ObstacleWorldModel` slice suffices) |
| 4 | Rule-based planner (rule-table/TOML) | ⏸️ Deferred — speculative (no consumer; the `AvoidancePlanner` slice suffices) |
| 5 | Vehicle action executor — retry + safety-stop | ✅ Complete |
| 6 | Routine registry (`explore` as first entry) | ✅ Complete |
| 6b | `follow-user` vision routine (camera + person detection) | ✅ Complete |
| 6c | Device deployment & integration testing | ✅ Complete |
| 7 | Autonomy telemetry to ArcadeDB | ⏸️ Deferred — needs device→central transport/auth design |

> **Lean core (ADR-006).** Runtime layer hot-swap (`Pipeline.swap_layer` /
> `LayerSlot.swap`) and competing-planner fan-in arbitration (`MergeStrategy.ARBITRATE`,
> dynamic `add_impl`/`remove_impl`) were **removed** as speculative machinery with no
> routine consumer. The "swappable models" capability is preserved by the routine
> registry/factory (each routine wires the layer implementations it needs) and by
> multi-source perception fan-in (`FanInSlot`, pass-through). Inter-layer queues now
> carry **typed message instances** rather than dicts. See ADR-006.

> **Vertical slice (current):** A minimal concrete implementation of every layer
> now exists, and `tests/test_pipeline_integration.py` runs the full
> `Perception → World Model → Planning → Action` loop end-to-end against a mock
> device (the Phase 6 "full-pipeline integration test" deliverable, landed
> early). The autonomy loop closes: a near ultrasonic reading drives a stop +
> reverse + steer command sequence. Phases 3–5 below now track the remaining
> *full* implementations.

---

## Architecture Principle — autonomon is the brain

All input processing and modeling lives in **autonomon**: sensor fusion,
computer vision, person/object detection, world modeling, and planning. The
autonomy pipeline is **self-contained between two boundaries** — Perception
ingests *raw* inputs, Action emits *action* commands — and everything in
between is autonomon's responsibility.

**nomothetic stays a thin hardware gateway.** It serves *raw* inputs (sensor
reads, raw camera frames/stream) and accepts *action* outputs (drive, steer,
stop, camera pan/tilt) over REST. It performs **no** perception, detection, or
modeling; no autonomy logic is ever pushed down into nomothetic or nomopractic.

**Consequences:**
- New capabilities (e.g. person detection) are added as autonomon perception
  implementations that pull raw data from nomothetic, **not** as new processing
  endpoints on nomothetic.
- nomothetic's API surface only grows when a *new raw input or raw actuator*
  is exposed — never to add interpretation of existing data.
- Heavy models can run in the autonomon process wherever it is hosted
  (on-device or on a remote host); the boundary contract is unchanged either
  way.

---

## Completed Phases

### Phase 0 — nomon_explore Plugin (Pre-Framework)

**Deliverables:**
- `nomon_explore` package: standalone obstacle-avoidance drive plugin
- `NomothicClient` wrapping httpx for nomothetic REST calls
- `ExploreConfig` dataclass; `run_loop` control function
- CLI entry point (`nomon-explore`) reading env vars, emitting NDJSON lifecycle events
- `nomon_manifest` dict for plugin discovery by `nomothetic AutonomyPluginManager`
- Capabilities: ultrasonic obstacle detection, grayscale cliff detection
- Parameters: speed, obstacle threshold, cliff threshold, duration, loop interval, avoidance timing

**Architecture note:** Phase 0 embedded all logic in a single-package monolith. That package no longer exists on disk (only empty `nomon_explore/src/` and `tests/` scaffolding remain). Phases 1–5 build the layered framework; Phase 6 (reshaped) re-introduces `explore` as a *routine* — a registry entry that wires the framework — rather than restoring the monolith. See ADR-003.

---

### Phase 1 — Core Framework (`autonomon` package)

**Status:** ✅ Complete

**Delivered:**
- `autonomon` package with `autonomon.messages`: `PerceptionEvent`, `WorldStateUpdate`, `ActionPlan`, `ActionResult` dataclasses with `to_dict()` / `from_dict()`
- Layer base classes: `PerceptionBase` (`run(queue_out)`), `WorldModelBase` / `PlannerBase` (`run(queue_in, queue_out)`), `ActionBase` (`run(queue_in)`), each with `stop()`
- `autonomon.pipeline.Pipeline`: wires layers with bounded asyncio queues and graceful shutdown
- Per-layer task ownership (`autonomon.slot.LayerSlot`, `SlotState`) and multi-source perception fan-in (`autonomon.fan_in.FanInSlot`, pass-through). (Runtime hot-swap and planner arbitration were later removed — ADR-006.)
- pytest suite covering pipeline wiring, back-pressure, hot-swap, and fan-in
- `docs/architecture.md`, `docs/roadmap.md`, `docs/adr/001-layered-architecture.md`, `docs/adr/002-rest-api-client-pattern.md`

---

### Phase 2 — Perception Implementations

**Status:** ✅ Complete

**Delivered:**
- `autonomon.perception.Perceptron`: a single configurable perception class declaring `sensor_type` + `endpoint` + `interpreter`, sharing all polling, timeout, error-handling, and stop logic
- Built-in sensors defined in one declarative `_SENSOR_SPECS` table, exposed via named constructors:
  - `Perceptron.ultrasonic` → `GET /api/sensor/ultrasonic`; emits `data={"distance_cm": float | None}`
  - `Perceptron.grayscale` → `GET /api/sensor/grayscale`; emits `data={"channels": [...], "values": [...]}` — **raw** ADC counts. The raw endpoint (not `/normalized`) is used deliberately: this hardware reads inverted vs the normalisation calibration, so a cliff is a *low* raw reading (floor ~400-900, edge ~30; threshold 200). See the World Model section and `ObstacleWorldModel`.
  - `Perceptron.battery` → `GET /api/hat/battery`; emits `data={"voltage_v": float}`
- Configurable poll interval and per-request timeout per instance (battery defaults to 30 s; others 0.1 s)
- Custom sensors via the general `Perceptron(...)` constructor with a user-supplied interpreter
- Transient HTTP errors, request errors, and timeouts are absorbed; the poll loop continues
- `AsyncMock` / `MagicMock` httpx fixtures for device-free testing (per ADR-002)

**Design note:** the original plan called for one class per sensor (`UltrasonicPerception`, `GrayscalePerception`, `BatteryPerception`); this was consolidated into a single configurable `Perceptron` so sensor differences are data (endpoint + interpreter + interval), not duplicated classes.

---

## Planned Phases

### Phase 3 — Occupancy-Grid World Model

**Status:** ⏸️ **Deferred (speculative — no consumer).** No current or planned routine
needs a spatial occupancy grid: `explore` uses boolean obstacle/cliff state, and
`follow-user` uses a target's relative bearing/range. A grid serves mapping /
path-planning behaviours that are not on the roadmap; building it now would be
speculative generality. Revisit when a routine actually needs spatial mapping.

**Done (and sufficient for current routines):**
- `autonomon.world_model.ObstacleWorldModel`: threshold-based fusion of ultrasonic
  (`obstacle_ahead`) and grayscale (`cliff_detected`) into a small boolean state
- Delta-based `WorldStateUpdate` emission; first observation emitted as a baseline
  so the planner always has an initial state

**Deferred (build only when a consumer exists):**
- `OccupancyWorldModel`: spatial occupancy grid (not just boolean flags)
- Configurable state decay (obstacles age out after N seconds without new readings)

---

### Phase 4 — Rule-Based Planner (rule-table / TOML)

**Status:** ⏸️ **Deferred (speculative — no consumer).** `AvoidancePlanner` already
covers `explore`, and `follow-user` uses a dedicated pursuit planner, not a rule
table. A generic TOML-loadable rule engine is config infrastructure for a second
rule-based behaviour that does not yet exist. Revisit when one does.

**Done (and sufficient for current routines):**
- `autonomon.planning.AvoidancePlanner`: two rules (avoid on obstacle/cliff →
  stop+reverse+steer; otherwise cruise forward); debounced on the selected strategy

**Deferred (build only when a second rule-based routine exists):**
- `RulePlanner`: evaluates world state against an ordered rule table, loadable from
  TOML (`{"condition": {...}, "actions": [...], "priority": 0}`)

---

### Phase 5 — Vehicle Action Executor

**Goal:** Execute `ActionPlan` sequences against the nomothetic REST API.

**Done (minimal slice):**
- `autonomon.action.VehicleAction`: executes `ActionPlan.actions` in priority order
- Maps `drive`/`steer`/`stop` to `POST /api/drive`, `/api/steer`, `/api/hat/motor/stop`
- Injected httpx async client (device JWT per ADR-002); emits `ActionResult` per
  action with success/error; best-effort telemetry seam via an optional results queue
- Absorbs transient HTTP/timeout errors without stopping the layer

**Done (full version):**
- Configurable retry with exponential backoff on transient errors (timeout /
  connection / 5xx); a 4xx is not retried; a 2xx with an unparseable body is not retried
- Safety-stop: if a `drive`/`steer` still fails to reach the device after retries, a
  best-effort `POST /api/hat/motor/stop` is issued before recording the failed result

---

### Phase 6 — Routine Registry (`explore` as first entry)

**Reshaped from "Migrate nomon_explore to Layered Framework."** The `nomon_explore`
monolith no longer exists on disk, so there is nothing to migrate. Instead of
restoring a standalone package, we generalise into a **routine registry**: each
routine is a named factory that wires a `Pipeline` from layer configs, and
`explore` becomes one registry entry. The integration test's `_build_pipeline()`
helper is already the `explore` factory in embryo. See ADR-003 for the decision
and the "Routines" section of `architecture.md` for the design.

> **Naming note:** an `autonomon` *routine* (host-side cognitive pipeline in a
> plugin process) is deliberately **distinct** from nomothetic's HAT-level
> `start_routine` IPC method / `POST /api/routine/start` (firmware obstacle
> avoidance inside nomopractic). They share the word and the example name
> `explore` but are different execution models. Docs say "autonomy routine" vs
> "HAT routine" to keep them apart.

**Goal:** Introduce a routine registry and the single generic plugin that runs
any routine by name; ship `explore` as the first entry — pure wiring of existing
Phase 2–5 layers.

**Deliverables:**
- `autonomon.routines` module: a registry mapping routine name → factory, plus
  the built-in `explore` factory wiring `Perceptron.ultrasonic` (+ optional
  `Perceptron.grayscale` via `FanInSlot`), `ObstacleWorldModel`,
  `AvoidancePlanner`, and `VehicleAction`
- `explore` factory accepts `(client, device_id, params)`; params map to layer
  constructor args (`obstacle_threshold_cm`, `forward_speed_pct`,
  `turn_angle_deg`, `cliff_threshold`)
- A single plugin CLI entry point (e.g. `nomon-autonomon`) that reads the routine
  name from `NOMON_PLUGIN_PARAMS`, looks it up in the registry, builds the
  `Pipeline`, runs it, and emits the existing NDJSON lifecycle events
- `nomon_manifest` listing available routine names and the union of their param
  schemas, discoverable by `nomothetic AutonomyPluginManager` as one manifest
- `tests/test_pipeline_integration.py` updated to build `explore` via the
  registry rather than its private `_build_pipeline()` helper
- ✅ Full-pipeline integration test against a mock device already landed
  (`tests/test_pipeline_integration.py`, Phase 3–5 vertical slice)
- The empty `nomon_explore/` scaffolding directory is removed

---

### Phase 6b — `follow-user` Routine

**Goal:** Add a second routine that proves the registry generalises beyond
wiring — it requires net-new layer implementations.

**Reuses unchanged:** `VehicleAction` (the action layer is target-agnostic).

**Net-new (the cost of a pursuit behaviour):**
- A **vision perception implementation** — a new perception layer that polls
  *raw* camera frames from nomothetic and runs person detection **inside
  autonomon**, emitting target bearing/range. Per the brain principle above, the
  detector is an *autonomon* dependency — nomothetic only serves the raw frame.
  Stack: **ONNX Runtime + a YOLOv8n model** (person class), behind a swappable
  `Detector` abstraction so CI injects a fake detector and the model can be
  replaced. Runtime deps: `onnxruntime`, `numpy`, `pillow` (a `[vision]` extra);
  `ultralytics` (AGPL) is used only offline to export the ONNX model, never at
  runtime.
- A **target world model** — a new `WorldModelBase` impl tracking the target's
  relative position over time (EMA smoothing, lost-target timeout), not a boolean
  obstacle flag
- A **pursuit planner** — a new `PlannerBase` impl that closes distance to a
  moving target (steer toward bearing, hold a `target_distance_cm` standoff),
  rather than avoiding obstacles
- `follow-user` factory + registry entry; params: `target_distance_cm`,
  `max_speed_pct`, `confidence_threshold`, `camera_hfov_deg`,
  `lost_target_timeout_s`, `model_path`

**Camera frame source.** `POST /api/camera/capture` writes a JPEG to disk and
returns metadata only — it does **not** return frame bytes. The only existing
frame-bytes path is the MJPEG stream. So this phase adds one small raw-input
endpoint to nomothetic: **`GET /api/camera/frame`** → `image/jpeg`, reusing the
camera's existing in-memory JPEG capture. This is ADR-004-legal (a *raw input*,
no interpretation) — it is the single exception to "no new nomothetic endpoint",
chosen over multipart-MJPEG parsing because it fits the perception poll model
exactly. The autonomon process may run on-device or on a remote host; the
boundary contract is unchanged.

---

### Phase 6c — Device Deployment & Integration Testing

**Goal:** Enable autonomous capability deployment to devices and add CI checks
for end-to-end autonomy testing.

**Deliverables:**
- ✅ Plugin auth handshake (nomothetic ADR-019): Ed25519 challenge-response so the
  plugin obtains a device JWT without any token on disk. `autonomon.plugin_auth`
  (keygen, signing, `PluginTokenAuth` with refresh-on-401, deploy CLI) +
  nomothetic `plugin_auth` / `plugin_auth_routes` (`register` localhost-only,
  `challenge`, `token`). The CLI prefers `NOMON_PLUGIN_KEY` over a static
  `NOMON_PLUGIN_TOKEN`.
- ✅ `scripts/deploy.sh` — deploy from latest semver tag or `--local` source tree
  via rsync; installs autonomon into its **own** venv (separate from nomothetic per
  ADR-005); optional test run;
  verifies CLI + manifest; **generates the on-device key, writes the plugin env
  file (no token), and registers the public key over loopback**; reloads
  `nomothetic-api.service` if running; rollback on failure. Same interface
  pattern as nomothetic/nomopractic workspace scripts.
- ✅ `.github/workflows/ci.yml` — `check` job (ruff + black + mypy + pytest with
  coverage) on push/PR to main; `release` job creates a GitHub Release on `v*` tags.
- ✅ Device integration test (`tests/test_integration_subprocess.py`): spins up a mock
  nomothetic over loopback, runs the `nomon-autonomon` CLI as a subprocess for both
  `explore` and `follow-user` (fake-detector hook), and asserts sensor/frame reads →
  drive/steer/stop commands reach the mock. Subprocess-level, CI-suitable.
- ✅ `README.md` deployment guide: dev setup, running a routine, the vision model fetch,
  and `scripts/deploy.sh` / `make deploy` usage.

**Rationale:** Phase 6 is complete but exists only in the repo; Phase 6b/7 will
add more routines. Without automated deployment and CI, we cannot verify that
the autonomy stack works end-to-end on actual hardware, and regressions can
silently break the pipeline. Deployment scripts let developers/QA test quickly;
CI ensures the contract holds across the fleet.

---

### Phase 7 — Autonomy Telemetry to ArcadeDB

**Status:** ⏸️ **Deferred (needs more design).** The open question is the
**device→central transport and auth**: central nomothetic routes authenticate a
user JWT (`jwt_required`), which a headless autonomy plugin does not have. Options
include a fleet-scoped device identity, a relay through the device API, or a
dedicated ingestion token — this should be settled before building. The
`VehicleAction` results-queue seam and the `cli.py` `report()` hook already exist,
so picking this up later is additive.

**Goal:** Persist autonomy run records and lifecycle events to the central ArcadeDB.

**Deliverables (when resumed):**
- `autonomon.telemetry.AutonomyPublisher`: batches `ActionResult` and lifecycle events; sends to nomothetic `/api/telemetry/autonomy`
- `nomographic` migration: `AutonomyRun` and `AutonomyEvent` vertex types in `central/`
- `nomothetic` endpoint: `POST /api/telemetry/autonomy` (central mode only)
