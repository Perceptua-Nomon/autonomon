# ADR-003: Routine Registry — Named Behaviours as Pipeline Factories

**Status:** Accepted
**Date:** 2026-06-18
**Deciders:** Perceptua

---

## Context

`autonomon` now has a working four-layer pipeline with a minimal concrete
implementation of every layer (`Perceptron`, `ObstacleWorldModel`,
`AvoidancePlanner`, `VehicleAction`) and an end-to-end integration test that
wires all four into a `Pipeline` and drives a mock device
(`tests/test_pipeline_integration.py`).

We want to ship **named, built-in behaviours** — each accomplishing one goal by
composing the autonomy layers. The initial catalogue is small: `explore`
(obstacle-avoidance wandering) and `follow-user`, with room to add more.

The roadmap's Phase 6 was "migrate `nomon_explore` to the layered framework" —
i.e. restore the Phase-0 monolithic `nomon_explore` package as thin wiring of
the framework. Two facts reframe that goal:

1. **`nomon_explore` is already gone.** The package directory survives only as
   empty scaffolding (`nomon_explore/src/nomon_explore/` with no `__init__.py`,
   no `pyproject.toml`, no tests). There is no monolith left to "migrate" — the
   work is greenfield wiring, not a refactor.
2. **The integration test already contains an "explore" routine.** Its
   `_build_pipeline()` helper constructs the exact `Pipeline` that an `explore`
   behaviour needs. That helper *is* a routine factory, written ad hoc in a
   test. The only open question is where the production version of that factory
   lives and how a second behaviour (`follow-user`) is added beside it.

### The naming collision

The term **"routine" already has a meaning in this project**, at a different
layer of the stack:

- `nomothetic/src/nomothetic/hat.py` exposes `start_routine` / `stop_routine` /
  `get_routine_status` — HAT IPC methods dispatched over the Unix socket to the
  Rust daemon (`nomopractic`).
- `nomothetic/src/nomothetic/api.py` exposes `POST /api/routine/start`,
  `/api/routine/stop`, `/api/routine/status` (the `Routine` REST tag), whose
  `name` field documents *"Currently only 'explore' is supported"*.
- `nomotactic` once had a `RoutineCard` UI for start/stop explore; it was
  scrubbed ("remove explore routine button and all routine references").

That existing "routine" is a **firmware-side behaviour**: obstacle avoidance
running *inside* nomopractic, commanded by a single IPC call, with no host-side
cognition. The nomothetic API simply forwards to the HAT. It shares the word
"routine" and even the example name "explore" with what we are now building, but
it is a completely separate execution model.

What `autonomon` builds is a **host-side cognitive behaviour**: a four-layer
asyncio pipeline running in a plugin process, polling and commanding the device
*over the REST API*. The device firmware does not know it exists.

## Decision

### D1 — A routine is a factory that wires a `Pipeline`; routines live in a registry

A **routine** in `autonomon` is a named, parameterised factory function that
returns a fully wired `Pipeline`. Routines live in a new `autonomon.routines`
module: a small registry mapping a routine name to its factory, plus the
built-in factories themselves.

```python
# Conceptual shape (NOT prescriptive code — the builder decides signatures):
#   build_explore(client, device_id, params)      -> Pipeline
#   build_follow_user(client, device_id, params)  -> Pipeline
#   ROUTINES: dict[str, RoutineFactory] = {"explore": ..., "follow-user": ...}
#   def get_routine(name) -> RoutineFactory: ...
```

`explore` becomes one registry entry — the production form of the integration
test's `_build_pipeline()` — not a bespoke package. `follow-user` is a second
entry beside it.

### D2 — Routines are the registry; the plugin is the runner

We do **not** create one Python package per behaviour. We keep exactly one
plugin package and entry point, and select the behaviour by name:

- The single CLI entry point (e.g. `nomon-autonomon`) reads the routine name
  from `NOMON_PLUGIN_PARAMS` (a `routine` / `name` key), looks it up in the
  registry, builds the `Pipeline`, and runs it — emitting the same NDJSON
  lifecycle events (`starting` / `running` / `stopping` / `error`) already
  specified in `architecture.md`.
- The plugin's `nomon_manifest` advertises the available routine names and the
  union of their parameter schemas, so `nomothetic`'s `AutonomyPluginManager`
  can discover them via one manifest rather than N packages.

The registry is the catalogue; the plugin is the generic launcher over it.

### D3 — Parameterisation is a per-routine params dict validated by the factory

Each routine factory accepts a shared `httpx.AsyncClient` and `device_id` (per
ADR-002) plus a routine-specific params dict. The factory is responsible for
mapping params onto its layers' constructor arguments (e.g. `explore` →
`obstacle_threshold_cm`, `forward_speed_pct`, `turn_angle_deg`; `follow-user` →
`target_distance_cm`, `max_speed_pct`). Parameter *schemas* are declared in the
manifest; parameter *application* is the factory's job. No new config framework
is introduced — params are a plain dict, consistent with `NOMON_PLUGIN_PARAMS`.

### D4 — Keep the `autonomon` name; do not adopt nomothetic's "routine" wire contract

We use "routine" inside `autonomon` because it is the most natural word for a
named built-in behaviour, and the user asked for "routines." But:

- The autonomon routine concept is **deliberately distinct** from nomothetic's
  HAT-level `start_routine` IPC method. They are not the same object at two
  layers; they are two different mechanisms that happen to share a noun.
- An autonomon routine MUST NOT be assumed to back `POST /api/routine/start`.
  That endpoint forwards to the firmware. If a future phase wants the app to
  launch *autonomon* routines, that is a separate REST surface
  (`AutonomyPluginManager` lifecycle), to be designed then — not this ADR.
- Docs that mention routines MUST disambiguate "HAT routine" (firmware,
  nomopractic) from "autonomy routine" (host pipeline, autonomon) to prevent
  the collision from leaking into code or operator confusion.

## Rationale

**Why a registry of factories rather than restoring the monolith?**
The monolith no longer exists, so "restore" overstates the work and understates
the opportunity. A factory-per-behaviour registry costs almost nothing — the
`explore` factory is already written (as a test helper) — and it makes the
*second* behaviour cheap: `follow-user` is a new dict entry plus whatever new
layers it needs, with zero new packaging, CLI, or manifest plumbing. This is the
"shortest path / minimal slice" the project favours.

**Why one plugin, not one package per routine?**
Per-package plugins duplicate `pyproject.toml`, CLI boilerplate, manifest, and
lifecycle-event emission for every behaviour — exactly the duplication ADR-001
rejected at the layer level. A single generic runner over a registry keeps that
machinery in one place. `AutonomyPluginManager` discovers one manifest listing
many routines instead of N manifests.

**Why surface the collision explicitly instead of renaming?**
Renaming to "behaviour" or "skill" would avoid the clash but fight the user's
own vocabulary and the existing REST tag. The collision is real but harmless as
long as it is *documented*: the two routines live in different repos, different
processes, and different execution models. The risk is silent confusion, which a
clear naming convention ("HAT routine" vs "autonomy routine") mitigates.

**Why does `follow-user` need new layers?**
`explore` reuses every existing layer as-is. `follow-user` cannot:
- **Perception:** needs a *target source* (e.g. camera/vision via a new
  endpoint, or a bearing/range sensor), not just ultrasonic distance. This is a
  new `Perceptron` configuration at minimum, or a new perception implementation.
- **World model:** must track a *target's relative position*, not a boolean
  obstacle flag — a new `WorldModelBase` implementation.
- **Planner:** must do *pursuit* (close distance to a moving target), not
  *avoidance* — a new `PlannerBase` implementation.
- **Action:** `VehicleAction` is reusable unchanged.

So `follow-user` is the proof that the registry is worth building: it exercises
the swap-one-layer extensibility ADR-001 promised, while `explore` proves the
common case is pure wiring.

## Trade-offs

| Benefit | Cost |
|---------|------|
| Second behaviour is a registry entry, not a package | Registry is one more module to maintain |
| Single CLI / manifest / lifecycle machinery | Routine name must be passed in params and validated |
| `explore` reuses 100% of existing layers | `follow-user` needs 3 new layer impls (expected) |
| "routine" matches the user's and the REST tag's vocabulary | Naming collision with HAT routine must be documented |

## Alternatives Considered

### Restore `nomon_explore` as a standalone migrated package (former Phase 6)

**Rejected / reshaped.** Nothing remains to migrate, and a standalone package
does not generalise to a catalogue. This ADR replaces that goal with the
registry; the roadmap's Phase 6 is reshaped accordingly (see roadmap).

### One package per routine (`nomon_explore`, `nomon_follow`, …)

**Rejected.** Duplicates packaging/CLI/manifest per behaviour. The registry
keeps that machinery singular.

### Rename the concept to "behaviour"/"skill" to dodge the collision

**Rejected.** Fights the user's vocabulary and the existing `Routine` REST tag.
Disambiguating documentation is cheaper and clearer than a divergent term.

### Make autonomon routines back `POST /api/routine/start`

**Deferred.** That endpoint forwards to firmware today. Re-pointing it (or
adding an autonomy-specific REST surface) is a cross-repo nomothetic change out
of scope here; it belongs with the `AutonomyPluginManager` lifecycle work.

## Consequences

- A new `autonomon.routines` module holds the registry and built-in factories.
- `explore` is implemented as a factory equal to the integration test's wiring;
  the integration test can call the registry rather than a private helper.
- The single plugin CLI selects a routine by name from `NOMON_PLUGIN_PARAMS`.
- `follow-user` introduces new perception/world-model/planner implementations;
  these are the next layer-implementation deliverables, tracked in the roadmap.
- All routine documentation distinguishes **HAT routines** (nomopractic
  firmware, via `start_routine` IPC) from **autonomy routines** (autonomon
  pipelines, via the plugin runner).

## References

- ADR-001: Four-Layer Cognitive Architecture (layer swap / extensibility)
- ADR-002: httpx AsyncClient with per-plugin token injection (factory inputs)
- `autonomon/docs/architecture.md`: Routines section, Plugin System
- `autonomon/docs/roadmap.md`: Phases 6 (reshaped) and 6b (follow-user)
- `nomothetic/src/nomothetic/api.py`: `Routine` REST tag (HAT routine — distinct)
- `nomothetic/src/nomothetic/hat.py`: `start_routine` IPC (HAT routine — distinct)
