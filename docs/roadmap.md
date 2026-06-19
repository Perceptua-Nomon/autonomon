# autonomon — Development Roadmap

## Status Summary

| Phase | Name | Status |
|-------|------|--------|
| 0 | nomon_explore plugin (pre-framework) | ✅ Complete |
| 1 | Core framework (`autonomon` package) | ✅ Complete |
| 2 | Perception implementations (ultrasonic, grayscale, battery) | ✅ Complete |
| 3 | Occupancy-grid world model | 🟡 Minimal slice (`ObstacleWorldModel`); occupancy grid pending |
| 4 | Rule-based planner | 🟡 Minimal slice (`AvoidancePlanner`); rule-table/TOML pending |
| 5 | Vehicle action executor | 🟡 Minimal slice (`VehicleAction`); retry/backoff + safety-stop pending |
| 6 | Routine registry (`explore` as first entry) | ✅ Complete |
| 6b | `follow-user` routine (new perception/world-model/planner) | 🔲 Planned |
| 6c | Device deployment & integration testing | 🟡 In progress (scripts + CI done; integration test pending) |
| 7 | Autonomy telemetry to ArcadeDB | 🔲 Planned |

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
- Runtime hot-swap (`autonomon.slot.LayerSlot`, `SlotState`) and multi-source fan-in (`autonomon.fan_in.FanInSlot`, `MergeStrategy` — `PASS_THROUGH` / `ARBITRATE`)
- pytest suite covering pipeline wiring, back-pressure, hot-swap, and fan-in
- `docs/architecture.md`, `docs/roadmap.md`, `docs/adr/001-layered-architecture.md`, `docs/adr/002-rest-api-client-pattern.md`

---

### Phase 2 — Perception Implementations

**Status:** ✅ Complete

**Delivered:**
- `autonomon.perception.Perceptron`: a single configurable perception class declaring `sensor_type` + `endpoint` + `interpreter`, sharing all polling, timeout, error-handling, and stop logic
- Built-in sensors defined in one declarative `_SENSOR_SPECS` table, exposed via named constructors:
  - `Perceptron.ultrasonic` → `GET /api/sensor/ultrasonic`; emits `data={"distance_cm": float | None}`
  - `Perceptron.grayscale` → `GET /api/sensor/grayscale/normalized`; emits `data={"channels": [...], "normalized": [...]}`
  - `Perceptron.battery` → `GET /api/hat/battery`; emits `data={"voltage_v": float}`
- Configurable poll interval and per-request timeout per instance (battery defaults to 30 s; others 0.1 s)
- Custom sensors via the general `Perceptron(...)` constructor with a user-supplied interpreter
- Transient HTTP errors, request errors, and timeouts are absorbed; the poll loop continues
- `AsyncMock` / `MagicMock` httpx fixtures for device-free testing (per ADR-002)

**Design note:** the original plan called for one class per sensor (`UltrasonicPerception`, `GrayscalePerception`, `BatteryPerception`); this was consolidated into a single configurable `Perceptron` so sensor differences are data (endpoint + interpreter + interval), not duplicated classes.

---

## Planned Phases

### Phase 3 — Occupancy-Grid World Model

**Goal:** Fuse sensor events into a spatial world state.

**Done (minimal slice):**
- `autonomon.world_model.ObstacleWorldModel`: threshold-based fusion of ultrasonic
  (`obstacle_ahead`) and grayscale (`cliff_detected`) into a small boolean state
- Delta-based `WorldStateUpdate` emission; first observation emitted as a baseline
  so the planner always has an initial state

**Remaining (full version):**
- `OccupancyWorldModel`: spatial occupancy grid (not just boolean flags)
- Battery state tracking
- Configurable state decay (obstacles age out after N seconds without new readings)
- Serialisable to JSON for logging and telemetry

---

### Phase 4 — Rule-Based Planner

**Goal:** Deterministic planner driven by a priority-ordered rule set.

**Done (minimal slice):**
- `autonomon.planning.AvoidancePlanner`: two hard-coded rules (avoid on
  obstacle/cliff → stop+reverse+steer; otherwise cruise forward)
- Debounce: emits a new `ActionPlan` only when the selected strategy changes

**Remaining (full version):**
- `RulePlanner`: evaluates world state against an ordered rule table
- Rule format: `{"condition": {...}, "actions": [...], "priority": 0}`
- Rules loadable from TOML or passed at construction
- Test coverage for all standard nomon_explore avoidance cases

---

### Phase 5 — Vehicle Action Executor

**Goal:** Execute `ActionPlan` sequences against the nomothetic REST API.

**Done (minimal slice):**
- `autonomon.action.VehicleAction`: executes `ActionPlan.actions` in priority order
- Maps `drive`/`steer`/`stop` to `POST /api/drive`, `/api/steer`, `/api/hat/motor/stop`
- Injected httpx async client (device JWT per ADR-002); emits `ActionResult` per
  action with success/error; best-effort telemetry seam via an optional results queue
- Absorbs transient HTTP/timeout errors without stopping the layer

**Remaining (full version):**
- Configurable retry with exponential backoff on transient errors
- Safety: if nomothetic returns 5xx, emit a stop action before propagating the error

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
- A **vision perception implementation** — a new perception layer that pulls
  *raw* camera frames from nomothetic (the existing `POST /api/camera/capture`
  still or the MJPEG stream) and runs person/target detection **inside
  autonomon**, emitting target bearing/range. Per the brain principle above,
  the detection model (e.g. OpenCV or a TFLite person detector) is an
  *autonomon* dependency — nomothetic only serves the raw frame.
- A **target world model** — a new `WorldModelBase` impl tracking the target's
  relative position over time (not a boolean obstacle flag)
- A **pursuit planner** — a new `PlannerBase` impl that closes distance to a
  moving target (drive toward bearing, hold a `target_distance_cm` standoff),
  rather than avoiding obstacles
- `follow-user` factory + registry entry; params: `target_distance_cm`,
  `max_speed_pct`, plus a target-source / model selector

**Dependency note:** nomothetic already exposes the only thing it needs to —
raw camera access (`/api/camera/capture`, MJPEG stream). **No new nomothetic
endpoint is required**; the detection model and all interpretation live in the
autonomon vision perception layer. The open decision is *where the autonomon
process runs* for this routine: on-device (lower latency, heavier CPU load on
the Pi Zero 2W) vs. on a remote host (offload detection, adds frame-transfer
bandwidth). Both honour the same boundary contract.

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
  via rsync; installs autonomon into nomothetic's `.venv`; optional test run;
  verifies CLI + manifest; **generates the on-device key, writes the plugin env
  file (no token), and registers the public key over loopback**; reloads
  `nomothetic-api.service` if running; rollback on failure. Same interface
  pattern as nomothetic/nomopractic workspace scripts.
- ✅ `.github/workflows/ci.yml` — `check` job (ruff + black + mypy + pytest with
  coverage) on push/PR to main; `release` job creates a GitHub Release on `v*` tags.
- 🔲 Device integration test (CI job): spin up mock nomothetic, run `nomon-autonomon`
  with the `explore` routine against a mock device, assert sensor reads → stop/drive
  commands reach the mock. Extends the existing `test_pipeline_integration.py`
  approach into a subprocess-level test suitable for CI.
- 🔲 README or deployment guide documenting the deploy process for developers.

**Rationale:** Phase 6 is complete but exists only in the repo; Phase 6b/7 will
add more routines. Without automated deployment and CI, we cannot verify that
the autonomy stack works end-to-end on actual hardware, and regressions can
silently break the pipeline. Deployment scripts let developers/QA test quickly;
CI ensures the contract holds across the fleet.

---

### Phase 7 — Autonomy Telemetry to ArcadeDB

**Goal:** Persist autonomy run records and lifecycle events to the central ArcadeDB.

**Deliverables:**
- `autonomon.telemetry.AutonomyPublisher`: batches `ActionResult` and lifecycle events; sends to nomothetic `/api/telemetry/autonomy`
- `nomographic` migration: `AutonomyRun` and `AutonomyEvent` vertex types in `central/`
- `nomothetic` endpoint: `POST /api/telemetry/autonomy` (central mode only)
