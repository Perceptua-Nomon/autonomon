# ADR-001: Four-Layer Cognitive Architecture for Autonomon

**Status:** Accepted
**Date:** 2026-06-17
**Deciders:** Perceptua

---

## Context

`autonomon` provides autonomous capabilities for nomon devices. The initial implementation (`nomon_explore`) embedded perception, decision-making, and actuation in a single control loop. This worked for a single behaviour but is difficult to extend: adding a new sensor, swapping a planning strategy, or changing how actions are executed requires touching the entire codebase.

We need an architecture that:
1. Separates concerns cleanly so each part can be developed and tested independently
2. Supports multiple concurrent sensor types without coupling them to planning logic
3. Allows swapping planning strategies (rule-based → ML-based) without changing perception or action
4. Keeps communication between layers inspectable and loggable

## Decision

Organise all autonomous behaviour around four layers — **Perception → World Model → Planning → Action** — each communicating via typed JSON messages passed through bounded `asyncio.Queue` channels.

## Rationale

### Why four layers?

The perception → world model → planning → action decomposition is the standard cognitive architecture pattern (deliberative / BDI-style), well-established in mobile robotics. It maps cleanly onto nomon's physical structure:

- **Perception**: only talks to the device REST API (reads sensors)
- **World Model**: only talks to the Perception layer (fuses events into state)
- **Planning**: only talks to the World Model (pure logic, no I/O)
- **Action**: only talks to the Planning layer and the device REST API (executes)

This means Planning is purely functional (testable without any mock HTTP), and Perception can be replaced with a replay of recorded sensor events for offline testing.

### Why asyncio queues?

- Each layer runs as an `asyncio` task; queues are the natural async primitive
- Bounded queues (default 32) provide back-pressure automatically — if Action is slow, Planning pauses; this prevents unbounded memory growth
- The same dict payload can be serialised to NDJSON for inter-process use or logging without changing the layer interface
- Simpler than message brokers (no Redis/MQTT required for single-device operation)

### Why not combine Perception and World Model?

The world model accumulates state across time (e.g., "obstacle was detected 0.3 s ago and hasn't cleared"). If perception and world model are merged, every sensor type has to reason about state history. Separating them keeps each sensor's logic to "read, normalise, emit" and centralises state management.

### Why not combine Planning and Action?

Planning is synchronous logic over a state snapshot; Action is async I/O with retries and error handling. Merging them would block the planning loop on HTTP latency and make planning untestable without a real device. Separated, the planner can be unit-tested with mock queue writes.

## Trade-offs

| Benefit | Cost |
|---------|------|
| Each layer independently testable | Small latency overhead per queue hop (~10 µs) |
| Swap implementations without touching other layers | More boilerplate than a monolithic loop |
| Back-pressure prevents memory blowup | Bounded queues can cause planning to stall if action is very slow |
| JSON messages are loggable/replayable | Serialisation overhead per message (negligible at robot timescales) |

For a robot operating at 10 Hz sensor loops, queue hop latency is negligible. The benefits strongly outweigh the costs.

## Alternatives Considered

### Single control loop (status quo in nomon_explore)

**Rejected:** Works for one behaviour but cannot scale. Adding a second sensor type or planning strategy requires rewriting the control loop.

### Separate OS processes with NDJSON stdio

**Rejected for initial implementation:** Adds process management complexity. The asyncio-queue approach achieves the same interface contract and can be adapted to multi-process later by replacing queue writes with NDJSON serialisation (the message types are already JSON-serialisable).

### ROS (Robot Operating System)

**Rejected:** ROS is not available on Raspberry Pi Zero 2W (too resource-constrained) and introduces a large dependency surface. The nomon stack is intentionally minimal.

## Consequences

- All future autonomy plugins implement the four base classes and wire them into `Pipeline`
- `nomon_explore` will be refactored (Phase 6) to use the framework; until then it coexists as a pre-framework plugin
- The `autonomon` package becomes a required dependency of all autonomon plugins
- Any future ML-enhanced layer only needs to implement the abstract base class for its slot in the pipeline
