"""Routine registry: maps a routine name to its pipeline factory.

A *routine* (an **autonomy routine**, distinct from nomothetic's HAT
``start_routine`` IPC — see ADR-003 D4) is a named, parameterised factory that
returns a fully wired :class:`~autonomon.pipeline.Pipeline`. This module is the
catalogue: it holds the ``name -> factory`` mapping plus lookup helpers. Adding a
behaviour is adding one entry here (plus any new layer implementations it needs).
"""

from __future__ import annotations

from typing import Any, Callable

import httpx

from autonomon.pipeline import Pipeline
from autonomon.routines.explore import build_explore
from autonomon.routines.follow_user import build_follow_user

# A routine factory: (client, device_id, params) -> Pipeline (ADR-003 D1/D3).
RoutineFactory = Callable[[httpx.AsyncClient, str, dict[str, Any]], Pipeline]


class UnknownRoutineError(KeyError):
    """Raised when a routine name is not present in the registry.

    Subclasses :class:`KeyError` so callers may catch either. The message lists
    the available routine names to aid the operator.
    """

    def __init__(self, name: str, available: list[str]) -> None:
        self.name = name
        self.available = available
        super().__init__(
            f"unknown routine '{name}'; available routines: {', '.join(available) or '(none)'}"
        )

    def __str__(self) -> str:
        # KeyError.__str__ wraps the message in repr quotes; override for clarity.
        return str(self.args[0])


# The catalogue. ``explore`` is pure wiring of existing layers; ``follow-user``
# (Phase 6b) adds net-new vision perception, target world model, and pursuit planner.
ROUTINES: dict[str, RoutineFactory] = {
    "explore": build_explore,
    "follow-user": build_follow_user,
}


def available_routines() -> list[str]:
    """Return the sorted list of registered routine names.

    Returns
    -------
    list of str
        The names that :func:`get_routine` accepts.
    """
    return sorted(ROUTINES)


def get_routine(name: str) -> RoutineFactory:
    """Look up a routine factory by name.

    Parameters
    ----------
    name : str
        The routine name (e.g. ``"explore"``).

    Returns
    -------
    RoutineFactory
        The factory ``(client, device_id, params) -> Pipeline``.

    Raises
    ------
    UnknownRoutineError
        If ``name`` is not registered. The error lists the available names.
    """
    try:
        return ROUTINES[name]
    except KeyError as exc:
        raise UnknownRoutineError(name, available_routines()) from exc
