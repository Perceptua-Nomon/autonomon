# autonomon — Development Roadmap

## Status Summary

| Phase | Name | Status |
|-------|------|--------|
| 0 | nomon_explore plugin (pre-framework) | ✅ Complete |
| 1 | Core framework (`autonomon` package) | 🔄 In Progress |
| 2 | Perception implementations (ultrasonic, grayscale, battery) | 🔲 Planned |
| 3 | Occupancy-grid world model | 🔲 Planned |
| 4 | Rule-based planner | 🔲 Planned |
| 5 | Vehicle action executor | 🔲 Planned |
| 6 | Migrate nomon_explore to layered framework | 🔲 Planned |
| 7 | Autonomy telemetry to ArcadeDB | 🔲 Planned |

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

**Architecture note:** Phase 0 embeds all logic in a single-package monolith. Phases 1–5 build the layered framework that Phase 6 uses to refactor nomon_explore.

---

## Planned Phases

### Phase 1 — Core Framework (`autonomon` package)

**Goal:** Define the 4-layer architecture, message types, and pipeline runner. No sensor-specific code; only base classes and contracts.

**Deliverables:**
- `autonomon` package at `autonomon/`
- `autonomon.messages`: `PerceptionEvent`, `WorldStateUpdate`, `ActionPlan`, `ActionResult` dataclasses with `to_dict()` / `from_dict()`
- `autonomon.perception.PerceptionBase`: abstract async `run(queue_out)` + `stop()`
- `autonomon.world_model.WorldModelBase`: abstract async `run(queue_in, queue_out)` + `stop()`
- `autonomon.planning.PlannerBase`: abstract async `run(queue_in, queue_out)` + `stop()`
- `autonomon.action.ActionBase`: abstract async `run(queue_in)` + `stop()`
- `autonomon.pipeline.Pipeline`: wires up layers with bounded asyncio queues, graceful shutdown
- Full pytest suite for pipeline wiring and back-pressure behaviour
- `docs/architecture.md`, `docs/roadmap.md`, `docs/adr/001-layered-architecture.md`

---

### Phase 2 — Perception Implementations

**Goal:** Concrete perception classes for the sensors available on the Robot HAT V4.

**Deliverables:**
- `autonomon.perception.UltrasonicPerception`: polls `/api/hat/ultrasonic`; emits `PerceptionEvent(sensor_type="ultrasonic")`
- `autonomon.perception.GrayscalePerception`: polls `/api/hat/grayscale`; emits `PerceptionEvent(sensor_type="grayscale")`
- `autonomon.perception.BatteryPerception`: polls `/api/hat/battery`; emits `PerceptionEvent(sensor_type="battery")`
- Configurable poll intervals per sensor type
- Mock httpx client fixture for testing without a device

---

### Phase 3 — Occupancy-Grid World Model

**Goal:** Fuse sensor events into a spatial world state.

**Deliverables:**
- `autonomon.world_model.OccupancyWorldModel`: maintains obstacle/cliff/battery state
- Emits `WorldStateUpdate` only on state change (delta-based)
- Configurable state decay (obstacles age out after N seconds without new readings)
- Serialisable to JSON for logging and telemetry

---

### Phase 4 — Rule-Based Planner

**Goal:** Deterministic planner driven by a priority-ordered rule set.

**Deliverables:**
- `autonomon.planning.RulePlanner`: evaluates world state against an ordered rule table
- Rule format: `{"condition": {...}, "actions": [...], "priority": 0}`
- Rules loadable from TOML or passed at construction
- Debounce: only emits a new `ActionPlan` when the selected plan changes
- Test coverage for all standard nomon_explore avoidance cases

---

### Phase 5 — Vehicle Action Executor

**Goal:** Execute `ActionPlan` sequences against the nomothetic REST API.

**Deliverables:**
- `autonomon.action.VehicleAction`: executes `ActionPlan.actions` in priority order
- Uses httpx async client; respects device JWT from constructor
- Emits `ActionResult` per action with success/error
- Handles transient HTTP errors with configurable retry (exponential backoff)
- Safety: if nomothetic returns 5xx, emits a stop action before propagating the error

---

### Phase 6 — Migrate nomon_explore to Layered Framework

**Goal:** Rewrite nomon_explore as thin wiring of Phase 2–5 components, eliminating duplicate httpx and control logic.

**Deliverables:**
- `nomon_explore/control.py` replaced by `Pipeline(UltrasonicPerception, GrayscalePerception, OccupancyWorldModel, RulePlanner, VehicleAction)`
- `nomon_explore/client.py` removed; httpx encapsulated in `autonomon.action.VehicleAction`
- All existing nomon_explore tests pass unchanged
- New integration test running the full pipeline with mock device

---

### Phase 7 — Autonomy Telemetry to ArcadeDB

**Goal:** Persist autonomy run records and lifecycle events to the central ArcadeDB.

**Deliverables:**
- `autonomon.telemetry.AutonomyPublisher`: batches `ActionResult` and lifecycle events; sends to nomothetic `/api/telemetry/autonomy`
- `nomographic` migration: `AutonomyRun` and `AutonomyEvent` vertex types in `central/`
- `nomothetic` endpoint: `POST /api/telemetry/autonomy` (central mode only)
