"""Tests for the single generic plugin CLI (``nomon-autonomon``).

The CLI's core logic is exercised via :func:`autonomon.routines.cli.run` and
:func:`autonomon.routines.cli.main` with an injected/mock client and a patched
pipeline run — no real environment, no network.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from autonomon.routines import cli


def _parse_events(captured: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in captured.strip().splitlines() if line]


def _mock_client_factory() -> tuple[Any, AsyncMock]:
    client = AsyncMock(spec=httpx.AsyncClient)
    return (lambda device_url, token: client), client


# ---------------------------------------------------------------------------
# run(): lifecycle event sequence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_emits_starting_running_stopping(monkeypatch, capsys) -> None:
    factory, client = _mock_client_factory()

    # Patch the explore factory so the pipeline run returns immediately.
    fake_pipeline = MagicMock()
    fake_pipeline.run = AsyncMock(return_value=None)
    monkeypatch.setattr(cli, "get_routine", lambda name: (lambda c, d, p: fake_pipeline))

    code = await cli.run(
        "https://device:8443",
        "secret-token",
        {"routine": "explore"},
        "nomon-1",
        client_factory=factory,
    )

    assert code == 0
    events = _parse_events(capsys.readouterr().out)
    assert [e["type"] for e in events] == ["starting", "running", "stopping"]
    assert events[1]["data"]["routine"] == "explore"
    assert events[2]["data"]["reason"] == "pipeline_completed"
    fake_pipeline.run.assert_awaited_once()
    client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_accepts_name_key(monkeypatch, capsys) -> None:
    factory, _ = _mock_client_factory()
    fake_pipeline = MagicMock()
    fake_pipeline.run = AsyncMock(return_value=None)
    monkeypatch.setattr(cli, "get_routine", lambda name: (lambda c, d, p: fake_pipeline))

    code = await cli.run("u", "t", {"name": "explore"}, "nomon-1", client_factory=factory)

    assert code == 0
    events = _parse_events(capsys.readouterr().out)
    assert events[1]["data"]["routine"] == "explore"


# ---------------------------------------------------------------------------
# run(): error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_unknown_routine_emits_error(capsys) -> None:
    factory, client = _mock_client_factory()

    code = await cli.run(
        "u", "t", {"routine": "no-such-routine"}, "nomon-1", client_factory=factory
    )

    assert code == 1
    events = _parse_events(capsys.readouterr().out)
    assert [e["type"] for e in events] == ["starting", "error"]
    assert "no-such-routine" in events[1]["data"]["message"]
    # The client is never built when the routine is unknown.
    client.aclose.assert_not_called()


@pytest.mark.asyncio
async def test_run_missing_routine_name_emits_error(capsys) -> None:
    factory, _ = _mock_client_factory()

    code = await cli.run("u", "t", {}, "nomon-1", client_factory=factory)

    assert code == 1
    events = _parse_events(capsys.readouterr().out)
    assert [e["type"] for e in events] == ["starting", "error"]


@pytest.mark.asyncio
async def test_run_pipeline_exception_emits_error(monkeypatch, capsys) -> None:
    factory, client = _mock_client_factory()
    fake_pipeline = MagicMock()
    fake_pipeline.run = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(cli, "get_routine", lambda name: (lambda c, d, p: fake_pipeline))

    code = await cli.run("u", "t", {"routine": "explore"}, "nomon-1", client_factory=factory)

    assert code == 1
    events = _parse_events(capsys.readouterr().out)
    assert [e["type"] for e in events] == ["starting", "running", "error"]
    assert "boom" in events[2]["data"]["message"]
    client.aclose.assert_awaited_once()  # client always closed


@pytest.mark.asyncio
async def test_run_never_logs_token(monkeypatch, capsys) -> None:
    factory, _ = _mock_client_factory()
    fake_pipeline = MagicMock()
    fake_pipeline.run = AsyncMock(return_value=None)
    monkeypatch.setattr(cli, "get_routine", lambda name: (lambda c, d, p: fake_pipeline))

    await cli.run(
        "https://device:8443",
        "super-secret-token",
        {"routine": "explore"},
        "nomon-1",
        client_factory=factory,
    )

    assert "super-secret-token" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# main(): env parsing
# ---------------------------------------------------------------------------


def test_main_missing_env_emits_error(monkeypatch, capsys) -> None:
    monkeypatch.delenv("NOMON_DEVICE_URL", raising=False)
    monkeypatch.delenv("NOMON_PLUGIN_TOKEN", raising=False)

    code = cli.main()

    assert code == 1
    events = _parse_events(capsys.readouterr().out)
    assert events[0]["type"] == "error"


def test_main_invalid_params_json_emits_error(monkeypatch, capsys) -> None:
    monkeypatch.setenv("NOMON_DEVICE_URL", "https://device:8443")
    monkeypatch.setenv("NOMON_PLUGIN_TOKEN", "tok")
    monkeypatch.setenv("NOMON_PLUGIN_PARAMS", "{not json")

    code = cli.main()

    assert code == 1
    events = _parse_events(capsys.readouterr().out)
    assert events[0]["type"] == "error"
    assert "JSON" in events[0]["data"]["message"]


def test_main_non_object_params_emits_error(monkeypatch, capsys) -> None:
    monkeypatch.setenv("NOMON_DEVICE_URL", "https://device:8443")
    monkeypatch.setenv("NOMON_PLUGIN_TOKEN", "tok")
    monkeypatch.setenv("NOMON_PLUGIN_PARAMS", "[1, 2, 3]")

    code = cli.main()

    assert code == 1
    events = _parse_events(capsys.readouterr().out)
    assert events[0]["type"] == "error"


def test_main_runs_routine_via_run(monkeypatch, capsys) -> None:
    monkeypatch.setenv("NOMON_DEVICE_URL", "https://device:8443")
    # Exercise the static-token path explicitly: clear any ambient key so
    # _resolve_auth does not prefer key-based auth on the test machine.
    monkeypatch.delenv("NOMON_PLUGIN_KEY", raising=False)
    monkeypatch.setenv("NOMON_PLUGIN_TOKEN", "tok")
    monkeypatch.setenv("NOMON_PLUGIN_PARAMS", json.dumps({"routine": "explore"}))

    fake_pipeline = MagicMock()
    fake_pipeline.run = AsyncMock(return_value=None)
    monkeypatch.setattr(cli, "get_routine", lambda name: (lambda c, d, p: fake_pipeline))
    monkeypatch.setattr(cli, "_build_client", lambda url, auth: AsyncMock(spec=httpx.AsyncClient))

    code = cli.main()

    assert code == 0
    events = _parse_events(capsys.readouterr().out)
    assert [e["type"] for e in events] == ["starting", "running", "stopping"]
