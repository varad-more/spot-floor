"""The backfill write path: segments that carry their own timestamps.

``write`` and ``backfill`` answer different questions. ``write`` is told "this is
the state *now*" and infers where segment boundaries fall. ``backfill`` is handed
intervals that are already known -- the shape of ``DescribeSpotPriceHistory``,
which emits a row when a price *changes*, making its history a change-log rather
than a sample series.

The property that matters operationally is **idempotence**. The database is a
rebuildable cache (AWS re-serves ~89 days on demand), so a lost CI cache means
backfilling the same window again, and that must not duplicate rows or inflate the
price-change count the volatility column reads.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from spotfloor.models import Availability, InstanceOffering, PriceKind
from spotfloor.storage.base import OfferingFilter, OfferingRecord, TimeRange
from spotfloor.storage.sqlite import SCHEMA_VERSION, SqliteTimeSeriesStore

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


def offering(price: float, *, zone: str = "us-east-1a") -> InstanceOffering:
    return InstanceOffering(
        provider="aws",
        instance_type="m5.large",
        region="us-east-1",
        zone=zone,
        price_usd_hr=price,
        price_kind=PriceKind.SPOT,
        availability=Availability.UNKNOWN,
        observed_at=NOW,
        vcpus=2,
        memory_gib=8.0,
    )


def segment(price: float, *, hours_ago: int, until_hours_ago: int, zone: str = "us-east-1a"):
    return OfferingRecord(
        offering=offering(price, zone=zone),
        first_seen=NOW - timedelta(hours=hours_ago),
        last_seen=NOW - timedelta(hours=until_hours_ago),
    )


@pytest.fixture
def store(tmp_path):
    s = SqliteTimeSeriesStore(str(tmp_path / "backfill.db"))
    yield s
    s.close()


def test_backfilled_segments_keep_their_own_intervals(store) -> None:
    """The wall clock is not when a historical price was true."""
    store.backfill(
        [
            segment(0.088, hours_ago=6, until_hours_ago=4),
            segment(0.051, hours_ago=4, until_hours_ago=0),
        ]
    )

    records = store.history(OfferingFilter(), TimeRange(NOW - timedelta(days=1), NOW))
    records.sort(key=lambda r: r.first_seen)

    assert [r.offering.price_usd_hr for r in records] == [0.088, 0.051]
    assert records[0].first_seen == NOW - timedelta(hours=6)
    assert records[0].last_seen == NOW - timedelta(hours=4)
    # The segments tile the window: one closes exactly where the next opens.
    assert records[0].last_seen == records[1].first_seen


def test_backfill_is_idempotent(store) -> None:
    """A lost cache means backfilling the same window again. It must be a no-op."""
    segments = [
        segment(0.088, hours_ago=6, until_hours_ago=4),
        segment(0.051, hours_ago=4, until_hours_ago=0),
    ]

    first = store.backfill(segments)
    second = store.backfill(segments)

    assert first.inserted == 2
    assert second.inserted == 0
    assert second.skipped == 2

    records = store.history(OfferingFilter(), TimeRange(NOW - timedelta(days=1), NOW))
    assert len(records) == 2, "re-running the backfill duplicated rows"


def test_repeated_backfill_does_not_inflate_the_price_change_count(store) -> None:
    """The volatility column reads segment count, so a duplicate is a phantom move."""
    from spotfloor.query import volatility

    segments = [
        segment(0.088, hours_ago=6, until_hours_ago=4),
        segment(0.051, hours_ago=4, until_hours_ago=0),
    ]
    store.backfill(segments)
    store.backfill(segments)

    records = store.history(OfferingFilter(), TimeRange(NOW - timedelta(days=1), NOW))
    changes, _ = volatility(records)
    assert changes == 1


def test_different_zones_are_separate_series(store) -> None:
    """Same instant, same type, different zone: two rows, not a unique-index clash."""
    store.backfill(
        [
            segment(0.088, hours_ago=4, until_hours_ago=0, zone="us-east-1a"),
            segment(0.051, hours_ago=4, until_hours_ago=0, zone="us-east-1d"),
        ]
    )

    records = store.history(OfferingFilter(), TimeRange(NOW - timedelta(days=1), NOW))
    assert {r.offering.zone for r in records} == {"us-east-1a", "us-east-1d"}


def test_a_reversed_interval_is_rejected_rather_than_stored(store) -> None:
    """last_seen before first_seen is not a segment; storing it corrupts every range query."""
    result = store.backfill(
        [
            OfferingRecord(
                offering=offering(0.05),
                first_seen=NOW,
                last_seen=NOW - timedelta(hours=1),
            )
        ]
    )

    assert result.inserted == 0
    assert result.skipped == 1


def test_a_stale_backfilled_segment_is_not_current_state(store) -> None:
    """History is not the present. `latest` must not resurrect a 6-hour-old price."""
    store.backfill([segment(0.088, hours_ago=8, until_hours_ago=6)])

    assert store.latest(OfferingFilter(), now=NOW) == []
    # ...but it is still there as history.
    assert store.history(OfferingFilter(), TimeRange(NOW - timedelta(days=1), NOW))


def test_an_open_backfilled_segment_is_current_state(store) -> None:
    """The newest quote stays open until `now`, so it *is* the current price."""
    store.backfill([segment(0.051, hours_ago=4, until_hours_ago=0)])

    current = store.latest(OfferingFilter(), now=NOW)
    assert [r.offering.price_usd_hr for r in current] == [0.051]


def test_backfill_and_poll_coexist_on_one_series(store) -> None:
    """The two write paths share a table; a poll after a backfill must extend, not clash."""
    store.backfill([segment(0.051, hours_ago=4, until_hours_ago=0)])
    result = store.write([offering(0.051)], now=NOW + timedelta(minutes=5))

    # Same price, still inside the gap TTL -> the open segment simply grows.
    assert result.extended == 1
    assert result.inserted == 0


# --- the schema guard --------------------------------------------------------


def test_an_older_schema_is_rebuilt_rather_than_half_migrated(tmp_path) -> None:
    """The store is a cache, so a layout change drops and rebuilds.

    A half-migrated cache is worse than an empty one: it serves rows the new code
    misreads. Every row here costs one API call to reproduce.
    """
    path = str(tmp_path / "old.db")

    store = SqliteTimeSeriesStore(path)
    store.backfill([segment(0.051, hours_ago=4, until_hours_ago=0)])
    store._conn.execute("PRAGMA user_version = 1")  # pretend an older layout wrote it
    store.close()

    reopened = SqliteTimeSeriesStore(path)
    try:
        assert reopened._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert reopened.history(OfferingFilter(), TimeRange(NOW - timedelta(days=1), NOW)) == []
        # And it is usable, not just empty.
        assert reopened.backfill([segment(0.051, hours_ago=4, until_hours_ago=0)]).inserted == 1
    finally:
        reopened.close()


def test_a_current_schema_is_left_alone(tmp_path) -> None:
    """The rebuild must not fire on every open, or history never accumulates."""
    path = str(tmp_path / "current.db")

    store = SqliteTimeSeriesStore(path)
    store.backfill([segment(0.051, hours_ago=4, until_hours_ago=0)])
    store.close()

    reopened = SqliteTimeSeriesStore(path)
    try:
        records = reopened.history(OfferingFilter(), TimeRange(NOW - timedelta(days=1), NOW))
        assert len(records) == 1, "reopening the store wiped valid data"
    finally:
        reopened.close()
