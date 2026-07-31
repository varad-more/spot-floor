"""One poll cycle: fetch every provider, persist, report.

A tick takes a single ``now`` and threads it through fetch, write and (from Phase 2)
alert evaluation. One clock reading per tick is what makes dedup deterministic --
if each provider stamped its own ``now``, identical observations would land at
different timestamps and the gap logic would be comparing apples to clocks.

A provider that fails does not fail the tick. Vast returning a 503 must not take
AWS pricing down with it; the failure is recorded and the tick proceeds with what
it has.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Sequence

from ec2_spot_prices.providers.base import Provider
from ec2_spot_prices.storage.base import TimeSeriesStore, WriteResult

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TickReport:
    started_at: datetime
    fetched: dict[str, int] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)
    write: WriteResult = field(default_factory=WriteResult)

    @property
    def ok(self) -> bool:
        return not self.failures


def run_tick(
    providers: Sequence[Provider],
    store: TimeSeriesStore,
    *,
    now: datetime | None = None,
) -> TickReport:
    """Poll all providers once and persist the result."""
    now = now or datetime.now(UTC)
    report = TickReport(started_at=now)

    offerings = []
    for provider in providers:
        try:
            fetched = provider.fetch()
        except Exception as exc:  # noqa: BLE001 - one bad provider must not kill the tick
            logger.exception("provider %s failed", provider.name)
            report.failures[provider.name] = str(exc)
            continue
        report.fetched[provider.name] = len(fetched)
        offerings.extend(fetched)

    # Guarded for the same reason `fetch` is: this ran unprotected, so any storage
    # error escaped into the scheduler thread (killing the tick) or out of
    # POST /api/refresh as a 500. A failed write is a reported failure, not a crash.
    try:
        report.write = store.write(offerings, now=now)
    except Exception as exc:  # noqa: BLE001 - a bad write must not kill the poll loop
        logger.exception("store.write failed")
        report.failures["store"] = str(exc)
    logger.info(
        "tick fetched=%s inserted=%d extended=%d skipped=%d failures=%s",
        report.fetched,
        report.write.inserted,
        report.write.extended,
        report.write.skipped,
        list(report.failures),
    )
    return report
