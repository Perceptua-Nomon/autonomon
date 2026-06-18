# ADR-004: autonomon Is the Brain — All Processing and Modeling Lives Here

**Status:** Accepted
**Date:** 2026-06-18
**Deciders:** Perceptua

---

## Context

Phase 6b (`follow-user`) needs person/target detection. Scoping nomothetic
found it already has *raw* camera access (`POST /api/camera/capture`, the MJPEG
stream, `/api/camera/pan` + `/api/camera/tilt` servo control) but **no** vision
processing: no person/face detection, no object tracking, no analytics anywhere
in the stack.

That surfaces a fork that recurs for every future capability, not just vision:
when a behaviour needs interpreted data the device cannot produce raw, *where
does the interpretation live* — in nomothetic (closer to the hardware) or in
autonomon (the autonomy layer)?

The initial Phase 6b scoping leaned toward adding a vision-processing endpoint
(`POST /api/vision/detect`) to nomothetic. On reflection that pushes cognition
down into the hardware gateway and erodes the boundary the four-layer pipeline
exists to enforce. This ADR settles the question as a standing principle.

The hard rule already in the top-level project context — *"Python
(`nomothetic`) contains zero hardware register knowledge; all I2C/PWM/ADC logic
lives in nomopractic"* — establishes a clean gateway boundary **below**
nomothetic. This ADR establishes the symmetric boundary **above** it:
nomothetic contains zero autonomy/perception cognition; all of that lives in
autonomon.

## Decision

**autonomon is the central brain. All input processing and modeling lives in
autonomon. nomothetic is a thin hardware gateway that serves raw inputs and
accepts action outputs — nothing more.**

### D1 — The boundary contract

The autonomy pipeline is self-contained between exactly two boundaries:

- **Inputs (raw):** Perception pulls *raw* data from nomothetic — sensor reads
  (ultrasonic, grayscale, battery, encoder) and raw camera frames / stream.
- **Outputs (actions):** Action emits *actuator commands* to nomothetic —
  drive, steer, stop, camera pan/tilt.

Everything between those boundaries — sensor fusion, computer vision,
person/object detection, world modeling, planning, arbitration — is
autonomon's responsibility and runs inside the autonomon process.

### D2 — nomothetic performs no interpretation

nomothetic (and nomopractic below it) perform **no** perception, detection,
fusion, or modeling. nomothetic's REST surface only grows when a **new raw
input or new raw actuator** is exposed. It is never extended to add
*interpretation* of data it already serves.

Concretely: person detection for `follow-user` is a new **autonomon perception
implementation** that pulls raw frames from the existing camera endpoints and
runs the detector in-process. It is **not** a new `/api/vision/*` endpoint on
nomothetic.

### D3 — Capabilities are added as autonomon layers, not gateway endpoints

A new capability that needs interpreted data is added as a new layer
implementation (most often a perception impl) inside autonomon, consuming raw
nomothetic I/O. The default answer to "where does this processing go?" is
**autonomon**; pushing it into nomothetic requires explicit justification and an
ADR superseding this one.

### D4 — Where the brain runs is a deployment choice, not an architecture one

The autonomon process may run on-device (Pi Zero 2W) or on a remote host. Heavy
models (e.g. a TFLite person detector) can run wherever autonomon is hosted.
This changes latency/bandwidth/CPU trade-offs but **not** the boundary
contract: raw in, actions out, cognition in between, regardless of host.

## Rationale

**Why keep all cognition in autonomon?**
The four-layer pipeline (ADR-001) exists precisely to own perception → world
model → planning → action. Splitting perception across two repos — some in a
nomothetic endpoint, some in an autonomon layer — defeats the layering: it
scatters modeling logic, forces cross-repo changes for one capability, and
couples autonomy evolution to the gateway's release cycle.

**Why is nomothetic the wrong home for processing?**
nomothetic is a per-device service whose job is hardware access and auth. Adding
vision/ML there bloats every device image with model dependencies, mixes
trust/latency profiles, and means a planner change and a perception change can
no longer ship together. Keeping it a thin gateway keeps it small, testable, and
stable.

**Why state it now?**
Phase 6b is the first capability that needs interpreted (non-raw) input, so it
is the first place the boundary could erode. Settling it as a principle prevents
each future capability (vision, audio, SLAM, multi-sensor fusion) from
re-litigating the same fork — and prevents a drift where half the perception
lives in nomothetic.

**Relationship to the existing hard rule.**
This is the mirror image of the nomopractic/nomothetic rule. Together they make
nomothetic a *pure gateway*: no hardware registers below it (those are
nomopractic's), no autonomy cognition above it (that is autonomon's). nomothetic
is exactly the raw I/O seam between firmware and brain.

## Trade-offs

| Benefit | Cost |
|---------|------|
| All perception/modeling in one repo, one process, one release | Raw frames/data may cross the network to reach autonomon (bandwidth) |
| nomothetic stays small, stable, dependency-light | autonomon carries the model/vision dependencies and their compute |
| A new capability = a new autonomon layer, no cross-repo endpoint | On-device heavy models stress the Pi Zero 2W (mitigated by remote hosting) |
| Boundary is host-independent (on-device or remote) | Some processing that *could* be cheaper at the edge is centralised by policy |

## Alternatives Considered

### Add a vision/processing endpoint to nomothetic (original 6b scoping)

**Rejected.** Pushes cognition into the hardware gateway, scatters perception
across two repos, bloats every device image with ML dependencies, and decouples
a perception change from the planner change that motivates it. Contradicts the
layering ADR-001 establishes.

### Split processing by cost (cheap/edge in nomothetic, heavy in autonomon)

**Rejected.** A moving boundary is worse than a fixed one: "is this cheap
enough to live in the gateway?" is re-argued per feature and drifts over time.
A single, predictable boundary (raw I/O only) is easier to reason about and
enforce in review. Deployment placement (D4) already captures the
cost/latency trade-off without moving the architectural line.

### Push processing further down into nomopractic firmware

**Rejected.** That is the existing HAT-routine model (firmware-side obstacle
avoidance, see ADR-003) — useful for tight reflexes but not a home for
general cognition. Reuses none of the four-layer pipeline and is Rust, not the
autonomy stack.

## Consequences

- Phase 6b's target perception is an **autonomon vision perception layer** that
  pulls raw frames from nomothetic's existing camera endpoints; **no new
  nomothetic endpoint is required** (roadmap Phase 6b updated accordingly).
- The detection model (OpenCV / TFLite / etc.) is an **autonomon** dependency,
  not a nomothetic one.
- nomothetic's API surface is treated as a raw-I/O contract; proposals to add
  *interpretation* endpoints to it must supersede this ADR.
- `architecture.md` states the brain principle and frames the "Nomothetic API
  Surface" as a raw-I/O boundary.
- Future capabilities (audio understanding, SLAM, multi-sensor fusion) follow
  the same rule: new autonomon layers over raw nomothetic I/O.

## References

- ADR-001: Four-Layer Cognitive Architecture (the pipeline this principle protects)
- ADR-002: httpx AsyncClient — raw inputs/outputs cross this client
- ADR-003: Routine Registry (HAT routine vs autonomy routine; firmware reflexes)
- `autonomon/docs/roadmap.md`: "Architecture Principle — autonomon is the brain"; Phase 6b
- Top-level `CLAUDE.md`: the nomopractic/nomothetic hard rule (the mirror boundary below nomothetic)
