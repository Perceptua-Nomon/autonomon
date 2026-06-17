# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Role

Mono-repo of Python packages adding autonomous capabilities to the nomon fleet. Devices running `nomothetic` host autonomy plugins from this repo; each plugin drives the device through a four-layer cognitive pipeline over the nomothetic REST API.

## Packages

| Directory | Package | Status |
|-----------|---------|--------|
| `autonomon/` | `autonomon` — core framework: base classes, message types, pipeline runner | Phase 1 in progress |
| `nomon_explore/` | `nomon_explore` — obstacle-avoidance drive plugin | Complete |

## Commands

### autonomon (core framework)

```bash
cd autonomon
make install-dev         # uv sync --all-extras
make test                # uv run pytest tests/ -v
uv run pytest tests/test_pipeline.py::test_name   # single test
make lint                # ruff check + black --check
make format              # black + ruff --fix
make type-check          # mypy src/ tests/
make check               # lint + type-check + test
```

### nomon_explore

```bash
cd nomon_explore
make check               # lint + type-check + test
# Manual run against a device:
NOMON_DEVICE_URL=https://<pi-host>:8443 \
NOMON_PLUGIN_TOKEN=<device-jwt> \
NOMON_PLUGIN_PARAMS='{"speed_pct": 20}' \
nomon-explore
```

## Four-Layer Architecture

```
nomothetic REST API (HTTPS :8443)
  │ poll                  ▲ execute
  ▼                       │
Perception ──► World Model ──► Planning ──► Action
  PerceptionEvent   WorldStateUpdate   ActionPlan   ActionResult
```

Each layer is an asyncio coroutine communicating via bounded `asyncio.Queue[dict]` (back-pressure by design). The `Pipeline` class in `autonomon.pipeline` wires them together.

**Layer contract:**
- `PerceptionBase.run(queue_out)` — poll sensors, emit `PerceptionEvent.to_dict()`
- `WorldModelBase.run(queue_in, queue_out)` — fuse events, emit `WorldStateUpdate.to_dict()` on change
- `PlannerBase.run(queue_in, queue_out)` — pure logic, emit `ActionPlan.to_dict()` on plan change
- `ActionBase.run(queue_in)` — execute plans via httpx; emit `ActionResult.to_dict()`

All message types are in `autonomon.messages`. Pass dicts (`.to_dict()`) on queues; reconstruct with `.from_dict()` when reading.

## Hot-Swap and Multi-Source Fan-In

Swap-in autonomy at each layer is a first-class feature:

**Runtime hot-swap** (single impl replacement, zero pipeline downtime):
```python
await pipeline.swap_layer("perception", new_yolo_model)
```
Implemented via `LayerSlot` in `autonomon.slot`. The queues persist across the swap; in-flight messages are never lost.

**Multi-source fan-in** (N impls at one position):
```python
from autonomon import FanInSlot, MergeStrategy

# PASS_THROUGH — both sources emit to the same downstream queue
Pipeline(perception=FanInSlot("perception", [yolo, ultrasonic]))

# ARBITRATE — pick the best plan from competing planners
Pipeline(planner=FanInSlot("planner", [rule_planner, llm_planner],
                            MergeStrategy.ARBITRATE, arbiter=pick_best))
```
Implemented via `FanInSlot` in `autonomon.fan_in`. `add_impl()` / `remove_impl()` work on a running slot.

**Key modules:** `autonomon.slot` (`LayerSlot`, `SlotState`), `autonomon.fan_in` (`FanInSlot`, `MergeStrategy`), `autonomon.pipeline` (`Pipeline.swap_layer`).

## Plugin System

Each plugin package exposes:
- `nomon_manifest` in `__init__.py` — name, version, required capabilities, params schema
- CLI entry point `nomon-<name>` — reads `NOMON_DEVICE_URL`, `NOMON_PLUGIN_TOKEN`, `NOMON_PLUGIN_PARAMS` from env; emits NDJSON lifecycle events to stdout
- `nomothetic` `AutonomyPluginManager` discovers plugins via `nomon_manifest` and launches them as subprocesses

## Coding Conventions

Follows nomothetic Python conventions (same toolchain: `black`, `ruff`, `mypy`, `pytest`):
- `black` line length 100; `ruff` same rule set as nomothetic
- NumPy-style docstrings on all public classes and methods; full type hints
- `asyncio.to_thread()` for any blocking I/O inside async methods
- Exception chaining: `raise NewError("...") from e`
- No direct I2C/GPIO access — all sensor reads and actuator writes go through the nomothetic REST API
- `httpx` for all HTTP calls; `verify=False` on device connections (self-signed TLS certs per nomothetic ADR-001)

## Testing

- All layers testable without a real device: inject a mock `httpx.AsyncClient` in Perception/Action; push test dicts into queues in World Model/Planning tests
- `pytest-asyncio` with `asyncio_mode = "auto"` — mark async tests with `@pytest.mark.asyncio`
- No Pi hardware required for any test

## Key Docs

- `docs/architecture.md` — layer diagram, message schemas, plugin system, nomothetic API surface
- `docs/roadmap.md` — phase status and planned work
- `docs/adr/001-layered-architecture.md` — rationale for the 4-layer design
