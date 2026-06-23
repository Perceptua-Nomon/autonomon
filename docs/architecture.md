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
    ├── slot.py           LayerSlot, SlotState — owns one layer's task + queues
    ├── fan_in.py         FanInSlot — multi-source perception fan-in (pass-through)
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
| `Perceptron.grayscale` | `GET /api/sensor/grayscale` | `{"channels": [...], "values": [...]}` (raw ADC) | 0.1 s |
| `Perceptron.battery` | `GET /api/hat/battery` | `{"voltage_v": float}` | 30 s |

> **Grayscale uses raw ADC, not `/normalized`.** On this hardware the downward
> sensors read *inverted* relative to the normalisation calibration: a reflective
> floor reads **high** (~400-900 raw) and a drop-off / no surface reads **low**
> (~30). So a cliff is a *low* reading, and `ObstacleWorldModel` thresholds the
> raw `values` (default 200). The `/api/sensor/grayscale/normalized` endpoint
> would compress this signal with the opposite-polarity assumption.

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
  strategy, emitting a new `ActionPlan` only when it changes. Once triggered, an
  avoid maneuver is **held for `avoid_duration_s`** before re-evaluating, so the
  robot commits to backing up and turning rather than darting forward the instant
  the front sensor clears (`explore` defaults this to 2.5 s; the layer default is
  0.0 = re-evaluate immediately).
- **`VehicleAction`** (`action.vehicle`) — executes plan actions in priority
  order, mapping `drive`/`steer`/`stop` to `POST /api/drive`, `/api/steer`,
  `/api/hat/motor/stop`. Injected httpx client per ADR-002; emits an
  `ActionResult` per action (best-effort onto an optional telemetry queue — the
  Phase 7 seam); transient HTTP/timeout errors are recorded, not fatal.
  **Renews the actuator lease**: `drive`/`steer` carry a TTL (`ttl_ms`) and
  nomopractic's watchdog idles any motor/servo whose lease lapses. Because the
  world model and planner are edge-triggered, no new plan arrives in steady
  state, so this layer re-issues the held plan every `renew_interval_s`
  (default: half the TTL) to keep the robot moving until the plan changes.
  Renewal stops when the routine stops, so the watchdog's safety stop is intact.

The full loop is exercised without hardware by
`tests/test_pipeline_integration.py`: a near ultrasonic reading propagates
Perception → World Model → Planning → Action and produces a stop/reverse command
to the (mock) device.

---

## Message Types

All messages are dataclasses passed as **typed instances** through the layers'
`asyncio.Queue` channels within a process (e.g. `asyncio.Queue[PerceptionEvent]`) —
keeping full typing and skipping a per-hop serialisation round-trip. They remain
JSON-serialisable via `to_dict()` for the **serialisation boundaries**: telemetry
to nomothetic, NDJSON logging, and test fixtures (ADR-006).

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
    {"method": "stop",          "params": {},                       "priority": 0},
    {"method": "drive",         "params": {"speed_pct": -30},       "priority": 1},
    {"method": "steer",         "params": {"angle_deg": 135},       "priority": 2}
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

## Inter-Layer Communication

Layers communicate via typed `asyncio.Queue` channels carrying message
**instances** (e.g. `asyncio.Queue[PerceptionEvent]`). The `Pipeline` class wires
up the queues and starts each layer as an asyncio task:

```python
pipeline = Pipeline(
    perception=MyPerception(client, device_id),
    world_model=MyWorldModel(),
    planner=MyPlanner(config),
    action=MyAction(client, device_id),
)
await pipeline.run()
```

Each queue has a bounded capacity (default 32) to create back-pressure: if the action layer falls behind, the planner pauses; if the planner pauses, the world model pauses; if the world model pauses, perception slows its polling.

Each layer position is owned by a `LayerSlot` that holds the asyncio Task and the
queues it was started with. `Pipeline.run()` starts every slot, then awaits the
tasks; `Pipeline.stop()` (also run in `run()`'s `finally`) stops them in order.

### Multi-source perception fan-in

Pass a `FanInSlot` as the `perception` argument to run several sensor sources
concurrently onto one downstream queue. All sources share that queue, so
back-pressure pauses them together — e.g. ultrasonic + grayscale for the
`explore` routine:

```python
from autonomon import FanInSlot, Perceptron

pipeline = Pipeline(
    perception=FanInSlot(
        "perception",
        [Perceptron.ultrasonic(client, device_id), Perceptron.grayscale(client, device_id)],
    ),
    world_model=ObstacleWorldModel(device_id),
    planner=AvoidancePlanner(device_id),
    action=VehicleAction(client, device_id),
)
```

`FanInSlot` is a Perception-position construct (it has no upstream queue to fan
out). It is the only multi-source composition autonomon provides.

> **Removed as speculative (ADR-006).** Runtime layer hot-swap
> (`Pipeline.swap_layer` / `LayerSlot.swap`) and competing-planner fan-in
> arbitration (`MergeStrategy.ARBITRATE`, an arbiter window, and dynamic
> `add_impl`/`remove_impl`) were removed — no routine used them, and the
> arbitration path carried a latent bug. The "swap an implementation" capability
> that routines actually use is **wiring-time selection**: each routine factory
> chooses the layer implementations it needs (`explore` vs `follow-user`). If
> real-time competing planners are ever required, reintroduce that as a dedicated
> slot with its own ADR.

---

## Layer Contract

Each layer is an `asyncio` coroutine that reads from `queue_in`, processes, and writes to `queue_out`. The `Pipeline` creates and injects both queues.

```python
class PerceptionBase(ABC):
    async def run(self, queue_out: asyncio.Queue[PerceptionEvent]) -> None: ...
    async def stop(self) -> None: ...

class WorldModelBase(ABC):
    async def run(
        self, queue_in: asyncio.Queue[PerceptionEvent], queue_out: asyncio.Queue[WorldStateUpdate]
    ) -> None: ...
    async def stop(self) -> None: ...

class PlannerBase(ABC):
    async def run(
        self, queue_in: asyncio.Queue[WorldStateUpdate], queue_out: asyncio.Queue[ActionPlan]
    ) -> None: ...
    async def stop(self) -> None: ...

class ActionBase(ABC):
    async def run(self, queue_in: asyncio.Queue[ActionPlan]) -> None: ...
    async def stop(self) -> None: ...
```

---

## Plugin System

Each plugin in the autonomon repo follows this pattern:

1. **`pyproject.toml`**: entry point `nomon-<name> = "<package>.cli:main"`
2. **`__init__.py`**: exports a `nomon_manifest` dict describing the plugin (name, version, required capabilities, params schema)
3. **`cli.py`**: reads `NOMON_DEVICE_URL`, `NOMON_PLUGIN_TOKEN`, `NOMON_PLUGIN_PARAMS` from env; emits NDJSON lifecycle events to stdout; runs the pipeline
4. **`control.py`** (or layer submodules): implements the `autonomon` base classes

At deploy time autonomon publishes its catalogue (the `nomon_manifest` plus its own venv's `nomon-autonomon` path) to a shared file (`NOMON_ROUTINE_CATALOG_PATH`); `nomothetic` reads that file to list routines and to launch the `nomon-autonomon` CLI as a subprocess, then reads its stdout NDJSON for lifecycle telemetry. The two projects keep separate venvs and never import each other (ADR-005).

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
   union of their param schemas; autonomon publishes it to a shared file at
   deploy time so `nomothetic` reads the whole catalogue from one file rather
   than importing autonomon or scanning N packages (ADR-005).

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
| Perception (sensors) | raw in | `GET /api/sensor/ultrasonic`, `GET /api/sensor/grayscale` (raw ADC), `GET /api/hat/battery` |
| Perception (vision, `follow-user`) | raw in | `GET /api/camera/frame` → `image/jpeg` (single raw frame) |
| Action | actions out | `POST /api/drive`, `POST /api/steer`, `POST /api/hat/motor/stop` |

**Raw camera frames (`follow-user`, Phase 6b):** `GET /api/camera/frame` returns a
single raw JPEG (`image/jpeg`). Note `POST /api/camera/capture` writes a file to
disk and returns metadata only — it does *not* return frame bytes — so the frame
endpoint was added as a small *raw input* (ADR-004-legal). The `follow-user`
routine polls this endpoint and runs person detection **inside its autonomon
vision perception layer** (ONNX Runtime + YOLOv8n) — it does *not* call a
nomothetic detection endpoint, because none exists or should.

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
