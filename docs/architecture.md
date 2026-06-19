# autonomon — Architecture

## System Overview

`autonomon` is a mono-repo of Python packages that add autonomous capabilities to the nomon fleet. Each device running `nomothetic` can host one or more autonomy plugins; each plugin drives the device through a four-layer cognitive pipeline.

> **Architecture principle — autonomon is the brain (ADR-004).** All input
> processing and modeling lives in autonomon: sensor fusion, computer vision,
> person/object detection, world modeling, and planning. The pipeline is
> self-contained between two boundaries — Perception ingests *raw* inputs,
> Action emits *action* commands — and everything in between is autonomon's
> responsibility. **nomothetic is a thin hardware gateway:** it serves raw
> inputs (sensor reads, raw camera frames) and accepts action outputs, and
> performs no perception, detection, or modeling. A new capability is added as
> an autonomon layer over raw nomothetic I/O, never as a processing endpoint on
> nomothetic. This is the mirror of the nomopractic/nomothetic hard rule: no
> hardware registers below nomothetic, no autonomy cognition above it.

```
┌──────────────────────────────────────────────────────────────────────┐
│  nomothetic REST API  (HTTPS :8443 on-device)                        │
│  Sensor reads: ultrasonic, grayscale, battery, encoder               │
│  Actuator writes: motors, servos, audio                              │
└──────────────┬──────────────────────────────┬────────────────────────┘
               │ poll sensor data             │ execute commands
               ▼                              │
┌──────────────────────────────┐              │
│  Perception Layer            │              │
│  autonomon.perception        │              │
│                              │              │
│  Polls nomothetic endpoints  │              │
│  Normalises raw sensor data  │              │
│  Emits: PerceptionEvent      │              │
└──────────────┬───────────────┘              │
               │ PerceptionEvent JSON         │
               ▼                              │
┌──────────────────────────────┐              │
│  World Model Layer           │              │
│  autonomon.world_model       │              │
│                              │              │
│  Fuses sensor events         │              │
│  Maintains current state     │              │
│  Emits: WorldStateUpdate     │              │
└──────────────┬───────────────┘              │
               │ WorldStateUpdate JSON        │
               ▼                              │
┌──────────────────────────────┐              │
│  Planning Layer              │              │
│  autonomon.planning          │              │
│                              │              │
│  Evaluates world state       │              │
│  Selects action strategies   │              │
│  Emits: ActionPlan           │              │
└──────────────┬───────────────┘              │
               │ ActionPlan JSON              │
               ▼                              │
┌──────────────────────────────┐              │
│  Action Layer                │──────────────┘
│  autonomon.action            │
│                              │
│  Executes plans via httpx    │
│  Calls nomothetic REST API   │
│  Emits: ActionResult         │
└──────────────────────────────┘
```

---

## Packages

### `autonomon` (core framework)

Provides the four-layer base classes, JSON message types, and the pipeline runner that wires layers together. Autonomy plugins import from here.

```
autonomon/
├── pyproject.toml
└── src/autonomon/
    ├── __init__.py       Exports all public surface
    ├── messages.py       PerceptionEvent, WorldStateUpdate, ActionPlan, ActionResult
    ├── pipeline.py       Pipeline — connects layers with asyncio queues
    ├── slot.py           LayerSlot, SlotState — runtime hot-swap of one layer
    ├── fan_in.py         FanInSlot, MergeStrategy — N impls at one position
    ├── perception/
    │   ├── __init__.py
    │   ├── base.py       PerceptionBase abstract class
    │   └── perceptron.py Perceptron — configurable single-sensor implementation
    ├── world_model/
    │   ├── __init__.py
    │   ├── base.py       WorldModelBase abstract class
    │   └── obstacle.py   ObstacleWorldModel — threshold obstacle/cliff fusion
    ├── planning/
    │   ├── __init__.py
    │   ├── base.py       PlannerBase abstract class
    │   └── avoidance.py  AvoidancePlanner — rule-based avoid/cruise selection
    └── action/
        ├── __init__.py
        ├── base.py       ActionBase abstract class
        └── vehicle.py    VehicleAction — executes plans via the vehicle API
```

### Routines (the `explore` behaviour)

`explore` (obstacle-avoidance wandering) is delivered as a **routine** — a
registry entry that wires the 4-layer pipeline — not as a standalone package.
The Phase-0 `nomon_explore` monolith no longer exists on disk (only empty
scaffolding remains). The `explore` routine uses:
- Perception: `Perceptron.ultrasonic` (+ optional `grayscale`) polling the
  nomothetic sensor endpoints
- World model: `ObstacleWorldModel` — detects obstacle/cliff conditions
- Planning: `AvoidancePlanner` — chooses forward / backup+turn / stop strategy
- Action: `VehicleAction` — calls `/api/drive`, `/api/steer`,
  `/api/hat/motor/stop`

See the **Routines** section below and ADR-003 for the registry design.

---

## Perception Implementations

The concrete Perception layer is a single configurable class, `Perceptron`
(`autonomon.perception.perceptron`), rather than one class per sensor. A
`Perceptron` instance declares a `sensor_type`, an `endpoint`, and an
`interpreter` callable that maps the raw JSON response body to the
`PerceptionEvent.data` payload; all polling, per-request timeout, transient-error
handling, and stop logic is shared in the one implementation.

Per-sensor knowledge for the built-in Robot HAT V4 sensors lives in one
declarative table (`_SENSOR_SPECS`), each row colocating the endpoint,
interpreter, and default poll interval. Named class-method constructors are
thin factories over that table:

```python
import httpx
from autonomon import Perceptron, Pipeline

async with httpx.AsyncClient(
    base_url=device_url, verify=False,
    headers={"Authorization": f"Bearer {token}"}, timeout=5.0,
) as client:
    pipeline = Pipeline(
        perception=Perceptron.ultrasonic(client, device_id),   # /api/sensor/ultrasonic, 0.1 s
        world_model=MyWorldModel(),
        planner=MyPlanner(config),
        action=MyAction(client, device_id),
    )
    await pipeline.run()
```

| Constructor | Endpoint | `data` payload | Default poll |
|-------------|----------|----------------|--------------|
| `Perceptron.ultrasonic` | `GET /api/sensor/ultrasonic` | `{"distance_cm": float \| None}` | 0.1 s |
| `Perceptron.grayscale` | `GET /api/sensor/grayscale/normalized` | `{"channels": [...], "normalized": [...]}` | 0.1 s |
| `Perceptron.battery` | `GET /api/hat/battery` | `{"voltage_v": float}` | 30 s |

For any other source, construct `Perceptron(client, device_id, sensor_type=...,
endpoint=..., interpreter=...)` directly. The HTTP client (pre-configured with
base URL, bearer token, and `verify=False`) is injected per ADR-002; the layer
holds no auth knowledge.

## World Model, Planning, and Action Implementations

A minimal concrete implementation of each remaining layer closes the autonomy
loop end-to-end (the heavier occupancy-grid / rule-table / retry versions are
tracked in the roadmap):

- **`ObstacleWorldModel`** (`world_model.obstacle`) — threshold fusion of
  ultrasonic (`obstacle_ahead`) and grayscale (`cliff_detected`) into a small
  boolean state. Emits the first observation as a baseline (empty `delta`) so the
  planner always has an initial state, then delta-based on change.
- **`AvoidancePlanner`** (`planning.avoidance`) — two rules: obstacle/cliff →
  stop + reverse + steer; otherwise cruise forward. Debounces on the selected
  strategy, emitting a new `ActionPlan` only when it changes.
- **`VehicleAction`** (`action.vehicle`) — executes plan actions in priority
  order, mapping `drive`/`steer`/`stop` to `POST /api/drive`, `/api/steer`,
  `/api/hat/motor/stop`. Injected httpx client per ADR-002; emits an
  `ActionResult` per action (best-effort onto an optional telemetry queue — the
  Phase 7 seam); transient HTTP/timeout errors are recorded, not fatal.

The full loop is exercised without hardware by
`tests/test_pipeline_integration.py`: a near ultrasonic reading propagates
Perception → World Model → Planning → Action and produces a stop/reverse command
to the (mock) device.

---

## Message Types

All messages are JSON-serialisable dataclasses passed through `asyncio.Queue[dict]` between layers within a process. Can also be serialised to NDJSON for inter-process pipelines.

### `PerceptionEvent`

Emitted by the Perception layer for each sensor reading.

```json
{
  "type": "perception_event",
  "timestamp": "2026-06-17T14:00:00.000Z",
  "device_id": "nomon-ab12",
  "sensor_type": "ultrasonic",
  "data": {
    "distance_cm": 18.4
  }
}
```

### `WorldStateUpdate`

Emitted by the World Model layer when state changes.

```json
{
  "type": "world_state_update",
  "timestamp": "2026-06-17T14:00:00.001Z",
  "device_id": "nomon-ab12",
  "state": {
    "obstacle_ahead": true,
    "cliff_left": false,
    "battery_v": 11.8
  },
  "delta": {
    "obstacle_ahead": true
  }
}
```

### `ActionPlan`

Emitted by the Planning layer when a new plan is selected.

```json
{
  "type": "action_plan",
  "timestamp": "2026-06-17T14:00:00.002Z",
  "device_id": "nomon-ab12",
  "plan_id": "avoid-001",
  "actions": [
    {"method": "stop",          "params": {},                    "priority": 0},
    {"method": "drive",         "params": {"speed": -30},        "priority": 1},
    {"method": "steer",         "params": {"angle": 135},        "priority": 2},
    {"method": "drive",         "params": {"speed": 30},         "priority": 3}
  ]
}
```

### `ActionResult`

Emitted by the Action layer after executing each action.

```json
{
  "type": "action_result",
  "timestamp": "2026-06-17T14:00:00.050Z",
  "device_id": "nomon-ab12",
  "plan_id": "avoid-001",
  "action": {"method": "stop", "params": {}},
  "success": true,
  "data": {},
  "error": null
}
```

---

## Inter-Layer Communication and Hot-Swap

Layers communicate via `asyncio.Queue[dict]` populated with JSON-serialisable dicts. The `Pipeline` class wires up the queues and starts each layer as an asyncio task:

```python
pipeline = Pipeline(
    perception=MyPerception(device_url, token),
    world_model=MyWorldModel(),
    planner=MyPlanner(config),
    action=MyAction(device_url, token),
)
await pipeline.run()
```

Each queue has a bounded capacity (default 32) to create back-pressure: if the action layer falls behind, the planner pauses; if the planner pauses, the world model pauses; if the world model pauses, perception slows its polling.

### Hot-Swap: replacing one layer at runtime

Each layer position is managed by a `LayerSlot` that owns the asyncio Task. The queues are attached to the slot, not the task — so swapping the implementation keeps the queues intact and in-flight messages are never lost.

```python
# Swap YOLO for a different vision model mid-run
await pipeline.swap_layer("perception", new_vision_model)
```

The state machine: `stop()` on old impl → await its task (drain_timeout, default 2 s) → create new task for new impl on the **same queues**. The other three layers never pause.

### Multi-source fan-in: N implementations at one position

Pass a `FanInSlot` instead of a single implementation to run N concurrent sources at one layer position.

**Perception fan-in (PASS_THROUGH):** Both sources write to the same downstream queue. Back-pressure applies to all sources: when the queue fills, all sources pause.

```python
from autonomon import FanInSlot, MergeStrategy

pipeline = Pipeline(
    perception=FanInSlot(
        "perception",
        [YoloPerception(client, device_id), Perceptron.ultrasonic(client, device_id)],
        MergeStrategy.PASS_THROUGH,
    ),
    world_model=MyWorldModel(),
    planner=MyPlanner(config),
    action=MyAction(client, device_id),
)
```

**Planning fan-in (ARBITRATE):** A dispatcher copies each `WorldStateUpdate` to both planners; each planner writes its plan to an internal arbiter queue; within a configurable window (default 50 ms), an arbiter function selects the best plan and forwards it to the Action layer.

```python
pipeline = Pipeline(
    perception=MyPerception(device_url, token),
    world_model=MyWorldModel(),
    planner=FanInSlot(
        "planner",
        [RulePlanner(rules), LLMPlanner(model)],
        MergeStrategy.ARBITRATE,
        arbiter=pick_highest_confidence_plan,
        arbitration_window_ms=50,
    ),
    action=MyAction(device_url, token),
)
```

**Dynamic add/remove:** Sources can be added or removed from a running `FanInSlot` without restarting the pipeline.

```python
fan_in_slot = pipeline._slots["perception"]  # FanInSlot
await fan_in_slot.add_impl(new_sensor)
await fan_in_slot.remove_impl(old_sensor)
```

---

## Layer Contract

Each layer is an `asyncio` coroutine that reads from `queue_in`, processes, and writes to `queue_out`. The `Pipeline` creates and injects both queues.

```python
class PerceptionBase(ABC):
    async def run(self, queue_out: asyncio.Queue[dict]) -> None: ...
    async def stop(self) -> None: ...

class WorldModelBase(ABC):
    async def run(self, queue_in: asyncio.Queue[dict], queue_out: asyncio.Queue[dict]) -> None: ...
    async def stop(self) -> None: ...

class PlannerBase(ABC):
    async def run(self, queue_in: asyncio.Queue[dict], queue_out: asyncio.Queue[dict]) -> None: ...
    async def stop(self) -> None: ...

class ActionBase(ABC):
    async def run(self, queue_in: asyncio.Queue[dict]) -> None: ...
    async def stop(self) -> None: ...
```

---

## Plugin System

Each plugin in the autonomon repo follows this pattern:

1. **`pyproject.toml`**: entry point `nomon-<name> = "<package>.cli:main"`
2. **`__init__.py`**: exports a `nomon_manifest` dict describing the plugin (name, version, required capabilities, params schema)
3. **`cli.py`**: reads `NOMON_DEVICE_URL`, `NOMON_PLUGIN_TOKEN`, `NOMON_PLUGIN_PARAMS` from env; emits NDJSON lifecycle events to stdout; runs the pipeline
4. **`control.py`** (or layer submodules): implements the `autonomon` base classes

The `nomothetic` `AutonomyPluginManager` discovers installed plugins via `nomon_manifest`, launches them as subprocesses, and reads their stdout NDJSON for lifecycle telemetry.

### Lifecycle Events (stdout NDJSON)

```json
{"type": "starting", "data": {}}
{"type": "running",  "data": {"loop_count": 42, "uptime_s": 4.2}}
{"type": "stopping", "data": {"reason": "max_duration_reached"}}
{"type": "error",    "data": {"message": "Connection refused"}}
```

---

## Routines

A **routine** is a named, built-in behaviour that accomplishes one goal by
composing the four autonomy layers into a single `Pipeline`. Routines are the
user-facing catalogue of "what this robot can do autonomously": `explore`
(obstacle-avoidance wandering), `follow-user`, and more over time. See ADR-003
for the decision record.

> **Naming — read this first.** An **autonomy routine** (this section) is a
> host-side cognitive pipeline that runs in a *plugin process* and drives the
> device over the REST API. It is **deliberately distinct** from a **HAT
> routine** — nomothetic's `start_routine` / `stop_routine` IPC methods and
> `POST /api/routine/start` REST endpoints, which command obstacle avoidance
> running *inside the nomopractic firmware*. The two share the word "routine"
> and even the example name `explore`, but they are different execution models
> in different repos. This documentation always qualifies which one it means.

### A routine is a factory that wires a Pipeline

Each routine is a factory function that takes a shared `httpx.AsyncClient`, a
`device_id` (per ADR-002), and a routine-specific params dict, and returns a
fully wired `Pipeline`:

```
build_explore(client, device_id, params)      -> Pipeline   # reuses every existing layer
build_follow_user(client, device_id, params)  -> Pipeline   # needs new perception/world-model/planner
```

The `explore` factory is the production form of the integration test's
`_build_pipeline()` helper (`tests/test_pipeline_integration.py`): it wires
`Perceptron.ultrasonic` (optionally with `Perceptron.grayscale` via a
`FanInSlot`) → `ObstacleWorldModel` → `AvoidancePlanner` → `VehicleAction`.

### Registry

A small registry in the `autonomon.routines` module maps each routine name to
its factory:

```
ROUTINES: dict[str, RoutineFactory] = {
    "explore":     build_explore,
    "follow-user": build_follow_user,
}
get_routine(name) -> RoutineFactory      # raises on unknown name
```

The registry is the catalogue; adding a behaviour is adding one entry (plus any
new layer implementations it needs). No new package, CLI, or manifest is created
per routine.

### How a routine maps to a Pipeline

```
routine name ──► get_routine(name) ──► factory(client, device_id, params)
                                              │
                                              ▼
                          Pipeline(perception=…, world_model=…,
                                   planner=…, action=…)
                                              │
                                              ▼
                                      pipeline.run()
```

The factory's only job is to choose and parameterise the four layer
implementations for that behaviour. Everything downstream — queues,
back-pressure, hot-swap, fan-in, shutdown — is the existing `Pipeline`
machinery, unchanged.

### Parameterisation

Routines are parameterised by a plain params dict (the same dict carried by
`NOMON_PLUGIN_PARAMS`); no new config framework is introduced. The *schema* for
each routine's params is declared in the plugin manifest; *applying* the params
onto layer constructor arguments is the factory's responsibility.

| Routine | Example params | Maps onto |
|---------|----------------|-----------|
| `explore` | `obstacle_threshold_cm`, `forward_speed_pct`, `turn_angle_deg`, `cliff_threshold` | `ObstacleWorldModel` + `AvoidancePlanner` constructor args |
| `follow-user` | `target_distance_cm`, `max_speed_pct`, target-source selector | the new follow-layer constructor args |

### Lifecycle and manifest relationship

There is **one** plugin package and **one** CLI entry point that runs *any*
routine — it is the generic launcher over the registry, not one launcher per
behaviour:

1. The CLI reads the routine name from `NOMON_PLUGIN_PARAMS` (a `routine` /
   `name` key), looks it up via `get_routine`, builds the `Pipeline`, and runs
   it.
2. It emits the same NDJSON lifecycle events (`starting` / `running` /
   `stopping` / `error`) documented under **Plugin System** above.
3. The plugin's `nomon_manifest` advertises the available routine names and the
   union of their param schemas, so `AutonomyPluginManager` discovers the whole
   catalogue from a single manifest rather than N packages.

### Reusable vs net-new per routine

| Layer | `explore` | `follow-user` |
|-------|-----------|---------------|
| Perception | `Perceptron.ultrasonic` (+ `grayscale`) — reused | **new** vision perception impl: pulls raw camera frames from nomothetic and runs person detection *in autonomon* (ADR-004) — not a nomothetic endpoint |
| World Model | `ObstacleWorldModel` — reused | **new** target-position world model |
| Planning | `AvoidancePlanner` — reused | **new** pursuit planner |
| Action | `VehicleAction` — reused | `VehicleAction` — reused |

`explore` is pure wiring of what already exists. `follow-user` is the proof the
registry is worth building: it reuses the action layer and the whole `Pipeline`
runtime, but swaps in three new layer implementations — exactly the
per-slot extensibility ADR-001 promised.

---

## Nomothetic API Surface Used

This surface is a **raw-I/O boundary** (ADR-004): autonomon consumes raw inputs
and emits actuator commands. nomothetic does no interpretation of this data —
all fusion, vision, detection, and modeling happens in the autonomon layers
above. The surface grows only when a *new raw input or actuator* is exposed,
never to add processing of data already served.

| Layer | Direction | Endpoints called |
|-------|-----------|-----------------|
| Perception | raw in | `GET /api/sensor/ultrasonic`, `GET /api/sensor/grayscale/normalized`, `GET /api/hat/battery` |
| Action | actions out | `POST /api/drive`, `POST /api/steer`, `POST /api/hat/motor/stop` |

**Raw inputs available but not yet consumed:** `POST /api/camera/capture` and
the MJPEG stream serve raw camera frames. Per ADR-004 the `follow-user` routine
(Phase 6b) consumes these frames and runs person detection **inside an autonomon
vision perception layer** — it does *not* call a nomothetic detection endpoint,
because none exists or should.

All calls use device-scoped JWT (`NOMON_PLUGIN_TOKEN`) in the `Authorization: Bearer` header. TLS verification is skipped for self-signed device certs (`verify=False` on httpx, documented in ADR-001 of nomothetic).

---

## Testing Approach

- All layers are testable without a real device: `PerceptionBase` implementations accept a mock httpx client; queue I/O is inspectable in tests
- `asyncio.Queue` makes unit tests straightforward: push a message in, assert on the message out
- Integration tests run the full pipeline with a mock nomothetic server
- No Pi hardware required for any test

---

## Related Repositories

| Repo | Role |
|------|------|
| `nomothetic` | Device REST API and HAT IPC client — autonomon calls this |
| `nomopractic` | Rust HAT daemon — data ultimately originates here |
| `nomotactic` | Mobile app — may display autonomy status from nomothetic |
| `nomographic` | ArcadeDB schemas — autonomy telemetry may be persisted here |
