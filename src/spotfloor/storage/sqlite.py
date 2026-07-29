"""SQLite time-series store with segment-based dedup.

**Segments, not points.** A row is not "the price at time T" but "this exact
(price, availability) held from ``first_seen`` to ``last_seen``". An unchanged
observation extends the open segment; a change opens a new one. So the table grows
with *change*, not with time, and "when did it change" is answered by reading a row
rather than by diffing a point series.

**Floats are never compared with ``=``.** Prices arrive as JSON floats and
``0.9950001`` is not ``0.995``, so a naive equality check would open a new segment
on every poll and destroy dedup. Change detection runs on ``state_hash``, computed
from integer-quantized values, making it exact.

Timestamps are stored as epoch integers rather than ISO text: window arithmetic
becomes plain integer math that ports to DuckDB unchanged.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from typing import Any, Sequence

from spotfloor.models import Availability, GpuOffering, PriceKind
from spotfloor.storage.base import (
    OfferingFilter,
    OfferingRecord,
    TimeRange,
    WriteResult,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS offering_observation (
    id                 INTEGER PRIMARY KEY,
    series_key         TEXT    NOT NULL,
    state_hash         TEXT    NOT NULL,
    provider           TEXT    NOT NULL,
    external_id        TEXT,
    instance_type      TEXT    NOT NULL,
    gpu_model          TEXT    NOT NULL,
    gpu_count          INTEGER NOT NULL,
    region             TEXT    NOT NULL,
    price_kind         TEXT    NOT NULL,
    price_usd_hr       REAL    NOT NULL,
    price_per_gpu_hr   REAL    NOT NULL,
    availability       TEXT    NOT NULL,
    availability_score REAL,
    first_seen         INTEGER NOT NULL,
    last_seen          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_series_lastseen
    ON offering_observation(series_key, last_seen DESC, id DESC);
CREATE INDEX IF NOT EXISTS ix_lookup
    ON offering_observation(gpu_model, gpu_count, price_kind, last_seen DESC);
"""


def _epoch(ts: datetime) -> int:
    return int(ts.timestamp())


def _dt(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, tz=UTC)


def state_hash(offering: GpuOffering) -> str:
    """Exact identity of an offering's *mutable state*, immune to float jitter.

    Quantizing before hashing is what makes "did this change?" a decidable
    question: price to micro-dollars, score to milli-units. Anything finer is noise
    from JSON float round-tripping, not a real market move.
    """
    price_micros = round(offering.price_usd_hr * 1_000_000)
    score = offering.availability_score
    score_milli = "-" if score is None else str(round(score * 1_000))
    return f"{price_micros}:{offering.availability}:{score_milli}"


class SqliteTimeSeriesStore:
    """SQLite implementation of :class:`~spotfloor.storage.base.TimeSeriesStore`."""

    def __init__(
        self,
        path: str = "spotfloor.db",
        *,
        gap_ttl_s: int = 900,
        max_span_s: int = 86_400,
        freshness_ttl_s: int = 900,
    ) -> None:
        """
        ``gap_ttl_s``: if a series goes silent longer than this and returns at the
        same price, that is a new segment -- the offering disappeared and came back,
        and the gap is itself signal. Must exceed the poll interval comfortably.

        ``max_span_s``: roll a segment after this long even if nothing changed, so a
        segment cannot grow unbounded.

        ``freshness_ttl_s``: how long an observation stays "current" for
        :meth:`latest`. Must be several poll intervals, so that one failed provider
        fetch does not make every price appear to vanish.
        """
        # The poller writes from a scheduler thread while the web layer reads from
        # request threads, so the connection is shared. That is safe here because
        # CPython's sqlite3 reports threadsafety=3 (serialized) and WAL lets readers
        # run alongside the writer -- but only at the *statement* level.
        self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._gap_ttl_s = gap_ttl_s
        self._max_span_s = max_span_s
        self._freshness_ttl_s = freshness_ttl_s
        # `write` decides insert-vs-extend by reading the open segment and then
        # writing based on what it saw. Under `isolation_level=None` those are
        # separate autocommitted statements, so two concurrent writers could both
        # read the same segment and both insert. Serialize the whole decision.
        self._write_lock = threading.Lock()

    # --- write ---------------------------------------------------------------

    def write(self, offerings: Sequence[GpuOffering], *, now: datetime) -> WriteResult:
        """Extend open segments where nothing changed; open new ones where it did."""
        ts = _epoch(now)
        inserted = extended = skipped = 0

        # Two machines can collide on a series key within one batch (e.g. the same
        # host returned by both sort orders). Resolve deterministically to the
        # cheapest, so a tick is idempotent rather than order-dependent.
        batch: dict[str, GpuOffering] = {}
        for offering in offerings:
            incumbent = batch.get(offering.series_key)
            if incumbent is None or offering.price_usd_hr < incumbent.price_usd_hr:
                batch[offering.series_key] = offering
        skipped += len(offerings) - len(batch)

        with self._write_lock, self._conn:
            for offering in batch.values():
                current = self._conn.execute(
                    """
                    SELECT id, state_hash, first_seen, last_seen
                      FROM offering_observation
                     WHERE series_key = ?
                     ORDER BY last_seen DESC, id DESC
                     LIMIT 1
                    """,
                    (offering.series_key,),
                ).fetchone()

                # Never rewind time: a replayed or out-of-order observation must not
                # rewrite history.
                if current is not None and ts < current["last_seen"]:
                    skipped += 1
                    continue

                if current is not None and self._extends(current, offering, ts):
                    self._conn.execute(
                        "UPDATE offering_observation SET last_seen = ? WHERE id = ?",
                        (ts, current["id"]),
                    )
                    extended += 1
                else:
                    self._insert(offering, ts)
                    inserted += 1

        return WriteResult(inserted=inserted, extended=extended, skipped=skipped)

    def _extends(self, current: sqlite3.Row, offering: GpuOffering, ts: int) -> bool:
        """True when this observation continues the open segment rather than starting one."""
        return (
            current["state_hash"] == state_hash(offering)
            and ts - current["last_seen"] <= self._gap_ttl_s
            and ts - current["first_seen"] <= self._max_span_s
        )

    def _insert(self, offering: GpuOffering, ts: int) -> None:
        self._conn.execute(
            """
            INSERT INTO offering_observation (
                series_key, state_hash, provider, external_id, instance_type,
                gpu_model, gpu_count, region, price_kind, price_usd_hr,
                price_per_gpu_hr, availability, availability_score,
                first_seen, last_seen
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                offering.series_key,
                state_hash(offering),
                offering.provider,
                offering.external_id,
                offering.instance_type,
                offering.gpu_model,
                offering.gpu_count,
                offering.region,
                str(offering.price_kind),
                offering.price_usd_hr,
                offering.price_per_gpu_hr,
                str(offering.availability),
                offering.availability_score,
                ts,
                ts,
            ),
        )

    # --- read ----------------------------------------------------------------

    def latest(self, filt: OfferingFilter, *, now: datetime) -> list[OfferingRecord]:
        """Freshest segment per series.

        Stale series are simply absent -- they are *not observed*, which is a
        different claim from "unavailable". No tombstone rows, nothing fabricated;
        the evaluator turns absence into its own input.
        """
        where, params = self._where(filt)
        cutoff = _epoch(now) - self._freshness_ttl_s
        rows = self._conn.execute(
            f"""
            SELECT * FROM (
                SELECT *, ROW_NUMBER() OVER (
                           PARTITION BY series_key ORDER BY last_seen DESC, id DESC
                       ) AS rn
                  FROM offering_observation
                 WHERE last_seen >= ? {where}
            ) WHERE rn = 1
            ORDER BY price_per_gpu_hr ASC
            """,
            (cutoff, *params),
        ).fetchall()
        return [self._record(r) for r in rows]

    def history(self, filt: OfferingFilter, time_range: TimeRange) -> list[OfferingRecord]:
        """Segments overlapping the range, time-ordered.

        Because segments carry an interval, a range query is an overlap test rather
        than point sampling: a price that held for six hours is one row, and it is
        returned for any window touching those six hours.
        """
        where, params = self._where(filt)
        rows = self._conn.execute(
            f"""
            SELECT * FROM offering_observation
             WHERE first_seen <= ? AND last_seen >= ? {where}
             ORDER BY first_seen ASC, id ASC
            """,
            (_epoch(time_range.end), _epoch(time_range.start), *params),
        ).fetchall()
        return [self._record(r) for r in rows]

    def prune(self, before: datetime) -> int:
        """Delete segments whose last observation predates ``before``.

        The test is on ``last_seen``, not ``first_seen``: a segment that opened
        two weeks ago and is *still open* is current state, not history, and
        dropping it would erase the price the dashboard is showing right now.

        ``VACUUM`` follows because the point of pruning here is a smaller file to
        publish, and SQLite otherwise keeps the freed pages.
        """
        with self._write_lock:
            cursor = self._conn.execute(
                "DELETE FROM offering_observation WHERE last_seen < ?", (_epoch(before),)
            )
            removed = cursor.rowcount
            if removed:
                self._conn.execute("VACUUM")
        return removed

    @staticmethod
    def _where(filt: OfferingFilter) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("provider", filt.provider),
            ("gpu_model", filt.gpu_model),
            ("gpu_count", filt.gpu_count),
            ("region", filt.region),
            ("price_kind", None if filt.price_kind is None else str(filt.price_kind)),
            ("availability", None if filt.availability is None else str(filt.availability)),
        ):
            if value is not None:
                clauses.append(f"AND {column} = ?")
                params.append(value)
        return " ".join(clauses), params

    @staticmethod
    def _record(row: sqlite3.Row) -> OfferingRecord:
        return OfferingRecord(
            offering=GpuOffering(
                provider=row["provider"],
                external_id=row["external_id"],
                instance_type=row["instance_type"],
                gpu_model=row["gpu_model"],
                gpu_count=row["gpu_count"],
                region=row["region"],
                price_usd_hr=row["price_usd_hr"],
                price_kind=PriceKind(row["price_kind"]),
                availability=Availability(row["availability"]),
                availability_score=row["availability_score"],
                observed_at=_dt(row["last_seen"]),
            ),
            first_seen=_dt(row["first_seen"]),
            last_seen=_dt(row["last_seen"]),
        )

    def close(self) -> None:
        self._conn.close()
