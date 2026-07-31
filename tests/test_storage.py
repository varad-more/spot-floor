"""GATE 1: dedup and query correctness.

Assertions are on WriteResult counters rather than row counts: "the table grew by
one" is a much weaker claim than "this observation extended the open segment
instead of opening a new one".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from spotfloor.models import Availability, InstanceOffering, PriceKind
from spotfloor.storage.base import OfferingFilter, TimeRange
from spotfloor.storage.sqlite import SqliteTimeSeriesStore

T0 = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    # File-backed, not :memory: -- shared-cache semantics differ and would mask bugs.
    s = SqliteTimeSeriesStore(str(tmp_path / "test.db"), gap_ttl_s=900, freshness_ttl_s=900)
    yield s
    s.close()


def offering(
    price: float = 16.0,
    availability: Availability = Availability.AVAILABLE,
    *,
    machine: str = "m1",
    region: str = "Japan, JP",
    score: float | None = 0.99,
) -> InstanceOffering:
    return InstanceOffering(
        provider="vast",
        external_id=machine,
        instance_type="8xH100_SXM_80GB",
        gpu_model="H100_SXM_80GB",
        gpu_count=8,
        region=region,
        price_usd_hr=price,
        price_kind=PriceKind.ON_DEMAND,
        availability=availability,
        availability_score=score,
        observed_at=T0,
    )


def test_unchanged_observations_extend_one_segment(store) -> None:
    """The table must grow with change, not with time. This is the dedup gate."""
    for i in range(5):
        result = store.write([offering()], now=T0 + timedelta(minutes=5 * i))
        assert result.inserted == (1 if i == 0 else 0)
        assert result.extended == (0 if i == 0 else 1)

    records = store.history(OfferingFilter(), TimeRange(T0, T0 + timedelta(hours=1)))
    assert len(records) == 1, "identical observations opened extra rows"
    assert records[0].first_seen == T0
    assert records[0].last_seen == T0 + timedelta(minutes=20)


@pytest.mark.parametrize(
    ("change", "what"),
    [
        ({"price": 12.0}, "price change"),
        ({"availability": Availability.CONSTRAINED}, "availability change"),
    ],
)
def test_a_real_change_opens_a_new_segment(store, change: dict, what: str) -> None:
    store.write([offering()], now=T0)
    result = store.write([offering(**change)], now=T0 + timedelta(minutes=5))

    assert result.inserted == 1, f"{what} did not open a new segment"

    records = store.history(OfferingFilter(), TimeRange(T0, T0 + timedelta(hours=1)))
    assert len(records) == 2

    # The old segment must NOT be advanced: [first_seen, last_seen] stays a truthful
    # statement that this price held over exactly that window.
    assert records[0].last_seen == T0


def test_float_jitter_does_not_churn_the_table(store) -> None:
    """0.9950001 is not a market move. Comparing floats with = would open a segment
    on every poll and silently destroy dedup."""
    store.write([offering(score=0.995)], now=T0)
    result = store.write([offering(score=0.9950001)], now=T0 + timedelta(minutes=5))
    assert result.extended == 1 and result.inserted == 0


def test_reappearance_after_a_gap_is_a_new_segment(store) -> None:
    """An offer that vanished for hours and came back at the same price is a new
    episode, not one continuous one. The gap is signal."""
    store.write([offering()], now=T0)
    result = store.write([offering()], now=T0 + timedelta(hours=2))  # > gap_ttl
    assert result.inserted == 1


def test_out_of_order_writes_never_rewind_time(store) -> None:
    store.write([offering()], now=T0 + timedelta(minutes=30))
    result = store.write([offering(price=1.0)], now=T0)
    assert result.skipped == 1 and result.inserted == 0


def test_replaying_a_tick_is_idempotent(store) -> None:
    store.write([offering()], now=T0)
    result = store.write([offering()], now=T0)
    assert result.inserted == 0


def test_distinct_machines_are_distinct_series(store) -> None:
    """Two hosts with the same GPU in the same region must not collapse into one
    series that appears to thrash its price."""
    result = store.write([offering(machine="m1"), offering(machine="m2", price=12.0)], now=T0)
    assert result.inserted == 2
    assert len(store.latest(OfferingFilter(), now=T0)) == 2


def test_query_returns_a_time_ordered_series_for_gpu_and_region(store) -> None:
    """GATE 1: query() returns a correct time-ordered series for (gpu_model, region)."""
    prices = [16.0, 15.0, 14.0, 18.0]
    for i, price in enumerate(prices):
        store.write(
            [offering(price=price), offering(price=99.0, region="Texas, US", machine="m2")],
            now=T0 + timedelta(minutes=5 * i),
        )

    records = store.history(
        OfferingFilter(gpu_model="H100_SXM_80GB", region="Japan, JP"),
        TimeRange(T0, T0 + timedelta(hours=1)),
    )

    assert [r.offering.price_usd_hr for r in records] == prices
    assert [r.first_seen for r in records] == sorted(r.first_seen for r in records)
    assert all(r.offering.region == "Japan, JP" for r in records), "region filter leaked"


def test_latest_returns_one_row_per_series_and_drops_stale(store) -> None:
    store.write([offering()], now=T0)

    assert len(store.latest(OfferingFilter(), now=T0)) == 1
    # Past the freshness window the series is *not observed* -- not "unavailable".
    # Absence is never rendered as a fabricated value.
    assert store.latest(OfferingFilter(), now=T0 + timedelta(hours=1)) == []


def test_second_writer_in_the_same_second_does_not_lose_the_batch(store, tmp_path) -> None:
    """A price change landing on an instant a segment already starts at is skipped.

    `_write_lock` is a threading.Lock, so it serializes nothing across processes:
    `scripts/scan.py` writes to the same database a running `serve.py` polls. Two
    writers reaching the same series in the same second used to raise IntegrityError
    on the `ux_segment_start` unique index, which escaped `run_tick` (it does not
    guard the write) and took every *other* offering in the batch down with it.
    """
    other = SqliteTimeSeriesStore(str(tmp_path / "test.db"))  # a second process
    try:
        assert store.write([offering(price=1.0)], now=T0).inserted == 1

        healthy = [offering(price=5.0, machine=f"m{i}") for i in range(2, 5)]
        result = other.write([offering(price=2.0), *healthy], now=T0)

        assert result.skipped == 1, "the colliding series should be skipped, not raise"
        assert result.inserted == 3, "the rest of the batch must still be written"
    finally:
        other.close()


def test_backfilled_segment_start_does_not_break_the_next_poll(store) -> None:
    """`serve.py --backfill` seeds segments, then the poller ticks immediately."""
    from spotfloor.storage.base import OfferingRecord

    store.backfill([OfferingRecord(offering=offering(price=1.0), first_seen=T0, last_seen=T0)])

    result = store.write([offering(price=2.0)], now=T0)

    assert result.skipped == 1
    assert len(store.history(OfferingFilter(), TimeRange(T0, T0 + timedelta(hours=1)))) == 1
