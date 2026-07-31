"""The poller's only real claim: it actually fires.

This file exists because it did not. ``add_job(next_run_time=None)`` reads as
"use the trigger's default" but means "add this job **paused**", so
``Poller.start()`` scheduled a job that never ran once. Nothing caught it: every
other test drives ``run_tick`` directly, so the scheduler wiring was the one part
of ingestion with no coverage and the one part that was broken.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime

import pytest

from ec2_spot_prices.ingest.poller import Poller
from ec2_spot_prices.storage.base import WriteResult


class SilentProvider:
    name = "silent"

    def fetch(self) -> list:
        return []


class CountingStore:
    """Counts writes and lets a test block until a given number have landed."""

    def __init__(self) -> None:
        self.writes = 0
        self._reached = threading.Event()
        self._target = 1

    def expect(self, target: int) -> None:
        self._target = target

    def write(self, offerings, *, now: datetime) -> WriteResult:
        self.writes += 1
        if self.writes >= self._target:
            self._reached.set()
        return WriteResult()

    def wait(self, timeout: float) -> bool:
        return self._reached.wait(timeout)

    def latest(self, filt, *, now):  # pragma: no cover - protocol completeness
        return []

    def history(self, filt, time_range):  # pragma: no cover - protocol completeness
        return []

    def close(self) -> None:  # pragma: no cover - protocol completeness
        pass


@pytest.fixture
def poller_store():
    return CountingStore()


def test_start_ticks_immediately_rather_than_after_one_interval(poller_store) -> None:
    """A fresh process must have data now, not in five minutes.

    With a 300s interval, a poller that waits for the first trigger leaves the
    dashboard empty for the whole interval. This asserts the first tick lands
    promptly, which also proves the job was not added paused.
    """
    poller = Poller([SilentProvider()], poller_store, interval_s=300)
    poller.start()
    try:
        assert poller_store.wait(timeout=5.0), "poller never ran a single tick"
    finally:
        poller.stop()

    assert poller_store.writes == 1


def test_it_keeps_ticking_on_the_interval(poller_store) -> None:
    """Not just once at startup -- it must actually repeat."""
    poller_store.expect(3)
    poller = Poller([SilentProvider()], poller_store, interval_s=1)
    poller.start()
    try:
        assert poller_store.wait(timeout=10.0), (
            f"expected 3 ticks, saw {poller_store.writes}"
        )
    finally:
        poller.stop()

    assert poller_store.writes >= 3


def test_stop_is_final(poller_store) -> None:
    """No stray ticks after shutdown -- otherwise a closed store gets written to."""
    poller = Poller([SilentProvider()], poller_store, interval_s=1)
    poller.start()
    assert poller_store.wait(timeout=5.0)
    poller.stop()

    settled = poller_store.writes
    time.sleep(2.5)
    assert poller_store.writes == settled


def test_a_failed_write_is_reported_not_raised() -> None:
    """Storage errors used to escape into the scheduler thread and out of
    POST /api/refresh as a 500. A tick reports the failure and returns."""
    from ec2_spot_prices.ingest.pipeline import run_tick

    class BrokenStore(CountingStore):
        def write(self, offerings, *, now):
            raise RuntimeError("disk on fire")

    report = run_tick([SilentProvider()], BrokenStore())

    assert not report.ok
    assert "disk on fire" in report.failures["store"]
    assert report.write.inserted == 0
