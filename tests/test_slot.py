"""Tests for LayerSlot hot-swap behaviour."""

import asyncio

import pytest

from autonomon import PerceptionBase
from autonomon.slot import LayerSlot, SlotState


class _CountingPerception(PerceptionBase):
    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.emitted: list = []
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
async def test_slot_swap_preserves_queue() -> None:
    """Swap replaces impl but keeps the same queue — in-flight messages survive."""
    impl_a = _CountingPerception("a")
    impl_b = _CountingPerception("b")
    q_out: asyncio.Queue = asyncio.Queue(maxsize=128)

    slot = LayerSlot("perception", impl_a)
    slot.start(queue_in=None, queue_out=q_out)

    await asyncio.sleep(0.05)
    await slot.swap(impl_b)
    assert slot.impl is impl_b
    assert slot._state == SlotState.RUNNING

    await asyncio.sleep(0.05)
    await slot.stop()

    tags = set()
    while not q_out.empty():
        tags.add(q_out.get_nowait()["tag"])

    assert "a" in tags
    assert "b" in tags


@pytest.mark.asyncio
async def test_slot_concurrent_swap_serialised() -> None:
    """Two concurrent swap() calls must not race — second awaits first."""
    impl_a = _CountingPerception("a")
    impl_b = _CountingPerception("b")
    impl_c = _CountingPerception("c")
    q_out: asyncio.Queue = asyncio.Queue(maxsize=128)

    slot = LayerSlot("perception", impl_a)
    slot.start(queue_in=None, queue_out=q_out)

    results = []

    async def _swap(to, tag):
        await slot.swap(to)
        results.append(tag)

    await asyncio.gather(_swap(impl_b, "b"), _swap(impl_c, "c"))
    await slot.stop()

    # Both swaps completed without error; final impl is one of the two
    assert slot.impl in (impl_b, impl_c)
    assert set(results) == {"b", "c"}
