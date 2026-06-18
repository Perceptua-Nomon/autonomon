"""Single generic plugin CLI: runs any autonomy routine by name.

This is the one plugin entry point over the routine registry (ADR-003 D2). It:

1. Reads ``NOMON_DEVICE_URL``, ``NOMON_PLUGIN_TOKEN``, and ``NOMON_PLUGIN_PARAMS``
   (JSON) from the environment.
2. Extracts the routine name from the params (a ``routine`` / ``name`` key).
3. Builds the shared ``httpx.AsyncClient`` per ADR-002 (base URL, bearer token,
   ``verify=False``, a request timeout).
4. Looks up the routine factory, builds its :class:`~autonomon.pipeline.Pipeline`,
   and runs it.
5. Emits NDJSON lifecycle events to stdout (``starting`` / ``running`` /
   ``stopping`` / ``error``) per ``architecture.md``.

The token is never logged or echoed. The core logic lives in :func:`run` (async)
and the synchronous :func:`main` wrapper, which are unit-testable without real
environment or network by injecting a client factory.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Callable

import httpx

from autonomon.pipeline import Pipeline
from autonomon.routines.registry import UnknownRoutineError, get_routine

# Per-request timeout for the shared device client (ADR-002).
_REQUEST_TIMEOUT_S = 5.0

# Factory type for the device HTTP client, so tests can inject a mock.
ClientFactory = Callable[[str, str], httpx.AsyncClient]


def emit(event_type: str, data: dict[str, Any]) -> None:
    """Write one NDJSON lifecycle event to stdout and flush.

    Parameters
    ----------
    event_type : str
        One of ``"starting"``, ``"running"``, ``"stopping"``, ``"error"``.
    data : dict
        The event payload (placed under the ``data`` key).
    """
    sys.stdout.write(json.dumps({"type": event_type, "data": data}) + "\n")
    sys.stdout.flush()


def _build_client(device_url: str, token: str) -> httpx.AsyncClient:
    """Build the shared device client per ADR-002 (base URL, bearer, verify=False)."""
    return httpx.AsyncClient(
        base_url=device_url,
        headers={"Authorization": f"Bearer {token}"},
        verify=False,
        timeout=_REQUEST_TIMEOUT_S,
    )


def _routine_name(params: dict[str, Any]) -> str:
    """Extract the routine name from params (``routine`` or ``name`` key)."""
    name = params.get("routine") or params.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError(
            "no routine selected; set a 'routine' (or 'name') key in NOMON_PLUGIN_PARAMS"
        )
    return name


async def run(
    device_url: str,
    token: str,
    params: dict[str, Any],
    device_id: str,
    client_factory: ClientFactory = _build_client,
) -> int:
    """Build and run the selected routine, emitting lifecycle events.

    Parameters
    ----------
    device_url : str
        Base URL of the device's nomothetic REST API (``NOMON_DEVICE_URL``).
    token : str
        Device-scoped JWT (``NOMON_PLUGIN_TOKEN``). Never logged.
    params : dict
        Routine params (parsed ``NOMON_PLUGIN_PARAMS``); selects the routine via
        a ``routine`` / ``name`` key and parameterises its layers.
    device_id : str
        Device identifier stamped on emitted messages.
    client_factory : callable, optional
        ``(device_url, token) -> httpx.AsyncClient``. Injectable for testing;
        defaults to the ADR-002 client builder.

    Returns
    -------
    int
        Process exit code: ``0`` on a clean stop, ``1`` on error.
    """
    emit("starting", {})
    try:
        name = _routine_name(params)
        factory = get_routine(name)
    except (UnknownRoutineError, ValueError) as exc:
        emit("error", {"message": str(exc)})
        return 1

    client = client_factory(device_url, token)
    try:
        pipeline: Pipeline = factory(client, device_id, params)
        emit("running", {"routine": name, "device_id": device_id})
        await pipeline.run()
        emit("stopping", {"reason": "pipeline_completed"})
        return 0
    except asyncio.CancelledError:
        emit("stopping", {"reason": "cancelled"})
        raise
    except Exception as exc:  # noqa: BLE001 — top-level guard: report, never crash silently
        emit("error", {"message": str(exc)})
        return 1
    finally:
        await client.aclose()


def main() -> int:
    """Console-script entry point: read env, run the routine, return an exit code.

    Reads ``NOMON_DEVICE_URL``, ``NOMON_PLUGIN_TOKEN``, ``NOMON_PLUGIN_PARAMS``,
    and the optional ``NOMON_DEVICE_ID`` from the environment. The token is never
    echoed. Returns the process exit code (``0`` clean, ``1`` on error).
    """
    device_url = os.environ.get("NOMON_DEVICE_URL", "")
    token = os.environ.get("NOMON_PLUGIN_TOKEN", "")
    raw_params = os.environ.get("NOMON_PLUGIN_PARAMS", "{}")
    device_id = os.environ.get("NOMON_DEVICE_ID", "nomon")

    if not device_url or not token:
        emit("error", {"message": "NOMON_DEVICE_URL and NOMON_PLUGIN_TOKEN are required"})
        return 1

    try:
        params = json.loads(raw_params)
    except json.JSONDecodeError as exc:
        emit("error", {"message": f"NOMON_PLUGIN_PARAMS is not valid JSON: {exc}"})
        return 1
    if not isinstance(params, dict):
        emit("error", {"message": "NOMON_PLUGIN_PARAMS must be a JSON object"})
        return 1

    try:
        return asyncio.run(run(device_url, token, params, device_id))
    except KeyboardInterrupt:
        emit("stopping", {"reason": "interrupted"})
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
