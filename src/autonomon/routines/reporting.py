"""Best-effort forwarding of routine lifecycle events to nomothetic.

The CLI (:mod:`autonomon.routines.cli`) already emits NDJSON lifecycle events to
stdout. :class:`StatusReporter` additionally forwards each event to the device's
nomothetic API (``POST /api/routines/{routine}/events``), attributed to the
routine name and a per-run ``run_id``. nomothetic stores them so an operator can
query a routine's status and recent logs — including the error that stopped it.

This is **reporting**, not control: stdout NDJSON remains the source of truth.
Reporting is best-effort — every failure (unreachable device, old nomothetic
without the endpoint, timeout) is swallowed so the routine never crashes because
its status sink is unavailable.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)


def utcnow_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


class StatusReporter:
    """Forward routine lifecycle events to nomothetic, best-effort.

    Parameters
    ----------
    client : httpx.AsyncClient
        The shared device client (already configured with base URL and auth per
        ADR-002). Reused so reporting rides the same authenticated connection as
        sensor/actuator I/O.
    routine : str
        Routine name; forms the endpoint path and attributes the events.
    run_id : str
        Per-run identifier so nomothetic can segment successive runs.
    device_id : str
        Device identifier stamped on each reported event.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        routine: str,
        run_id: str,
        device_id: str,
    ) -> None:
        self._client = client
        self._routine = routine
        self._run_id = run_id
        self._device_id = device_id
        self._path = f"/api/routines/{quote(routine, safe='')}/events"

    async def report(self, event_type: str, data: dict[str, Any]) -> None:
        """Forward one lifecycle event. Never raises.

        Parameters
        ----------
        event_type : str
            Lifecycle event type (``starting``/``running``/``stopping``/
            ``error``) or a free-form type such as ``log``.
        data : dict
            The event payload (the same ``data`` object emitted to stdout).
        """
        payload = {
            "type": event_type,
            "data": data,
            "run_id": self._run_id,
            "device_id": self._device_id,
            "timestamp": utcnow_iso(),
        }
        try:
            await self._client.post(self._path, json=payload)
        except Exception as exc:  # noqa: BLE001 — reporting is best-effort
            logger.debug("status report for %r (%s) failed: %s", self._routine, event_type, exc)
