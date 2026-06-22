# ADR-005: File-Based Routine Catalogue Handoff — autonomon and nomothetic Are Standalone Projects

**Status:** Accepted
**Date:** 2026-06-22
**Deciders:** Perceptua

---

## Context

nomothetic must know which autonomy routines a device offers, for two purposes:

1. **Discovery** — serve `GET /api/routines/available` so the app (nomotactic)
   can list routines.
2. **Launch** — spawn the right `nomon-autonomon` CLI for `POST /api/routines/start`.

The first implementation did both by treating autonomon as a Python library of
nomothetic: `routine_catalog.py` ran `from autonomon.routines import nomon_manifest`
**in nomothetic's own process**, and `routine_manager.py` resolved the
`nomon-autonomon` script **beside nomothetic's interpreter**. Both assume autonomon
is installed into nomothetic's virtualenv.

That assumption entangles two projects that are otherwise independent:

- The autonomon deploy had to install autonomon into *nomothetic's* venv. It did
  not (it installed only into autonomon's own venv), so the in-process import
  raised `ImportError`, the catalogue came back empty, and **routines silently
  vanished from the app** — the deploy even reported success because it verified
  the manifest against autonomon's own venv.
- nomothetic's `make install-pi` runs `rm -rf .venv`, so even a correct
  cross-venv install is wiped on the next nomothetic redeploy — the two deploys
  fight over one venv.
- It forces autonomon's full dependency closure (httpx, cryptography, any future
  vision/ML libs) into nomothetic, contradicting "nomothetic stays small and
  dependency-light" (ADR-004).

## Decision

**autonomon and nomothetic are standalone projects with separate virtualenvs that
never import each other. autonomon publishes its routine catalogue to a shared
file at deploy time; nomothetic reads that file to both list and launch routines.**

### D1 — Separate venvs, no cross-imports

Neither project imports the other. nomothetic has no autonomon dependency; the
autonomon deploy never touches nomothetic's venv. Each is installed and tested in
isolation.

### D2 — A file is the contract

autonomon writes a JSON catalogue to an absolute path on the device. The path is
`NOMON_ROUTINE_CATALOG_PATH`, default `/var/lib/nomon/routine_catalog.json` (the
existing shared-state dir, alongside `pairing_secret`). The **same default is set
in both repos' `.env.device` / `.env.device.example`** so the publishing side and
the reading side always agree. The file is world-readable so nomothetic's service
user can read it; only autonomon (at deploy, under sudo) writes it.

### D3 — Catalogue contents

`{ name, version, routines[], params_schema{}, autonomon_bin, published_at }`.
`routines`, `params_schema`, and `version` come straight from the in-process
`nomon_manifest`; `autonomon_bin` is the absolute path to *autonomon's own venv*
`nomon-autonomon` CLI; `published_at` is an ISO-8601 timestamp. The write is
atomic (temp file + rename) so a concurrent reader never sees a partial file.

### D4 — Missing file degrades gracefully

A missing, unreadable, or malformed catalogue yields an **empty** catalogue (no
routines), never an error. A freshly provisioned device — or one with no
autonomon deployed yet — simply offers no routines until autonomon publishes one.
autonomon remains a soft, not hard, dependency of nomothetic.

### D5 — Launch uses the published CLI path

`RoutineManager` resolves the `nomon-autonomon` binary from the catalogue's
`autonomon_bin` (autonomon's own venv), so the gateway needs no autonomon install
to launch a routine. Resolution order: `NOMON_AUTONOMON_BIN` (explicit override)
→ published `autonomon_bin` → a script beside nomothetic's interpreter (legacy
same-venv installs) → the bare name via `PATH`.

## Rationale

The catalogue changes only when autonomon is **redeployed**, so a static file
written at deploy time is sufficient — no runtime IPC, no long-running autonomon
service to query (autonomon only runs while a routine is active). A file is the
lowest-friction contract reachable from both venvs and survives either project's
independent redeploy. Publishing the CLI path in the same file means launch needs
zero operator configuration and no second handoff mechanism.

## Trade-offs

| Benefit | Cost |
|---------|------|
| Two projects, two venvs, two independent deploys — neither fights the other | A redeploy of autonomon is required to refresh the catalogue (acceptable: that is the only time it changes) |
| nomothetic stays dependency-light; no autonomon libs in its venv | A shared on-disk path is now part of the contract (pinned by a shared env default) |
| One file carries both discovery and launch info | The file must be world-readable and kept in sync via the shared default path |

## Alternatives Considered

### Import `autonomon` in nomothetic's process (the original design)

**Rejected.** Requires autonomon in nomothetic's venv — entangles the venvs (the
bug this ADR fixes), bloats nomothetic with autonomon's dependency closure, and
breaks on either project's independent redeploy.

### Install autonomon into nomothetic's venv from the autonomon deploy

**Rejected.** nomothetic's `make install-pi` does `rm -rf .venv`, so the install
is wiped whenever nomothetic redeploys; the two deploys race over one venv. Still
couples nomothetic to autonomon's dependencies.

### Expose the catalogue over HTTP from autonomon

**Rejected.** autonomon is not a long-running service — it is launched on demand
per routine. There is no always-on process to serve a catalogue endpoint, and
standing one up purely for discovery is far heavier than a deploy-time file.

## Consequences

- `autonomon.routines.publish` (new) writes the catalogue; the autonomon deploy
  invokes it (replacing the cross-venv install step).
- nomothetic's `routine_catalog.py` reads the file (no autonomon import);
  `routine_manager.py` resolves the launch CLI from the published `autonomon_bin`.
- Supersedes the ADR-003 D4 note that `AutonomyPluginManager` discovers the
  catalogue by importing a single `nomon_manifest`: the manifest is still the
  source of truth inside autonomon, but nomothetic now reads it via the published
  file rather than an in-process import.
- The brain boundary (ADR-004) is unchanged; this ADR only changes *how* the
  catalogue crosses the autonomon↔nomothetic seam, not what lives on each side.

## References

- ADR-003: Routine Registry (the `nomon_manifest` this ADR publishes to a file)
- ADR-004: autonomon Is the Brain (the gateway boundary this keeps dependency-light)
- nomothetic ADR-019: Plugin Challenge-Response Auth (how the launched CLI authenticates)
- Top-level `CLAUDE.md`: "Cross-Repo Catalog Contract (autonomon ↔ nomothetic)"
