"""autonomon.routines — the autonomy routine registry and the built-in routines.

A *routine* is a named, built-in behaviour that composes the four autonomy
layers into a single :class:`~autonomon.pipeline.Pipeline`. This subpackage holds
the registry (``ROUTINES`` / :func:`get_routine` / :func:`available_routines`),
the built-in factories (``explore``), and the single ``nomon_manifest`` that
autonomon publishes to a shared file (via :mod:`autonomon.routines.publish`) for
nomothetic to read — the two run from separate venvs and never import each other
(ADR-005).

> An **autonomy routine** (this package) is deliberately distinct from
> nomothetic's HAT-level ``start_routine`` IPC / ``POST /api/routine/start``,
> which command firmware obstacle avoidance inside nomopractic. See ADR-003.
"""

from __future__ import annotations

from typing import Any

from autonomon.routines.explore import EXPLORE_PARAMS_SCHEMA, build_explore
from autonomon.routines.follow_user import FOLLOW_USER_PARAMS_SCHEMA, build_follow_user
from autonomon.routines.registry import (
    ROUTINES,
    RoutineFactory,
    UnknownRoutineError,
    available_routines,
    get_routine,
)

# Per-routine parameter schemas, keyed by routine name. The manifest advertises
# the union of these so the plugin manager can present params for every routine.
_PARAM_SCHEMAS: dict[str, dict[str, dict[str, Any]]] = {
    "explore": EXPLORE_PARAMS_SCHEMA,
    "follow-user": FOLLOW_USER_PARAMS_SCHEMA,
}


def _union_params_schema() -> dict[str, dict[str, Any]]:
    """Merge every routine's param schema into one dict (last write wins on key clash)."""
    union: dict[str, dict[str, Any]] = {}
    for schema in _PARAM_SCHEMAS.values():
        union.update(schema)
    return union


# Single manifest advertising all routines (ADR-003 D2), not one per routine.
# autonomon publishes it to a shared file for nomothetic to read (ADR-005);
# nomothetic never imports autonomon.
nomon_manifest: dict[str, object] = {
    "name": "autonomon",
    "version": "0.2.0",
    "routines": available_routines(),
    "params_schema": _union_params_schema(),
}

__all__ = [
    "ROUTINES",
    "RoutineFactory",
    "UnknownRoutineError",
    "available_routines",
    "get_routine",
    "build_explore",
    "EXPLORE_PARAMS_SCHEMA",
    "build_follow_user",
    "FOLLOW_USER_PARAMS_SCHEMA",
    "nomon_manifest",
]
