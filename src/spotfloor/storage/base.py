"""Storage interface. Business logic depends on this, never on a concrete engine.

This is the seam that lets a DuckDB/Parquet backend replace SQLite for range
queries without the pipeline, evaluator or API noticing. Nothing above this layer
may import a driver, a session, or SQL.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, Sequence

from spotfloor.models import Availability, GpuOffering, PriceKind


@dataclass(frozen=True, slots=True)
class OfferingFilter:
    """Scopes a read. Every field is optional; None means "any"."""

    provider: str | None = None
    gpu_model: str | None = None
    gpu_count: int | None = None
    region: str | None = None
    price_kind: PriceKind | None = None
    availability: Availability | None = None


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
    """

    offering: GpuOffering
    first_seen: datetime
    last_seen: datetime


class TimeSeriesStore(Protocol):
    """Append-with-dedup storage for offering observations."""

    def write(self, offerings: Sequence[GpuOffering], *, now: datetime) -> WriteResult:
        """Persist a poll's observations, collapsing unchanged ones into open segments."""
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
