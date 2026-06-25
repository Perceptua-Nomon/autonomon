"""RulePlanner: a data-driven, table-based planner loadable from TOML.

``RulePlanner`` generalises :class:`~autonomon.planning.avoidance.AvoidancePlanner`
into an **ordered rule table**: each rule pairs a *condition* over the world state
with an *action sequence*, the first matching rule wins, and a new
:class:`~autonomon.messages.ActionPlan` is emitted only when the selected rule
changes (debounce). It shares the proven control-loop shape of
``AvoidancePlanner`` — the idle-tick re-evaluation, the ``loop.time()`` hold
window (``hold_s``, the generalisation of ``avoid_duration_s``), and the
plan-counter — so its runtime behaviour is identical; only the rule set is data
rather than code.

The table can be supplied in-memory (a list of rule dicts — handy for tests) or
loaded from a TOML file via :meth:`RulePlanner.from_toml`. It is the Phase-4
"rule-based planner" deliverable, introduced together with a concrete consumer
(the ``patrol`` routine) per ADR-006.

Rule shape
----------
Each rule is a dict::

    {
        "name": "avoid",                       # the plan "kind" (debounce key)
        "when": {"obstacle_ahead": True},      # all clauses must hold (AND)
        "any_of": [{"a": True}, {"b": True}],  # optional: at least one holds (OR)
        "actions": [{"method": ..., "params": {...}, "priority": 0}, ...],
        "hold_s": 2.5,                         # optional commit window (default 0)
    }

A clause value is either a bare value (equality) or a ``{op: value}`` map with
``op`` in ``lt``/``le``/``gt``/``ge``/``ne``/``eq``/``in``/``truthy``/``exists``.
A rule with neither ``when`` nor ``any_of`` always matches (a catch-all). If no
rule matches, ``default_actions`` is emitted under ``default_name``.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autonomon.messages import ActionPlan, WorldStateUpdate
from autonomon.planning.base import PlannerBase

if sys.version_info >= (3, 11):  # pragma: no cover - exercised on the runtime in use
    import tomllib
else:  # pragma: no cover - 3.9/3.10 backport path
    import tomli as tomllib

logger = logging.getLogger(__name__)

_QUEUE_GET_TIMEOUT_S = 0.05

# Rule tables shipped inside the package (declared as package-data in
# pyproject.toml). Resolved relative to this module so it works from both an
# editable checkout and an installed wheel.
_RULES_DIR = Path(__file__).resolve().parent / "rules"


def bundled_rules_path(name: str) -> Path:
    """Return the path to a rule table shipped inside ``autonomon.planning.rules``.

    Parameters
    ----------
    name : str
        The table file name, e.g. ``"explore.toml"``.

    Returns
    -------
    pathlib.Path
        Absolute path to the bundled table (existence is not checked here).
    """
    return _RULES_DIR / name


# Default plan emitted when no rule matches the current world state.
_DEFAULT_NAME = "default"
_DEFAULT_ACTIONS: list[dict[str, Any]] = [{"method": "stop", "params": {}, "priority": 0}]


def _coerce_actual(actual: Any) -> float | None:
    """Return ``actual`` as a float for numeric comparison, or None if it isn't one."""
    if isinstance(actual, bool) or actual is None:
        return None
    if isinstance(actual, (int, float)):
        return float(actual)
    return None


def _op_match(op: str, actual: Any, expected: Any) -> bool:
    """Evaluate one ``{op: expected}`` clause against an observed value.

    Numeric operators (``lt``/``le``/``gt``/``ge``) return False when ``actual``
    is absent (None) or non-numeric, so a missing field never matches a numeric
    bound (and never raises). ``truthy``/``exists`` take a bool ``expected``.
    """
    if op == "eq":
        return bool(actual == expected)
    if op == "ne":
        return bool(actual != expected)
    if op == "in":
        return bool(actual in expected)
    if op == "truthy":
        return bool(actual) is bool(expected)
    if op == "exists":
        return (actual is not None) is bool(expected)
    number = _coerce_actual(actual)
    if number is None:
        return False
    if op == "lt":
        return bool(number < expected)
    if op == "le":
        return bool(number <= expected)
    if op == "gt":
        return bool(number > expected)
    if op == "ge":
        return bool(number >= expected)
    raise ValueError(f"unknown rule operator '{op}'")


def _clause_matches(actual: Any, matcher: Any) -> bool:
    """Match one field: a bare value is equality; a ``{op: value}`` map ANDs its ops."""
    if isinstance(matcher, dict):
        return all(_op_match(op, actual, value) for op, value in matcher.items())
    return bool(actual == matcher)


def _all_match(clauses: dict[str, Any], state: dict[str, Any]) -> bool:
    """True when every ``field -> matcher`` clause holds against ``state`` (AND)."""
    return all(_clause_matches(state.get(field), matcher) for field, matcher in clauses.items())


class RulePlanner(PlannerBase):
    """Selects an action plan from an ordered rule table (first match wins).

    Pure logic with no I/O, so it is fully testable by pushing
    :class:`WorldStateUpdate` instances into a queue. A new ``ActionPlan`` is
    emitted only when the selected rule's ``name`` changes (debounce), so the
    action layer is not re-commanded on every world-state tick.

    A selected rule with a positive ``hold_s`` is **committed** for that many
    seconds before the planner re-evaluates — the same mechanism as
    ``AvoidancePlanner.avoid_duration_s``, generalised per rule. The hold is
    re-checked on the idle tick, so the planner releases on schedule without a
    new world update.

    Parameters
    ----------
    device_id : str
        Device identifier included in every ``ActionPlan``.
    rules : list of dict
        Ordered rule table; see the module docstring for the rule shape. The
        first rule whose condition matches the current state is selected.
    default_actions : list of dict, optional
        Action sequence emitted when **no** rule matches. Defaults to a single
        ``stop`` (the safe choice).
    default_name : str
        Plan ``name``/kind used for the no-match default. Default ``"default"``.

    Raises
    ------
    ValueError
        If a rule is missing ``name`` or ``actions``.
    """

    def __init__(
        self,
        device_id: str,
        rules: list[dict[str, Any]],
        default_actions: list[dict[str, Any]] | None = None,
        default_name: str = _DEFAULT_NAME,
    ) -> None:
        self._device_id = device_id
        self._rules = [self._validate_rule(r) for r in rules]
        self._default_actions = (
            default_actions if default_actions is not None else list(_DEFAULT_ACTIONS)
        )
        self._default_name = default_name
        self._last_name: str | None = None
        self._last_state: dict[str, Any] | None = None
        self._hold_until: float | None = None
        self._plan_counter = 0
        self._stop = asyncio.Event()

    @staticmethod
    def _validate_rule(rule: dict[str, Any]) -> dict[str, Any]:
        if "name" not in rule:
            raise ValueError(f"rule missing 'name': {rule!r}")
        if "actions" not in rule:
            raise ValueError(f"rule '{rule.get('name')}' missing 'actions'")
        return rule

    @classmethod
    def from_toml(
        cls,
        path: str | Path,
        device_id: str,
        **kwargs: Any,
    ) -> RulePlanner:
        """Build a ``RulePlanner`` from a TOML rule table.

        The file holds an array of ``[[rules]]`` tables and an optional
        ``[default]`` table::

            [[rules]]
            name = "avoid"
            hold_s = 2.5
            any_of = [{ obstacle_ahead = true }, { cliff_detected = true }]
            actions = [
                { method = "stop",  params = {},                  priority = 0 },
                { method = "drive", params = { speed_pct = -60 }, priority = 1 },
                { method = "steer", params = { angle_deg = 135 }, priority = 2 },
            ]

            [default]
            name = "stop"
            actions = [{ method = "stop", params = {}, priority = 0 }]

        Parameters
        ----------
        path : str or pathlib.Path
            Path to the TOML rule table.
        device_id : str
            Device identifier forwarded to the constructor.
        **kwargs
            Forwarded to :class:`RulePlanner` (e.g. ``default_actions``); values
            parsed from the ``[default]`` table are used only when not overridden
            here.

        Returns
        -------
        RulePlanner
        """
        data = _load_toml(Path(path))
        rules = data.get("rules", [])
        default = data.get("default") or {}
        if "default_actions" not in kwargs and "actions" in default:
            kwargs["default_actions"] = default["actions"]
        if "default_name" not in kwargs and "name" in default:
            kwargs["default_name"] = default["name"]
        return cls(device_id, rules=rules, **kwargs)

    async def run(
        self,
        queue_in: asyncio.Queue[WorldStateUpdate],
        queue_out: asyncio.Queue[ActionPlan],
    ) -> None:
        """Evaluate world state against the rule table and emit plans on change.

        Parameters
        ----------
        queue_in : asyncio.Queue[WorldStateUpdate]
            Source of ``WorldStateUpdate`` instances.
        queue_out : asyncio.Queue[ActionPlan]
            Receives ``ActionPlan`` instances, emitted only when the selected
            rule changes. An active ``hold_s`` window is also re-checked while
            the queue is idle, so the planner releases on schedule.
        """
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            try:
                update = await asyncio.wait_for(queue_in.get(), timeout=_QUEUE_GET_TIMEOUT_S)
            except asyncio.TimeoutError:
                update = None
            if update is not None:
                self._last_state = update.state
            await self._tick(queue_out, loop.time())

    async def _tick(self, queue_out: asyncio.Queue[ActionPlan], now: float) -> None:
        """Select a rule for the latest state and emit it on change.

        While the selected rule's ``hold_s`` window is still open, re-evaluation
        is suppressed so the robot commits to the maneuver (mirrors
        ``AvoidancePlanner``'s avoid hold).
        """
        state = self._last_state
        if state is None:
            return
        if self._hold_until is not None and now < self._hold_until:
            return
        name, actions, hold_s = self._select(state)
        self._hold_until = now + hold_s if hold_s > 0 else None
        if name != self._last_name:
            self._last_name = name
            await queue_out.put(self._build_plan(name, actions))

    def _select(self, state: dict[str, Any]) -> tuple[str, list[dict[str, Any]], float]:
        """Return ``(name, actions, hold_s)`` for the first matching rule, else the default."""
        for rule in self._rules:
            if self._rule_matches(rule, state):
                return rule["name"], rule["actions"], float(rule.get("hold_s", 0.0))
        return self._default_name, self._default_actions, 0.0

    @staticmethod
    def _rule_matches(rule: dict[str, Any], state: dict[str, Any]) -> bool:
        """A rule matches when its ``when`` clauses AND its ``any_of`` group both hold.

        ``when`` (all clauses, AND) defaults to empty (always true). ``any_of``
        (at least one full clause-map, OR) is checked only when present. A rule
        with neither is a catch-all.
        """
        if not _all_match(rule.get("when") or {}, state):
            return False
        any_of = rule.get("any_of")
        if any_of:
            return any(_all_match(clause, state) for clause in any_of)
        return True

    def _build_plan(self, name: str, actions: list[dict[str, Any]]) -> ActionPlan:
        self._plan_counter += 1
        return ActionPlan(
            timestamp=datetime.now(timezone.utc).isoformat(),
            device_id=self._device_id,
            plan_id=f"{name}-{self._plan_counter}",
            actions=list(actions),
        )

    async def stop(self) -> None:
        self._stop.set()


def _load_toml(path: Path) -> dict[str, Any]:
    """Parse a TOML file into a dict, chaining a clear error on failure."""
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except OSError as exc:
        raise ValueError(f"cannot read rule table '{path}': {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid TOML in rule table '{path}': {exc}") from exc
