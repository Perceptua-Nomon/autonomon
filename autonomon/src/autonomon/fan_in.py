"""FanInSlot: runs N concurrent implementations at one pipeline position.

Supports two merge strategies:

PASS_THROUGH
    All implementations write directly to the shared downstream queue.
    Correct for Perception (YOLO + ultrasonic both emit PerceptionEvents)
    or Planning when you want all plans forwarded downstream.

ARBITRATE
    Each implementation writes to a private arbiter queue. An arbitration
    task collects competing outputs within a configurable window and calls
    a user-supplied arbiter function to select the best one, which is then
    forwarded to the downstream queue. Correct for Planning when you want
    to pick the best plan from competing planners.

Fan-out (for WorldModel / Planning / Action positions):
    A dispatcher task copies each incoming message (shallow dict copy) to
    per-impl private queues so every implementation sees every message.
    Private queues are unbounded to prevent a slow impl from blocking the
    dispatcher and thus stalling the upstream layer.
"""

from __future__ import annotations

import asyncio
import enum
import logging
from typing import Any, Callable

from autonomon.slot import AnyLayer

logger = logging.getLogger(__name__)


class MergeStrategy(enum.Enum):
    PASS_THROUGH = "pass_through"
    ARBITRATE = "arbitrate"


class FanInSlot:
    """Runs N concurrent implementations at one layer position.

    Parameters
    ----------
    name : str
        Slot identifier used in log messages and task names.
    impls : list
        Layer implementations to run concurrently. All must satisfy the
        same abstract base class for their position (all PerceptionBase, etc.).
    merge_strategy : MergeStrategy
        How competing outputs are merged onto the downstream queue.
    arbiter : callable or None
        Required when merge_strategy is ARBITRATE. Called as
        ``arbiter(candidates: list[dict]) -> dict`` to select the best
        output from competing implementations within the arbitration window.
    arbitration_window_ms : float
        Time window in milliseconds within which competing outputs are
        collected before the arbiter selects. Ignored for PASS_THROUGH.
    """

    def __init__(
        self,
        name: str,
        impls: list[AnyLayer],
        merge_strategy: MergeStrategy = MergeStrategy.PASS_THROUGH,
        arbiter: Callable[[list[dict[str, Any]]], dict[str, Any]] | None = None,
        arbitration_window_ms: float = 50.0,
    ) -> None:
        if not impls:
            raise ValueError(f"FanInSlot '{name}': impls must be non-empty")
        if merge_strategy is MergeStrategy.ARBITRATE and arbiter is None:
            raise ValueError(
                f"FanInSlot '{name}': arbiter callable is required for ARBITRATE strategy"
            )

        self.name = name
        self._impls: list[AnyLayer] = list(impls)
        self._merge_strategy = merge_strategy
        self._arbiter = arbiter
        self._arbitration_window_s = arbitration_window_ms / 1000.0

        self._queue_in: asyncio.Queue | None = None  # type: ignore[type-arg]
        self._queue_out: asyncio.Queue | None = None  # type: ignore[type-arg]

        # Per-impl private input queues (Strategy B — fan-out of queue_in)
        self._impl_queues: dict[int, asyncio.Queue] = (
            {}
        )  # id(impl) → queue  # type: ignore[type-arg]

        # All running tasks: dispatcher + impl tasks + arbiter task
        self._all_tasks: list[asyncio.Task] = []  # type: ignore[type-arg]
        self._stop_event = asyncio.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(
        self,
        queue_in: asyncio.Queue | None,  # type: ignore[type-arg]
        queue_out: asyncio.Queue | None,  # type: ignore[type-arg]
    ) -> list:  # list[asyncio.Task]
        """Assign queues and start all impl tasks (and dispatcher/arbiter if needed).

        Parameters
        ----------
        queue_in : asyncio.Queue or None
            Incoming queue. None for Perception.
        queue_out : asyncio.Queue or None
            Outgoing queue. None for Action.
        """
        self._queue_in = queue_in
        self._queue_out = queue_out
        self._stop_event.clear()
        self._all_tasks = []

        # Determine the effective queue each impl writes to
        if self._merge_strategy is MergeStrategy.ARBITRATE:
            # Impls write to a shared arbiter collection queue; arbiter picks & forwards
            effective_out: asyncio.Queue | None = asyncio.Queue()  # type: ignore[type-arg]
            arbiter_task = asyncio.create_task(
                self._arbiter_loop(effective_out, self._queue_out),  # type: ignore[arg-type]
                name=f"{self.name}:arbiter",
            )
            self._all_tasks.append(arbiter_task)
        else:
            effective_out = queue_out  # PASS_THROUGH: impls write directly

        # For positions that have a queue_in, start a dispatcher
        if queue_in is not None:
            dispatch_task = asyncio.create_task(
                self._dispatcher_loop(), name=f"{self.name}:dispatcher"
            )
            self._all_tasks.append(dispatch_task)

        # Start each impl
        for impl in self._impls:
            self._start_impl_task(impl, effective_out)

        logger.debug("fan-in slot '%s' started with %d impl(s)", self.name, len(self._impls))
        return list(self._all_tasks)

    async def stop(self, timeout: float = 2.0) -> None:
        """Stop all impl tasks, dispatcher, and arbiter."""
        self._stop_event.set()

        for impl in self._impls:
            try:
                await impl.stop()
            except Exception:
                pass

        for task in list(self._all_tasks):
            if not task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass

        self._all_tasks = []
        self._impl_queues.clear()
        logger.debug("fan-in slot '%s' stopped", self.name)

    # ------------------------------------------------------------------
    # Dynamic impl management
    # ------------------------------------------------------------------

    async def add_impl(self, impl: AnyLayer) -> None:
        """Add a new implementation to the running fan-in slot.

        Safe to call while the slot is running. The new implementation
        immediately begins receiving messages from the dispatcher.

        Parameters
        ----------
        impl : AnyLayer
            New implementation to add (must match the position's base class).
        """
        self._impls.append(impl)
        effective_out: asyncio.Queue | None  # type: ignore[type-arg]
        if self._merge_strategy is MergeStrategy.ARBITRATE:
            # Find the existing arbiter queue from the arbiter task
            # It's the first queue arg to _arbiter_loop — stored implicitly in the closure.
            # Re-use the same _arbiter_queue stored in the first impl's private channel.
            # Simplification: get effective_out from any existing impl task name.
            # The arbiter queue is created in start(); we keep a reference.
            effective_out = self._arbiter_queue
        else:
            effective_out = self._queue_out
        self._start_impl_task(impl, effective_out)
        logger.info("fan-in slot '%s': added impl %s", self.name, type(impl).__name__)

    async def remove_impl(self, impl: AnyLayer) -> None:
        """Remove an implementation from the running fan-in slot.

        Calls the implementation's stop() and awaits its task before removing it.
        At least one implementation must remain.

        Parameters
        ----------
        impl : AnyLayer
            Implementation to remove. Must be present in the slot.
        """
        if impl not in self._impls:
            raise ValueError(f"FanInSlot '{self.name}': impl {impl!r} not found")
        if len(self._impls) == 1:
            raise RuntimeError(
                f"FanInSlot '{self.name}': cannot remove last impl; stop the slot instead"
            )

        # Stop the impl
        try:
            await impl.stop()
        except Exception:
            pass

        # Cancel and remove its task
        impl_id = id(impl)
        task_name = f"{self.name}:impl:{impl_id}"
        for task in list(self._all_tasks):
            if task.get_name() == task_name:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
                self._all_tasks.remove(task)
                break

        # Remove its private queue from the dispatcher target map
        self._impl_queues.pop(impl_id, None)
        self._impls.remove(impl)
        logger.info("fan-in slot '%s': removed impl %s", self.name, type(impl).__name__)

    # ------------------------------------------------------------------
    # Internal: task creation helpers
    # ------------------------------------------------------------------

    def _start_impl_task(
        self,
        impl: AnyLayer,
        effective_out: asyncio.Queue | None,  # type: ignore[type-arg]
    ) -> asyncio.Task:  # type: ignore[type-arg]
        impl_id = id(impl)
        task_name = f"{self.name}:impl:{impl_id}"

        if self._queue_in is None:
            # Perception position: impl.run(queue_out)
            coro = impl.run(effective_out)  # type: ignore[call-arg, arg-type]
        elif self._queue_out is None:
            # Action position: impl.run(queue_in) — each gets its own copy
            private_q: asyncio.Queue = asyncio.Queue()  # type: ignore[type-arg]
            self._impl_queues[impl_id] = private_q
            coro = impl.run(private_q)  # type: ignore[call-arg]
        else:
            # WorldModel / Planning: impl.run(queue_in, queue_out)
            private_q = asyncio.Queue()  # type: ignore[type-arg]
            self._impl_queues[impl_id] = private_q
            coro = impl.run(private_q, effective_out)  # type: ignore[call-arg, arg-type]

        task = asyncio.create_task(coro, name=task_name)
        self._all_tasks.append(task)
        return task

    async def _dispatcher_loop(self) -> None:
        """Copy each incoming message (shallow dict copy) to all per-impl queues."""
        assert self._queue_in is not None
        while not self._stop_event.is_set():
            try:
                msg = await asyncio.wait_for(self._queue_in.get(), timeout=0.05)
                for q in list(self._impl_queues.values()):  # snapshot for safety
                    await q.put(dict(msg))
            except asyncio.TimeoutError:
                pass

    async def _arbiter_loop(
        self,
        arbiter_queue: asyncio.Queue,  # type: ignore[type-arg]
        queue_out: asyncio.Queue | None,  # type: ignore[type-arg]
    ) -> None:
        """Collect competing outputs within the window, pick the best, forward it."""
        self._arbiter_queue = arbiter_queue  # stash for add_impl
        while not self._stop_event.is_set():
            try:
                first = await asyncio.wait_for(arbiter_queue.get(), timeout=0.05)
                candidates = [first]
                deadline = asyncio.get_event_loop().time() + self._arbitration_window_s
                while True:
                    remaining = deadline - asyncio.get_event_loop().time()
                    if remaining <= 0:
                        break
                    try:
                        extra = await asyncio.wait_for(arbiter_queue.get(), timeout=remaining)
                        candidates.append(extra)
                    except asyncio.TimeoutError:
                        break

                assert self._arbiter is not None
                chosen = self._arbiter(candidates)
                if queue_out is not None:
                    await queue_out.put(chosen)
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def tasks(self) -> list:  # list[asyncio.Task]
        return list(self._all_tasks)
