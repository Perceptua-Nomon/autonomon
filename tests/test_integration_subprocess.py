"""Subprocess-level integration test: the real ``nomon-autonomon`` CLI vs a mock device.

Spins a lightweight mock nomothetic HTTP server (sensor reads, ``/api/camera/frame``,
and recorded actuator POSTs), launches the CLI as a **subprocess** for a routine,
and asserts that (a) it emits NDJSON lifecycle events on stdout and (b) sensor reads
drive actuator commands back to the mock. This is the CI-grade end-to-end check that
the autonomy stack wires up and runs outside the test process. No Pi, no network
beyond loopback.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="subprocess SIGINT semantics differ on Windows"
)


class _MockDevice:
    """A loopback mock of the nomothetic device API used by the routines."""

    def __init__(self, ultrasonic_distance_cm: float) -> None:
        self.posts: list[str] = []
        self.gets: list[str] = []
        self._ultrasonic = ultrasonic_distance_cm
        device = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # silence noisy logging
                pass

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                device.gets.append(self.path)
                if self.path == "/api/sensor/ultrasonic":
                    self._json({"distance_cm": device._ultrasonic, "timestamp": "t"})
                elif self.path == "/api/sensor/grayscale":
                    self._json({"channels": [0, 1, 2], "values": [500, 500, 500], "timestamp": "t"})
                elif self.path == "/api/camera/frame":
                    self._raw(b"\xff\xd8\xff\xe0jpeg\xff\xd9", "image/jpeg")
                else:
                    self._json({"timestamp": "t"})

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                length = int(self.headers.get("Content-Length", 0))
                if length:
                    self.rfile.read(length)
                device.posts.append(self.path)
                self._json({"timestamp": "t"})

            def _json(self, obj: dict[str, Any]) -> None:
                self._raw(json.dumps(obj).encode(), "application/json")

            def _raw(self, body: bytes, content_type: str) -> None:
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> _MockDevice:
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()


_ACTUATOR_PATHS = frozenset({"/api/hat/motor/stop", "/api/drive", "/api/steer"})


def _run_routine(
    device: _MockDevice,
    params: dict[str, Any],
    *,
    extra_env: dict[str, str] | None = None,
    run_s: float = 30.0,
) -> tuple[list[dict[str, Any]], int]:
    """Run the CLI subprocess until an actuator command arrives, then SIGINT it.

    Waits for a POST to one of the actuator endpoints (_ACTUATOR_PATHS), not just
    any POST — lifecycle event POSTs (e.g. /api/routines/explore/events) happen
    before the pipeline runs and would otherwise satisfy a naive count check.
    The default timeout (30 s) is generous enough for Pi Zero 2W startup overhead.

    Returns the parsed NDJSON lifecycle events from stdout and the exit code.
    """
    import os

    env = dict(os.environ)
    env.pop("NOMON_PLUGIN_KEY", None)  # force the static-token auth path
    env.update(
        {
            "NOMON_DEVICE_URL": device.url,
            "NOMON_PLUGIN_TOKEN": "test-token",
            "NOMON_DEVICE_ID": "nomon-it",
            "NOMON_PLUGIN_PARAMS": json.dumps(params),
            # Suppress the on-device env file: _load_env_file() runs at CLI startup
            # and would re-inject NOMON_PLUGIN_KEY from /etc/autonomon/autonomon.env,
            # overriding the pop above and switching auth to challenge-response against
            # the mock device (which has no key registered).
            "NOMON_AUTONOMON_ENV_FILE": "/dev/null",
        }
    )
    if extra_env:
        env.update(extra_env)

    proc = subprocess.Popen(
        [sys.executable, "-m", "autonomon.routines.cli"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.time() + run_s
    while time.time() < deadline:
        if any(p in _ACTUATOR_PATHS for p in device.posts):
            break
        time.sleep(0.05)
    proc.send_signal(signal.SIGINT)
    try:
        out, _ = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()

    events = [json.loads(line) for line in out.splitlines() if line.strip()]
    return events, proc.returncode


def test_explore_routine_drives_the_mock_device() -> None:
    # A near ultrasonic reading must propagate Perception -> ... -> Action and
    # command a stop + reverse on the mock device.
    with _MockDevice(ultrasonic_distance_cm=10.0) as device:
        events, _ = _run_routine(device, {"routine": "explore", "obstacle_threshold_cm": 20.0})

    types = [e["type"] for e in events]
    assert "starting" in types
    assert "running" in types
    # The avoid plan is stop -> reverse -> steer; assert the device was commanded.
    assert "/api/hat/motor/stop" in device.posts
    assert "/api/drive" in device.posts


def test_follow_user_routine_pursues_a_fake_target() -> None:
    # A scripted far detection (small box height -> large distance) must drive the
    # robot forward toward the target. The fake-detector env hook avoids needing a model.
    fake = json.dumps([{"cx": 0.5, "cy": 0.5, "w": 0.2, "h": 0.1, "confidence": 0.9}])
    with _MockDevice(ultrasonic_distance_cm=200.0) as device:
        events, _ = _run_routine(
            device,
            {"routine": "follow-user"},
            extra_env={"NOMON_VISION_FAKE_DETECTIONS": fake},
        )

    types = [e["type"] for e in events]
    assert "running" in types
    assert "/api/camera/frame" in device.gets  # the vision layer pulled raw frames
    assert "/api/drive" in device.posts  # the follower drove toward the target
