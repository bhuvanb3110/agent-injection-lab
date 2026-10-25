"""Time source for the scheduler, injectable so tests never really sleep."""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float:
        """Seconds since the Unix epoch. Wall-clock, so daily usage survives a restart."""
        ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class FakeClock:
    """A clock that only moves when told to. `sleep` advances it instantly and is recorded."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:  # 2027-01-15, any fixed time
        self._now = start
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self._now += seconds

    def advance(self, seconds: float) -> None:
        self._now += seconds
