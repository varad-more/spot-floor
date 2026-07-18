"""Scheduled polling.

``max_instances=1`` and ``coalesce=True`` are load-bearing, not defaults-by-habit:
overlapping ticks would interleave writes to the same series and (from Phase 2)
corrupt the alert engine's consecutive-observation counters, which assume ticks are
serial. If a tick runs long, the next one waits; backed-up runs collapse to one.
"""

from __future__ import annotations

import logging
from typing import Sequence

from apscheduler.schedulers.background import BackgroundScheduler

from spotfloor.ingest.pipeline import TickReport, run_tick
from spotfloor.providers.base import Provider
from spotfloor.storage.base import TimeSeriesStore

logger = logging.getLogger(__name__)


class Poller:
    """Runs :func:`run_tick` on an interval."""

    def __init__(
        self,
        providers: Sequence[Provider],
        store: TimeSeriesStore,
        *,
        interval_s: int = 300,
    ) -> None:
        self._providers = providers
        self._store = store
        self._interval_s = interval_s
        self._scheduler = BackgroundScheduler()

    def tick(self) -> TickReport:
        return run_tick(self._providers, self._store)

    def start(self) -> None:
        self._scheduler.add_job(
            self.tick,
            "interval",
            seconds=self._interval_s,
            max_instances=1,
            coalesce=True,
            next_run_time=None,
        )
        self._scheduler.start()
        logger.info("poller started; interval=%ds", self._interval_s)

    def stop(self) -> None:
        self._scheduler.shutdown(wait=True)
