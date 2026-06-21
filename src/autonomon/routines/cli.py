"""Single generic plugin CLI: runs any autonomy routine by name.

This is the one plugin entry point over the routine registry (ADR-003 D2). It:

1. Reads ``NOMON_DEVICE_URL`` and ``NOMON_PLUGIN_PARAMS`` (JSON) from the
   environment, plus credentials (see below).
2. Extracts the routine name from the params (a ``routine`` / ``name`` key).
3. Builds the shared ``httpx.AsyncClient`` per ADR-002 (base URL, ``verify=False``,
   a request timeout) with one of two auth modes.
4. Looks up the routine factory, builds its :class:`~autonomon.pipeline.Pipeline`,
   and runs it.
5. Emits NDJSON lifecycle events to stdout (``starting`` / ``running`` /
   ``stopping`` / ``error``) per ``architecture.md``.

Auth modes (preferred first):

* **Key-based (nomothetic ADR-019):** if ``NOMON_PLUGIN_KEY`` points at an Ed25519 private
  key, the client uses :class:`~autonomon.plugin_auth.PluginTokenAuth` to acquire
  and refresh a device JWT via challenge-response. No token is ever on disk.
* **Static token:** if ``NOMON_PLUGIN_TOKEN`` is set, it is used as a bearer
  token directly (manual/testing fallback).

The token/key is never logged or echoed. The core logic lives in :func:`run`
(async) and the synchronous :func:`main` wrapper, which are unit-testable without
real environment or network by injecting a client factory.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from typing import Any, Callable, Union

import httpx

from autonomon.pipeline import Pipeline
from autonomon.routines.registry import UnknownRoutineError, get_routine
from autonomon.routines.reporting import StatusReporter

# Per-request timeout for the shared device client (ADR-002).
_REQUEST_TIMEOUT_S = 5.0

# An auth value is either a bearer-token string or a prepared httpx.Auth flow.
AuthValue = Union[str, httpx.Auth]

# Factory type for the device HTTP client, so tests can inject a mock.
ClientFactory = Callable[[str, AuthValue], httpx.AsyncClient]

# Factory for the status reporter, so tests can inject one or disable reporting
# (pass ``None``). ``(client, routine, run_id, device_id) -> StatusReporter``.
ReporterFactory = Callable[[httpx.AsyncClient, str, str, str], StatusReporter]


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


def _build_client(device_url: str, auth: AuthValue) -> httpx.AsyncClient:
    """Build the shared device client per ADR-002 (base URL, verify=False).

    *auth* is either a bearer-token string (set as a static ``Authorization``
    header) or an :class:`httpx.Auth` flow (e.g.
    :class:`~autonomon.plugin_auth.PluginTokenAuth`, which acquires/refreshes a
    device JWT per request).
    """
    if isinstance(auth, str):
        return httpx.AsyncClient(
            base_url=device_url,
            headers={"Authorization": f"Bearer {auth}"},
            verify=False,
            timeout=_REQUEST_TIMEOUT_S,
        )
    return httpx.AsyncClient(
        base_url=device_url,
        auth=auth,
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
    auth: AuthValue,
    params: dict[str, Any],
    device_id: str,
    client_factory: ClientFactory = _build_client,
    reporter_factory: ReporterFactory | None = StatusReporter,
) -> int:
    """Build and run the selected routine, emitting lifecycle events.

    Lifecycle events are always written to stdout as NDJSON. Once the routine
    is selected and the device client exists, they are *also* forwarded to
    nomothetic (best-effort) so its status/log endpoints reflect this run.

    Parameters
    ----------
    device_url : str
        Base URL of the device's nomothetic REST API (``NOMON_DEVICE_URL``).
    auth : str or httpx.Auth
        Either a bearer-token string or a prepared auth flow (e.g.
        :class:`~autonomon.plugin_auth.PluginTokenAuth`). Never logged.
    params : dict
        Routine params (parsed ``NOMON_PLUGIN_PARAMS``); selects the routine via
        a ``routine`` / ``name`` key and parameterises its layers.
    device_id : str
        Device identifier stamped on emitted messages.
    client_factory : callable, optional
        ``(device_url, token) -> httpx.AsyncClient``. Injectable for testing;
        defaults to the ADR-002 client builder.
    reporter_factory : callable or None, optional
        ``(client, routine, run_id, device_id) -> StatusReporter`` used to
        forward events to nomothetic. Pass ``None`` to disable forwarding
        (stdout NDJSON is unaffected). Defaults to :class:`StatusReporter`.

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

    run_id = uuid.uuid4().hex
    client = client_factory(device_url, auth)
    reporter = reporter_factory(client, name, run_id, device_id) if reporter_factory else None

    async def report(event_type: str, data: dict[str, Any]) -> None:
        """Emit to stdout (source of truth) and forward to nomothetic."""
        emit(event_type, data)
        if reporter is not None:
            await reporter.report(event_type, data)

    try:
        pipeline: Pipeline = factory(client, device_id, params)
        await report("running", {"routine": name, "device_id": device_id, "run_id": run_id})
        await pipeline.run()
        await report("stopping", {"reason": "pipeline_completed"})
        return 0
    except asyncio.CancelledError:
        await report("stopping", {"reason": "cancelled"})
        raise
    except Exception as exc:  # noqa: BLE001 — top-level guard: report, never crash silently
        await report("error", {"message": str(exc)})
        return 1
    finally:
        await client.aclose()


def _resolve_auth(device_url: str) -> AuthValue | None:
    """Resolve device auth from the environment, preferring key-based auth.

    Returns ``None`` (and emits an ``error`` event) if no usable credential is
    configured. ``NOMON_PLUGIN_KEY`` (Ed25519 private key path) takes precedence
    over a static ``NOMON_PLUGIN_TOKEN``.
    """
    key_path = os.environ.get("NOMON_PLUGIN_KEY", "")
    if key_path:
        # Imported lazily so the static-token path needs no cryptography import.
        from autonomon.plugin_auth import PluginTokenAuth, load_private_key

        plugin_name = os.environ.get("NOMON_PLUGIN_NAME", "autonomon")
        try:
            private_key = load_private_key(key_path)
        except (OSError, ValueError) as exc:
            emit("error", {"message": f"could not load NOMON_PLUGIN_KEY: {exc}"})
            return None
        return PluginTokenAuth(device_url, plugin_name, private_key, verify=False)

    token = os.environ.get("NOMON_PLUGIN_TOKEN", "")
    if token:
        return token

    emit(
        "error",
        {"message": "set NOMON_PLUGIN_KEY (preferred) or NOMON_PLUGIN_TOKEN for device auth"},
    )
    return None


def main() -> int:
    """Console-script entry point: read env, run the routine, return an exit code.

    Reads ``NOMON_DEVICE_URL``, ``NOMON_PLUGIN_PARAMS``, the optional
    ``NOMON_DEVICE_ID``, and credentials (``NOMON_PLUGIN_KEY`` preferred, else
    ``NOMON_PLUGIN_TOKEN``). Secrets are never echoed. Returns the process exit
    code (``0`` clean, ``1`` on error).
    """
    device_url = os.environ.get("NOMON_DEVICE_URL", "")
    raw_params = os.environ.get("NOMON_PLUGIN_PARAMS", "{}")
    device_id = os.environ.get("NOMON_DEVICE_ID", "nomon")

    if not device_url:
        emit("error", {"message": "NOMON_DEVICE_URL is required"})
        return 1

    try:
        params = json.loads(raw_params)
    except json.JSONDecodeError as exc:
        emit("error", {"message": f"NOMON_PLUGIN_PARAMS is not valid JSON: {exc}"})
        return 1
    if not isinstance(params, dict):
        emit("error", {"message": "NOMON_PLUGIN_PARAMS must be a JSON object"})
        return 1

    auth = _resolve_auth(device_url)
    if auth is None:
        return 1

    try:
        return asyncio.run(run(device_url, auth, params, device_id))
    except KeyboardInterrupt:
        emit("stopping", {"reason": "interrupted"})
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
