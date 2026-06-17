# autonomon — Architecture

## System Overview

`autonomon` is a mono-repo of Python packages that add autonomous capabilities to the nomon fleet. Each device running `nomothetic` can host one or more autonomy plugins; each plugin drives the device through a four-layer cognitive pipeline.

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
    ├── perception/
    │   ├── __init__.py
    │   └── base.py       PerceptionBase abstract class
    ├── world_model/
    │   ├── __init__.py
    │   └── base.py       WorldModelBase abstract class
    ├── planning/
    │   ├── __init__.py
    │   └── base.py       PlannerBase abstract class
    └── action/
        ├── __init__.py
        └── base.py       ActionBase abstract class
```

### `nomon_explore` (obstacle-avoidance plugin)

First concrete plugin. Uses the 4-layer pipeline:
- Perception: polls `/api/hat/ultrasonic` and `/api/hat/grayscale`
- World model: detects obstacle/cliff conditions
- Planning: chooses forward / backup+turn / stop strategy
- Action: calls `/api/vehicle/*` endpoints

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

## Inter-Layer Communication

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

## Nomothetic API Surface Used

| Layer | Endpoints called |
|-------|-----------------|
| Perception | `GET /api/hat/ultrasonic`, `GET /api/hat/grayscale`, `GET /api/hat/battery` |
| Action | `POST /api/vehicle/drive`, `POST /api/vehicle/steer`, `POST /api/vehicle/stop`, `POST /api/hat/servo` |

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
