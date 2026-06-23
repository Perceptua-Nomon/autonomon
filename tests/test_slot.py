"""Tests for LayerSlot lifecycle (start/stop)."""

import asyncio

import pytest

from autonomon import PerceptionBase
from autonomon.slot import LayerSlot, SlotState


class _CountingPerception(PerceptionBase):
    def __init__(self, tag: str) -> None:
        self.tag = tag
        self._stop = asyncio.Event()

    async def run(self, queue_out: asyncio.Queue) -> None:
        count = 0
        while not self._stop.is_set():
            await queue_out.put({"tag": self.tag, "n": count})
            count += 1
            await asyncio.sleep(0.01)

    async def stop(self) -> None:
        self._stop.set()


@pytest.mark.asyncio
async def test_slot_starts_and_stops() -> None:
    impl = _CountingPerception("a")
    q_out: asyncio.Queue = asyncio.Queue()
    slot = LayerSlot("perception", impl)

    assert slot._state == SlotState.STOPPED
    task = slot.start(queue_in=None, queue_out=q_out)
    assert slot._state == SlotState.RUNNING
    assert not task.done()

    await asyncio.sleep(0.05)
    await slot.stop()
    assert slot._state == SlotState.STOPPED
    assert task.done()
    assert not q_out.empty()


@pytest.mark.asyncio
async def test_slot_stop_is_idempotent() -> None:
    """Stopping an already-stopped slot is a no-op, not an error."""
    slot = LayerSlot("perception", _CountingPerception("a"))
    slot.start(queue_in=None, queue_out=asyncio.Queue())
    await slot.stop()
    await slot.stop()  # second stop must not raise
    assert slot._state == SlotState.STOPPED
