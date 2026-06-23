"""Tests for FanInSlot multi-source perception pass-through."""

import asyncio

import pytest

from autonomon import PerceptionBase
from autonomon.fan_in import FanInSlot
from autonomon.messages import PerceptionEvent


class _TaggedPerception(PerceptionBase):
    def __init__(self, tag: str, count: int = 5) -> None:
        self.tag = tag
        self._count = count
        self._stop = asyncio.Event()

    async def run(self, queue_out: asyncio.Queue[PerceptionEvent]) -> None:
        for i in range(self._count):
            if self._stop.is_set():
                break
            ev = PerceptionEvent(
                timestamp="t", device_id="test", sensor_type=self.tag, data={"i": i}
            )
            await queue_out.put(ev)
            await asyncio.sleep(0.005)
        await self._stop.wait()

    async def stop(self) -> None:
        self._stop.set()


@pytest.mark.asyncio
async def test_fan_in_perception_pass_through() -> None:
    """Both perception sources emit to the same queue; all messages reach downstream."""
    src_a = _TaggedPerception("ultrasonic", count=3)
    src_b = _TaggedPerception("grayscale", count=3)
    q_out: asyncio.Queue[PerceptionEvent] = asyncio.Queue(maxsize=64)

    slot = FanInSlot("perception", [src_a, src_b])
    slot.start(queue_in=None, queue_out=q_out)

    await asyncio.sleep(0.15)
    await slot.stop()

    sensor_types = set()
    while not q_out.empty():
        sensor_types.add(q_out.get_nowait().sensor_type)

    assert "ultrasonic" in sensor_types
    assert "grayscale" in sensor_types


@pytest.mark.asyncio
async def test_fan_in_empty_impls_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        FanInSlot("perception", [])


@pytest.mark.asyncio
async def test_fan_in_requires_perception_position() -> None:
    """FanInSlot is a Perception-position construct: queue_in must be None."""
    slot = FanInSlot("world_model", [_TaggedPerception("a")])
    with pytest.raises(ValueError, match="Perception position"):
        slot.start(queue_in=asyncio.Queue(), queue_out=asyncio.Queue())
