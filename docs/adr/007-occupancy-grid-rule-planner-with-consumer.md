# ADR-007: Build the Occupancy Grid & Rule Planner With a Concrete Consumer (`patrol`)

**Status:** Accepted
**Date:** 2026-06-25
**Deciders:** Perceptua

---

## Context

Phases 3 (occupancy-grid world model) and 4 (rule-table planner) were the only
roadmap items left before Phase 7. Both had been **deliberately deferred** as
*"speculative — no consumer"*: `explore` runs on `ObstacleWorldModel`'s two
booleans and `AvoidancePlanner`'s two rules, and `follow-user` uses a target
world model and the follow planner. ADR-006 had just removed ~250 lines of
consumer-less machinery (runtime hot-swap, planner arbitration) and set an
explicit rule: reintroduce such infrastructure only *"with a concrete consumer
driving the design."*

Building `OccupancyWorldModel` and `RulePlanner` as bare, unused classes would
re-create exactly the speculative surface ADR-006 cut. We also surfaced a
concrete sensing constraint: a meaningful **world-frame** occupancy grid needs a
pose/odometry source to place obstacles in a fixed frame, and the fleet exposes
only a single forward ultrasonic (no odometry) today — which is part of why the
grid had no consumer.

## Decision

Build both deferred phases **together with one routine that needs both** —
`patrol` — so neither layer ships without a consumer.

1. **`OccupancyWorldModel` (Phase 3)** — a **robot-centric local costmap** with
   configurable time **decay** (the deferred "obstacles age out after N seconds"
   item), not a world-frame map. It emits the backward-compatible
   `obstacle_ahead`/`cliff_detected` booleans plus memory the boolean model
   cannot give: `recently_blocked`, `occupied_cells`, `nearest_obstacle_cm`, and
   a serialisable `occupancy` snapshot. Emission is debounced on the salient
   booleans so grid churn never floods the planner. Absent a heading source,
   cells are placed on the forward axis only (`ix == 0`); the 2-D cell key leaves
   room to populate off-axis cells once a sweep or odometry source exists.

2. **`RulePlanner` (Phase 4)** — an ordered **rule table** (first match wins)
   loadable from TOML, generalising `AvoidancePlanner`: each rule pairs a
   condition (`when` AND-clauses with `lt/le/gt/ge/ne/eq/in/truthy/exists`
   operators, plus `any_of` OR-groups) with an action sequence, debounced on the
   rule name, with a per-rule `hold_s` commit window (the generalisation of
   `avoid_duration_s`). It reuses `AvoidancePlanner`'s proven idle-tick loop
   verbatim. A bundled `explore.toml` reproduces `explore`'s avoid/cruise
   behaviour (proven by an equivalence test) and is selectable via
   `build_explore(params={"planner": "rule"})`.

3. **`patrol` routine (the consumer)** — `(ultrasonic + grayscale) ->
   OccupancyWorldModel -> RulePlanner(patrol.toml) -> VehicleAction`. Its bundled
   `patrol.toml` keys a `caution` rule on `recently_blocked`, so the behaviour
   genuinely depends on the grid's memory; the table is swappable via a
   `rules_path` param. Motion lives in the table (the Phase-4 value: behaviour as
   data); the routine's params tune the world model.

## Consequences

- The roadmap's Phase 3 and Phase 4 deliverables ship **without** leaving
  consumer-less infrastructure: every new class is exercised by `patrol` (and the
  rule engine additionally by the `explore` rule-table variant).
- `OccupancyWorldModel` is a drop-in `WorldModelBase` (it still emits the boolean
  fields), and `RulePlanner` is a drop-in `PlannerBase`; existing routines are
  unchanged, and `explore` keeps `AvoidancePlanner` as its default.
- TOML parsing uses stdlib `tomllib` on 3.11+ with a `tomli` backport pin for
  3.9/3.10; bundled tables ship as package-data resolved by `bundled_rules_path`.
- **Deferred (future work, when a sensor justifies it):** a true **world-frame**
  occupancy grid built on an odometry/encoder perception source, and off-axis
  cell placement from a sweeping range sensor. The current model upgrades to these
  without an interface change.

## Alternatives considered

- **Build the two classes standalone, per the literal phase deliverables.**
  Rejected: that is the speculative, consumer-less surface ADR-006 just removed.
- **Skip Phase 3; ship only the rule planner.** Rejected: the user asked for both,
  and pairing them under one `patrol` consumer justifies each without extra cost.
- **A world-frame occupancy grid now (add odometry first).** Rejected as
  out-of-scope: it needs a new raw-input dependency (encoder/odometry from
  nomothetic) and is speculative until a routine needs spatial mapping — the local
  costmap covers `patrol`'s memory need today.
