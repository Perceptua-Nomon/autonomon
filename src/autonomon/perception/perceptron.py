"""Perceptron: configurable single-sensor perception implementation."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, NamedTuple

import httpx

from autonomon.messages import PerceptionEvent
from autonomon.perception.base import PerceptionBase

logger = logging.getLogger(__name__)

_Interpreter = Callable[[dict[str, Any]], dict[str, Any]]


class _SensorSpec(NamedTuple):
    """Declarative definition of a standard Robot HAT V4 sensor.

    Colocates the three facts that distinguish one built-in sensor from
    another: its REST endpoint, its response-body interpreter, and its
    default poll interval.
    """

    endpoint: str
    interpreter: _Interpreter
    poll_interval_s: float


# All per-sensor knowledge for the built-in sensors lives here, one row each.
_SENSOR_SPECS: dict[str, _SensorSpec] = {
    "ultrasonic": _SensorSpec(
        "/api/sensor/ultrasonic",
        lambda body: {"distance_cm": body["distance_cm"]},
        0.1,
    ),
    "grayscale": _SensorSpec(
        "/api/sensor/grayscale/normalized",
        lambda body: {"channels": body["channels"], "normalized": body["normalized"]},
        0.1,
    ),
    "battery": _SensorSpec(
        "/api/hat/battery",
        lambda body: {"voltage_v": body["voltage_v"]},
        30.0,
    ),
}


class Perceptron(PerceptionBase):
    """A configurable perception layer instance for a single sensor endpoint.

    Declare the sensor type and endpoint URL at construction; all polling,
    timing, error handling, and stop logic is shared. The ``interpreter``
    callable transforms the raw JSON response body into the ``data`` dict
    stored on each ``PerceptionEvent``.

    Use the named class-method constructors (``ultrasonic``, ``grayscale``,
    ``battery``) for the standard Robot HAT V4 sensors, or pass a custom
    ``sensor_type``, ``endpoint``, and ``interpreter`` for any other source.

    Parameters
    ----------
    client : httpx.AsyncClient
        Shared async HTTP client (pre-configured with base_url, auth,
        ``verify=False``).
    device_id : str
        Device identifier included in every ``PerceptionEvent``.
    sensor_type : str
        Sensor category string stored on each ``PerceptionEvent``.
    endpoint : str
        Path on the nomothetic REST API to GET (e.g. ``/api/sensor/ultrasonic``).
    interpreter : callable, optional
        ``(response_body: dict) -> data: dict``. Extracts the normalised
        payload from the raw JSON response. Defaults to the built-in
        interpreter for ``sensor_type`` if one exists, otherwise the full
        response body is used as-is.
    poll_interval_s : float
        Seconds between sensor reads. Default 0.1 s (10 Hz).
    timeout_s : float
        Per-request wall-clock timeout. Default 1.0 s.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        device_id: str,
        sensor_type: str,
        endpoint: str,
        interpreter: _Interpreter | None = None,
        poll_interval_s: float = 0.1,
        timeout_s: float = 1.0,
    ) -> None:
        self._client = client
        self._device_id = device_id
        self._sensor_type = sensor_type
        self._endpoint = endpoint
        spec = _SENSOR_SPECS.get(sensor_type)
        self._interpreter: _Interpreter = (
            interpreter or (spec.interpreter if spec else None) or (lambda body: body)
        )
        self._poll_interval_s = poll_interval_s
        self._timeout_s = timeout_s
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------
    # Named constructors for standard Robot HAT V4 sensors
    # ------------------------------------------------------------------

    @classmethod
    def _from_spec(
        cls,
        sensor_type: str,
        client: httpx.AsyncClient,
        device_id: str,
        poll_interval_s: float | None,
        timeout_s: float,
    ) -> Perceptron:
        """Build a Perceptron for a built-in sensor from its ``_SENSOR_SPECS`` row.

        ``poll_interval_s`` falls back to the sensor's spec default when None.
        """
        spec = _SENSOR_SPECS[sensor_type]
        return cls(
            client,
            device_id,
            sensor_type=sensor_type,
            endpoint=spec.endpoint,
            interpreter=spec.interpreter,
            poll_interval_s=spec.poll_interval_s if poll_interval_s is None else poll_interval_s,
            timeout_s=timeout_s,
        )

    @classmethod
    def ultrasonic(
        cls,
        client: httpx.AsyncClient,
        device_id: str,
        poll_interval_s: float | None = None,
        timeout_s: float = 1.0,
    ) -> Perceptron:
        """Return a Perceptron configured for the ultrasonic distance sensor.

        Polls ``/api/sensor/ultrasonic`` (every 0.1 s by default); emits
        ``data={"distance_cm": float | None}``. ``distance_cm`` is ``None``
        when no object is in range or the echo times out.
        """
        return cls._from_spec("ultrasonic", client, device_id, poll_interval_s, timeout_s)

    @classmethod
    def grayscale(
        cls,
        client: httpx.AsyncClient,
        device_id: str,
        poll_interval_s: float | None = None,
        timeout_s: float = 1.0,
    ) -> Perceptron:
        """Return a Perceptron configured for the normalised grayscale sensors.

        Polls ``/api/sensor/grayscale/normalized`` (every 0.1 s by default);
        emits ``data={"channels": [0, 1, 2], "normalized": [float, float, float]}``.
        Values are 0.0–1.0 (calibrated via the nomothetic calibration API).
        """
        return cls._from_spec("grayscale", client, device_id, poll_interval_s, timeout_s)

    @classmethod
    def battery(
        cls,
        client: httpx.AsyncClient,
        device_id: str,
        poll_interval_s: float | None = None,
        timeout_s: float = 1.0,
    ) -> Perceptron:
        """Return a Perceptron configured for the HAT battery voltage sensor.

        Polls ``/api/hat/battery``; emits ``data={"voltage_v": float}``.
        Defaults to a 30-second poll interval (battery changes slowly).
        """
        return cls._from_spec("battery", client, device_id, poll_interval_s, timeout_s)

    # ------------------------------------------------------------------
    # PerceptionBase implementation
    # ------------------------------------------------------------------

    async def run(self, queue_out: asyncio.Queue) -> None:  # type: ignore[type-arg]
        """Poll the configured endpoint and emit PerceptionEvents until stopped.

        Parameters
        ----------
        queue_out : asyncio.Queue
            Receives ``PerceptionEvent.to_dict()`` items. The ``sensor_type``
            and ``data`` fields are set by this instance's configuration.
        """
        while not self._stop.is_set():
            await self._poll(queue_out)
            await self._interruptible_sleep(self._poll_interval_s)

    async def _poll(self, queue_out: asyncio.Queue) -> None:  # type: ignore[type-arg]
        try:
            resp = await asyncio.wait_for(
                self._client.get(self._endpoint),
                timeout=self._timeout_s,
            )
            resp.raise_for_status()
            body = resp.json()
            event = PerceptionEvent(
                timestamp=body.get("timestamp") or datetime.now(timezone.utc).isoformat(),
                device_id=self._device_id,
                sensor_type=self._sensor_type,
                data=self._interpreter(body),
            )
            await queue_out.put(event.to_dict())
        except asyncio.TimeoutError:
            logger.warning("%s poll timed out after %.1f s", self._sensor_type, self._timeout_s)
        except httpx.RequestError as exc:
            logger.warning("%s poll request error: %s", self._sensor_type, exc)
        except httpx.HTTPStatusError as exc:
            logger.warning("%s poll HTTP %d", self._sensor_type, exc.response.status_code)
        except (KeyError, ValueError, TypeError) as exc:
            # Bad JSON (resp.json()) or a body missing the keys the interpreter
            # expects: log and keep polling rather than letting the layer crash.
            logger.warning("%s poll returned an unusable body: %s", self._sensor_type, exc)

    async def _interruptible_sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def stop(self) -> None:
        self._stop.set()
