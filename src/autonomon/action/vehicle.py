"""VehicleAction: executes ActionPlans against the nomothetic REST API."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from autonomon.action.base import ActionBase
from autonomon.messages import ActionPlan, ActionResult

logger = logging.getLogger(__name__)

_QUEUE_GET_TIMEOUT_S = 0.05

# Fraction of the lease TTL at which an idle plan is re-issued to keep the
# nomopractic motor/servo lease alive (see VehicleAction's lease-renewal note).
# Half the TTL leaves comfortable margin for request latency and the watchdog's
# poll granularity.
_RENEW_FRACTION = 0.5

# Action method -> nomothetic endpoint. Methods absent here are "unknown".
_ENDPOINTS = {
    "drive": "/api/drive",
    "steer": "/api/steer",
    "stop": "/api/hat/motor/stop",
    "pan": "/api/camera/pan",
    "tilt": "/api/camera/tilt",
}

# Methods that set motion. If one of these fails to reach the device (timeout,
# connection error, or a 5xx that survives retries), the robot may keep coasting
# on its last lease, so a safety stop is issued before recording the failure.
_MOTION_METHODS = frozenset({"drive", "steer"})


class VehicleAction(ActionBase):
    """Executes ``ActionPlan`` actions by POSTing to the nomothetic vehicle API.

    Each action is a ``{"method", "params", "priority"}`` dict. Actions are
    executed in ascending priority order. Supported methods map to nomothetic
    endpoints:

    | method  | endpoint                | body                          |
    |---------|-------------------------|-------------------------------|
    | ``drive`` | ``POST /api/drive``   | ``{"speed_pct", "ttl_ms"}``   |
    | ``steer`` | ``POST /api/steer``   | ``{"angle_deg", "ttl_ms"}``   |
    | ``stop``  | ``POST /api/hat/motor/stop`` | (none)                 |
    | ``pan``   | ``POST /api/camera/pan``  | ``{"angle_deg", "ttl_ms"}`` |
    | ``tilt``  | ``POST /api/camera/tilt`` | ``{"angle_deg", "ttl_ms"}`` |

    ``pan`` and ``tilt`` move the camera servos only; a failure to land one does
    not endanger the robot, so (unlike ``drive``/``steer``) it does not trigger a
    motor safety stop.

    This is the only layer permitted to make state-mutating HTTP calls. Per
    ADR-002 it receives a pre-configured ``httpx.AsyncClient`` (base URL,
    bearer token, ``verify=False``) and holds no auth knowledge itself.

    An ``ActionResult`` is produced for every action attempt. If a ``results``
    queue is provided, each result is emitted onto it (the seam for Phase 7
    telemetry); otherwise results are logged only. Transient HTTP failures are
    recorded on the ``ActionResult`` and do not stop the layer.

    **Retry with backoff.** Transient failures (request timeout, connection error,
    or a 5xx response) are retried up to ``max_retries`` times with exponential
    backoff (``backoff_base_s * 2**attempt``). A 4xx response is not retried (the
    command was rejected; retrying will not help), and a 2xx with an unparseable
    body is recorded as a failure without retry.

    **Safety stop.** If a ``drive``/``steer`` command still fails to reach the
    device after retries (timeout, connection error, or 5xx), the robot may keep
    coasting on its last actuator lease, so a best-effort ``POST
    /api/hat/motor/stop`` is issued before the failed ``ActionResult`` is recorded.
    A failed ``stop`` is itself the safety action, so it is retried but does not
    trigger a further stop.

    **Lease renewal.** ``drive`` and ``steer`` commands carry a TTL (``ttl_ms``):
    nomopractic runs a watchdog that idles any motor and zeroes any steering
    servo whose lease elapses without a refresh — a safety stop for a crashed
    controller. The upstream layers are edge-triggered (the world model emits
    only on change; the planner debounces on strategy), so in steady state — for
    example cruising across an open floor — no new plan arrives, the lease
    expires, and the robot stalls until the world *changes*. To keep moving, this
    layer re-issues the most recent plan whenever no new one has arrived within
    ``renew_interval_s``, renewing the lease. When the routine stops or the
    process dies, renewal stops too and the watchdog halts the robot, so the
    safety property is preserved.

    Parameters
    ----------
    client : httpx.AsyncClient
        Shared async HTTP client, pre-configured per ADR-002.
    device_id : str
        Device identifier included in every ``ActionResult``.
    ttl_ms : int
        Lease TTL sent with drive/steer commands. Default 500 ms.
    timeout_s : float
        Per-request wall-clock timeout. Default 2.0 s.
    max_retries : int
        Maximum retries on a transient failure (timeout / connection error / 5xx)
        per action, beyond the initial attempt. Default 2 (so up to 3 attempts).
    backoff_base_s : float
        Base delay for exponential backoff between retries
        (``backoff_base_s * 2**attempt``). Default 0.1 s.
    results : asyncio.Queue[ActionResult] or None
        Optional sink for ``ActionResult`` instances (the Phase 7 telemetry seam).
    renew_interval_s : float or None
        Seconds of idle (no new plan) after which the current plan is re-issued
        to keep the actuator lease alive. Defaults to half of ``ttl_ms`` (e.g.
        0.25 s for the 500 ms default), comfortably inside the TTL. Must be
        shorter than ``ttl_ms / 1000`` to be effective.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        device_id: str,
        ttl_ms: int = 500,
        timeout_s: float = 2.0,
        max_retries: int = 2,
        backoff_base_s: float = 0.1,
        results: asyncio.Queue[ActionResult] | None = None,
        renew_interval_s: float | None = None,
    ) -> None:
        self._client = client
        self._device_id = device_id
        self._ttl_ms = ttl_ms
        self._timeout_s = timeout_s
        self._max_retries = max_retries
        self._backoff_base_s = backoff_base_s
        self._results = results
        self._renew_interval_s = (
            renew_interval_s
            if renew_interval_s is not None
            else (ttl_ms / 1000.0) * _RENEW_FRACTION
        )
        self._last_plan: ActionPlan | None = None
        self._last_command_monotonic = 0.0
        self._stop = asyncio.Event()

    async def run(self, queue_in: asyncio.Queue[ActionPlan]) -> None:
        """Execute incoming ActionPlans until stopped, renewing leases while idle.

        A new plan is executed and retained. While no new plan arrives, the
        retained plan is re-issued every ``renew_interval_s`` to keep the
        actuator lease alive (see the class docstring's lease-renewal note);
        without this the robot stalls between world-state changes.

        Parameters
        ----------
        queue_in : asyncio.Queue[ActionPlan]
            Source of ``ActionPlan`` instances.
        """
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            try:
                plan = await asyncio.wait_for(queue_in.get(), timeout=_QUEUE_GET_TIMEOUT_S)
            except asyncio.TimeoutError:
                await self._renew_if_due(loop.time())
                continue
            self._last_plan = plan
            await self._execute(self._last_plan)
            self._last_command_monotonic = loop.time()

    async def _renew_if_due(self, now: float) -> None:
        """Re-issue the current plan if its lease is due for renewal.

        Re-issuing the whole plan renews every lease it set (drive and steer). An
        ``avoid`` plan's leading ``stop`` is harmless on re-issue: the reverse
        ``drive`` that follows in the same plan immediately re-establishes the
        motor lease.

        Parameters
        ----------
        now : float
            Current event-loop monotonic time (``loop.time()``).
        """
        if self._last_plan is None:
            return
        if now - self._last_command_monotonic < self._renew_interval_s:
            return
        await self._execute(self._last_plan)
        self._last_command_monotonic = now

    async def _execute(self, plan: ActionPlan) -> None:
        for action in sorted(plan.actions, key=lambda a: a.get("priority", 0)):
            result = await self._dispatch(plan.plan_id, action)
            self._emit_result(result)

    def _emit_result(self, result: ActionResult) -> None:
        """Best-effort emit to the optional telemetry queue; never block actuation."""
        if self._results is None:
            return
        try:
            self._results.put_nowait(result)
        except asyncio.QueueFull:
            logger.warning("results queue full; dropping ActionResult for plan %s", result.plan_id)

    async def _dispatch(self, plan_id: str, action: dict[str, Any]) -> ActionResult:
        method = action.get("method", "")
        params = action.get("params", {})
        endpoint = _ENDPOINTS.get(method)
        if endpoint is None:
            return self._result(plan_id, action, success=False, error=f"unknown method '{method}'")
        try:
            body = self._body_for(method, params)
        except KeyError as exc:
            return self._result(plan_id, action, success=False, error=f"missing param {exc}")

        result, device_unreachable = await self._post_with_retries(plan_id, action, endpoint, body)
        if device_unreachable and method in _MOTION_METHODS:
            await self._safety_stop()
        return result

    async def _post_with_retries(
        self,
        plan_id: str,
        action: dict[str, Any],
        endpoint: str,
        body: dict[str, Any] | None,
    ) -> tuple[ActionResult, bool]:
        """POST one action, retrying transient failures with exponential backoff.

        Returns the ``ActionResult`` and a flag that is True only when the command
        did **not** reach the device (timeout / connection error / 5xx after
        retries) — the condition under which a motion command warrants a safety
        stop. A rejected request (4xx) or an unparseable 2xx body returns False:
        the device was reached, so no safety stop is needed.
        """
        last_error = "no attempt made"
        for attempt in range(self._max_retries + 1):
            try:
                resp = await asyncio.wait_for(
                    self._client.post(endpoint, json=body), timeout=self._timeout_s
                )
                resp.raise_for_status()
                data = resp.json()
                return self._result(plan_id, action, success=True, data=data), False
            except asyncio.TimeoutError:
                last_error = "request timed out"
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                last_error = f"HTTP {status}"
                if status < 500:
                    # Client error: the device rejected the command; retry won't help.
                    return self._result(plan_id, action, success=False, error=last_error), False
            except httpx.RequestError as exc:
                last_error = str(exc)
            except ValueError as exc:
                # 2xx with an unparseable body: the device was reached.
                msg = f"bad response body: {exc}"
                return self._result(plan_id, action, success=False, error=msg), False
            if attempt < self._max_retries:
                await self._backoff_sleep(attempt)
        return self._result(plan_id, action, success=False, error=last_error), True

    async def _backoff_sleep(self, attempt: int) -> None:
        delay = self._backoff_base_s * (2**attempt)
        if delay > 0:
            await asyncio.sleep(delay)

    async def _safety_stop(self) -> None:
        """Best-effort motor stop after a motion command failed to reach the device.

        Swallows every error: the routine is already in a fault path, and if this
        stop also fails to land the nomopractic watchdog idles the motors once the
        lease lapses. Issued as a single attempt (no retry) to halt promptly.
        """
        logger.warning("motion command failed to reach device; issuing safety stop")
        try:
            await asyncio.wait_for(
                self._client.post(_ENDPOINTS["stop"], json=None), timeout=self._timeout_s
            )
        except Exception as exc:  # noqa: BLE001 — safety stop is best-effort
            logger.warning("safety stop did not land: %s", exc)

    def _body_for(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        """Build the JSON body for a known method. Raises KeyError if a param is missing."""
        if method == "drive":
            return {"speed_pct": params["speed_pct"], "ttl_ms": self._ttl_ms}
        if method in ("steer", "pan", "tilt"):
            return {"angle_deg": params["angle_deg"], "ttl_ms": self._ttl_ms}
        return None  # stop: no body

    def _result(
        self,
        plan_id: str,
        action: dict[str, Any],
        success: bool,
        data: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> ActionResult:
        if not success:
            logger.warning("action %s failed: %s", action.get("method"), error)
        return ActionResult(
            timestamp=datetime.now(timezone.utc).isoformat(),
            device_id=self._device_id,
            plan_id=plan_id,
            action=action,
            success=success,
            data=data if data is not None else {},
            error=error,
        )

    async def stop(self) -> None:
        self._stop.set()
