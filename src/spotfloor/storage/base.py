"""Storage interface. Business logic depends on this, never on a concrete engine.

This is the seam that lets a DuckDB/Parquet backend replace SQLite for range
queries without the pipeline, evaluator or API noticing. Nothing above this layer
may import a driver, a session, or SQL.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, Sequence

from spotfloor.models import Availability, InstanceOffering, PriceKind


@dataclass(frozen=True, slots=True)
class OfferingFilter:
    """Scopes a read. Every field is optional; None means "any"."""

    provider: str | None = None
    instance_type: str | None = None
    instance_family: str | None = None
    gpu_model: str | None = None
    gpu_count: int | None = None
    region: str | None = None
    zone: str | None = None
    price_kind: PriceKind | None = None
    availability: Availability | None = None
    # True -> only instances with a GPU, False -> only those without, None -> any.
    has_gpu: bool | None = None


@dataclass(frozen=True, slots=True)
class TimeRange:
    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class WriteResult:
    """What a write actually did.

    Dedup tests assert on these counters rather than on row counts, because
    "the table grew by one" is a much weaker claim than "this observation
    extended an existing segment instead of opening a new one".
    """

    inserted: int = 0
    extended: int = 0
    skipped: int = 0


@dataclass(frozen=True, slots=True)
class OfferingRecord:
    """A stored observation *segment*: one state that held over a time interval.

    ``first_seen``/``last_seen`` bound the window over which this exact
    (price, availability) held. That makes "when did it change" a fact in the
    schema rather than something reconstructed by diffing adjacent points.

    This doubles as the *input* type for :meth:`TimeSeriesStore.backfill`, because
    a historical quote already knows the interval it covered -- see that method.
    """

    offering: InstanceOffering
    first_seen: datetime
    last_seen: datetime


class TimeSeriesStore(Protocol):
    """Append-with-dedup storage for offering observations."""

    def write(self, offerings: Sequence[InstanceOffering], *, now: datetime) -> WriteResult:
        """Persist a poll's observations, collapsing unchanged ones into open segments."""
        ...

    def backfill(self, segments: Sequence[OfferingRecord]) -> WriteResult:
        """Persist segments that carry their own timestamps.

        Distinct from :meth:`write` because the two answer different questions.
        ``write`` is told "here is the state *now*" and has to infer where segment
        boundaries fall by comparing against what it already holds. ``backfill`` is
        handed intervals that are already known, which is exactly the shape of
        ``DescribeSpotPriceHistory``: AWS emits a row precisely *when the price
        changes*, so its history is a change-log, not a sample series, and the
        segment boundaries are given rather than inferred.

        Routing history through ``write(now=...)`` instead would stamp 90 days of
        dated quotes with the wall clock and collapse them into one segment.

        Must be idempotent: re-running a backfill over an interval already stored
        is a no-op, counted as ``skipped``. Callers rely on that, because the
        database is a rebuildable cache and a lost cache means backfilling again.
        """
        ...

    def latest(self, filt: OfferingFilter, *, now: datetime) -> list[OfferingRecord]:
        """Current state: the freshest segment per series, excluding stale ones."""
        ...

    def history(self, filt: OfferingFilter, time_range: TimeRange) -> list[OfferingRecord]:
        """Time-ordered series of segments overlapping the range."""
        ...

    def prune(self, before: datetime) -> int:
        """Drop segments that ended before ``before``. Returns rows removed.

        Retention belongs here rather than in a caller: deciding what "ended
        before" means is a statement about the segment model, and a script that
        reached past this protocol to issue its own DELETE would be the first
        thing to break when the backend becomes DuckDB.
        """
        ...

    def close(self) -> None: ...
