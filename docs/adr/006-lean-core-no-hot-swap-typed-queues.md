# ADR-006: Lean Core — Drop Runtime Hot-Swap & Planner Arbitration; Typed In-Process Queues

**Status:** Accepted
**Date:** 2026-06-23
**Deciders:** Perceptua

---

## Context

Phase 1 shipped three pieces of pipeline machinery that anticipated future needs:

1. **Runtime layer hot-swap** — `Pipeline.swap_layer()` / `LayerSlot.swap()` with a
   `DRAINING` state and a swap lock, to replace one layer implementation mid-run
   while preserving the queues.
2. **Competing-planner fan-in arbitration** — `FanInSlot` with
   `MergeStrategy.ARBITRATE`, an arbiter callback, a timing window, a fan-out
   dispatcher for non-perception positions, and dynamic `add_impl`/`remove_impl`.
3. **Dict-on-queue messages** — every layer called `to_dict()` when putting a
   message and `from_dict()` when getting one, keeping queues typed as
   `Queue[dict]` "for easy NDJSON serialisation."

A review for leanness found that none of (1) or (2) has a production or routine
consumer: only the unit tests and the docs referenced them. `explore` uses
`FanInSlot` solely in pass-through mode at the Perception position; no routine
calls `swap_layer` or uses `ARBITRATE`. The `add_impl` ARBITRATE branch also
carried a latent bug (it read `self._arbiter_queue` before it was assigned). For
(3), the inter-process/NDJSON benefit is not used — the four layers run in one
asyncio process — so the per-hop `to_dict`/`from_dict` round-trip only cost type
safety and cycles.

The project's stated priority is a lean codebase that still supports *swappable
models for intelligent behaviour*. The swappability that matters is already
provided by the **routine registry/factory** (ADR-003): each routine wires the
layer implementations it needs (`explore` vs `follow-user` choose different
perception/world-model/planner layers). Runtime hot-swap and real-time planner
arbitration are a heavier, different claim with no current use.

## Decision

1. **Remove runtime hot-swap.** Delete `Pipeline.swap_layer`, `LayerSlot.swap`,
   the `DRAINING` state, and the swap lock. `LayerSlot` remains as the per-layer
   task owner the `Pipeline` is built on (`start`/`stop`/`tasks`).
2. **Remove fan-in arbitration and dynamic membership.** Collapse `FanInSlot` to a
   pure multi-source **pass-through at the Perception position** (N sources → one
   shared downstream queue). Delete `MergeStrategy`, the arbiter loop/window, the
   non-perception dispatcher, and `add_impl`/`remove_impl`.
3. **Pass typed message instances on queues.** Layers `put`/`get`
   `PerceptionEvent` / `WorldStateUpdate` / `ActionPlan` / `ActionResult`
   instances directly; queues are typed (`asyncio.Queue[PerceptionEvent]`, …).
   `to_dict()`/`from_dict()` remain for the serialisation **boundaries** only:
   telemetry to nomothetic, NDJSON logging, and test fixtures.

Swappability is preserved by the registry/factory (wiring-time layer selection)
and by multi-source perception fan-in.

## Consequences

- ~250 lines of unused, partly-buggy machinery and their tests are removed; the
  core is smaller and fully typed (mypy now checks message flow between layers).
- No behavioural change to any routine; the full pytest suite stays green.
- If real-time competing planners are ever needed, reintroduce a dedicated planner
  slot (and its arbiter) behind a new ADR — with a concrete consumer driving the
  design — rather than carrying speculative infrastructure.
- This supersedes the hot-swap and `ARBITRATE` portions of ADR-001's "swappable at
  each layer" promise; the layered architecture and per-routine layer selection
  from ADR-001/ADR-003 are unchanged.

## Alternatives considered

- **Keep the machinery as forward-looking infrastructure.** Rejected: it had no
  consumer, carried a latent bug, and the registry already delivers the
  swappability routines use. Speculative generality is the cost we are cutting.
- **Trim only arbitration, keep hot-swap.** Rejected: hot-swap likewise has no
  consumer and adds the same kind of speculative surface.
- **Leave dict-on-queue messages.** Rejected: the cross-process benefit is unused
  in-process, and typed instances give better safety with less overhead.
