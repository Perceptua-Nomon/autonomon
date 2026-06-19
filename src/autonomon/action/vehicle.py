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

# Action method -> nomothetic endpoint. Methods absent here are "unknown".
_ENDPOINTS = {
    "drive": "/api/drive",
    "steer": "/api/steer",
    "stop": "/api/hat/motor/stop",
}


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

    This is the only layer permitted to make state-mutating HTTP calls. Per
    ADR-002 it receives a pre-configured ``httpx.AsyncClient`` (base URL,
    bearer token, ``verify=False``) and holds no auth knowledge itself.

    An ``ActionResult`` is produced for every action attempt. If a ``results``
    queue is provided, each result is emitted onto it (the seam for Phase 7
    telemetry); otherwise results are logged only. Transient HTTP failures are
    recorded on the ``ActionResult`` and do not stop the layer.

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
    results : asyncio.Queue or None
        Optional sink for ``ActionResult.to_dict()`` items (Phase 7 telemetry).
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        device_id: str,
        ttl_ms: int = 500,
        timeout_s: float = 2.0,
        results: asyncio.Queue | None = None,  # type: ignore[type-arg]
    ) -> None:
        self._client = client
        self._device_id = device_id
        self._ttl_ms = ttl_ms
        self._timeout_s = timeout_s
        self._results = results
        self._stop = asyncio.Event()

    async def run(self, queue_in: asyncio.Queue) -> None:  # type: ignore[type-arg]
        """Execute incoming ActionPlans until stopped.

        Parameters
        ----------
        queue_in : asyncio.Queue
            Source of ``ActionPlan.to_dict()`` items.
        """
        while not self._stop.is_set():
            try:
                msg = await asyncio.wait_for(queue_in.get(), timeout=_QUEUE_GET_TIMEOUT_S)
            except asyncio.TimeoutError:
                continue
            await self._execute(ActionPlan.from_dict(msg))

    async def _execute(self, plan: ActionPlan) -> None:
        for action in sorted(plan.actions, key=lambda a: a.get("priority", 0)):
            result = await self._dispatch(plan.plan_id, action)
            self._emit_result(result)

    def _emit_result(self, result: ActionResult) -> None:
        """Best-effort emit to the optional telemetry queue; never block actuation."""
        if self._results is None:
            return
        try:
            self._results.put_nowait(result.to_dict())
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

        try:
            resp = await asyncio.wait_for(
                self._client.post(endpoint, json=body), timeout=self._timeout_s
            )
            resp.raise_for_status()
            data = resp.json()
            return self._result(plan_id, action, success=True, data=data)
        except asyncio.TimeoutError:
            return self._result(plan_id, action, success=False, error="request timed out")
        except httpx.HTTPStatusError as exc:
            return self._result(
                plan_id, action, success=False, error=f"HTTP {exc.response.status_code}"
            )
        except httpx.RequestError as exc:
            return self._result(plan_id, action, success=False, error=str(exc))
        except ValueError as exc:
            return self._result(plan_id, action, success=False, error=f"bad response body: {exc}")

    def _body_for(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        """Build the JSON body for a known method. Raises KeyError if a param is missing."""
        if method == "drive":
            return {"speed_pct": params["speed_pct"], "ttl_ms": self._ttl_ms}
        if method == "steer":
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
