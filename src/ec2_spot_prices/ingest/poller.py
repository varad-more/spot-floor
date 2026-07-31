"""Scheduled polling.

``max_instances=1`` and ``coalesce=True`` are load-bearing, not defaults-by-habit:
overlapping ticks would interleave writes to the same series and (from Phase 2)
corrupt the alert engine's consecutive-observation counters, which assume ticks are
serial. If a tick runs long, the next one waits; backed-up runs collapse to one.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Sequence

from apscheduler.schedulers.background import BackgroundScheduler

from ec2_spot_prices.ingest.pipeline import TickReport, run_tick
from ec2_spot_prices.providers.base import Provider
from ec2_spot_prices.storage.base import TimeSeriesStore

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
        """Tick once immediately, then every ``interval_s``.

        ``next_run_time`` must be a real timestamp. Passing ``None`` is not
        "use the trigger's default" -- APScheduler reads it as "add this job
        **paused**", so the poller silently never fires. Omitting it entirely
        would work but defers the first tick by a whole interval, leaving a
        freshly started process with an empty store and a blank dashboard for
        five minutes. Polling now and scheduling from now is what a poller means.
        """
        self._scheduler.add_job(
            self.tick,
            "interval",
            seconds=self._interval_s,
            max_instances=1,
            coalesce=True,
            next_run_time=datetime.now(UTC),
        )
        self._scheduler.start()
        logger.info("poller started; interval=%ds", self._interval_s)

    def stop(self) -> None:
        self._scheduler.shutdown(wait=True)
