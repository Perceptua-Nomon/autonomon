"""Tests for routine status reporting (StatusReporter) and its CLI wiring.

No real network: the device client is an AsyncMock; reporters are injected.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from autonomon.routines import cli
from autonomon.routines.reporting import StatusReporter

# ---------------------------------------------------------------------------
# StatusReporter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reporter_posts_event_to_routine_path():
    client = AsyncMock(spec=httpx.AsyncClient)
    reporter = StatusReporter(client, "explore", "run-1", "nomon-1")

    await reporter.report("running", {"routine": "explore"})

    client.post.assert_awaited_once()
    args, kwargs = client.post.call_args
    assert args[0] == "/api/routines/explore/events"
    payload = kwargs["json"]
    assert payload["type"] == "running"
    assert payload["data"] == {"routine": "explore"}
    assert payload["run_id"] == "run-1"
    assert payload["device_id"] == "nomon-1"
    assert payload["timestamp"]


@pytest.mark.asyncio
async def test_reporter_swallows_transport_errors():
    client = AsyncMock(spec=httpx.AsyncClient)
    client.post.side_effect = httpx.ConnectError("connection refused")
    reporter = StatusReporter(client, "explore", "run-1", "nomon-1")

    # Best-effort: a dead/old nomothetic must never crash the routine.
    await reporter.report("error", {"message": "boom"})


@pytest.mark.asyncio
async def test_reporter_url_encodes_routine_name():
    client = AsyncMock(spec=httpx.AsyncClient)
    reporter = StatusReporter(client, "follow user", "r", "d")

    await reporter.report("log", {})

    assert client.post.call_args[0][0] == "/api/routines/follow%20user/events"


# ---------------------------------------------------------------------------
# cli.run() wiring
# ---------------------------------------------------------------------------


def _fake_pipeline() -> MagicMock:
    pipeline = MagicMock()
    pipeline.run = AsyncMock(return_value=None)
    return pipeline


@pytest.mark.asyncio
async def test_run_forwards_lifecycle_events_to_reporter(monkeypatch):
    client = AsyncMock(spec=httpx.AsyncClient)
    monkeypatch.setattr(cli, "get_routine", lambda name: (lambda c, d, p: _fake_pipeline()))

    reported: list[tuple[str, dict[str, Any]]] = []

    class FakeReporter:
        def __init__(self, c, routine, run_id, device_id):
            self.run_id = run_id

        async def report(self, event_type, data):
            reported.append((event_type, data))

    code = await cli.run(
        "https://device:8443",
        "tok",
        {"routine": "explore"},
        "nomon-1",
        client_factory=lambda url, auth: client,
        reporter_factory=FakeReporter,
    )

    assert code == 0
    # starting is stdout-only (before the client/routine exist); the device-bound
    # events are forwarded.
    assert [t for t, _ in reported] == ["running", "stopping"]
    assert reported[0][1]["routine"] == "explore"
    assert reported[0][1]["run_id"]  # a run id is attached


@pytest.mark.asyncio
async def test_run_with_reporting_disabled_still_emits_stdout(monkeypatch, capsys):
    client = AsyncMock(spec=httpx.AsyncClient)
    monkeypatch.setattr(cli, "get_routine", lambda name: (lambda c, d, p: _fake_pipeline()))

    code = await cli.run(
        "u",
        "t",
        {"routine": "explore"},
        "nomon-1",
        client_factory=lambda url, auth: client,
        reporter_factory=None,
    )

    assert code == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    assert [e["type"] for e in events] == ["starting", "running", "stopping"]


@pytest.mark.asyncio
async def test_run_reports_error_event(monkeypatch):
    client = AsyncMock(spec=httpx.AsyncClient)
    pipeline = MagicMock()
    pipeline.run = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(cli, "get_routine", lambda name: (lambda c, d, p: pipeline))

    reported: list[tuple[str, dict[str, Any]]] = []

    class FakeReporter:
        def __init__(self, *a):
            pass

        async def report(self, event_type, data):
            reported.append((event_type, data))

    code = await cli.run(
        "u",
        "t",
        {"routine": "explore"},
        "nomon-1",
        client_factory=lambda url, auth: client,
        reporter_factory=FakeReporter,
    )

    assert code == 1
    assert [t for t, _ in reported] == ["running", "error"]
    assert reported[-1][1]["message"] == "boom"
