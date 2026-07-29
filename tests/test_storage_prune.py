"""Retention.

The subtle case is a segment that opened long ago and is *still open*: that is
current state, not history, and pruning by ``first_seen`` would delete the price
the dashboard is displaying right now.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from spotfloor.models import Availability, InstanceOffering, PriceKind
from spotfloor.storage.base import OfferingFilter, TimeRange
from spotfloor.storage.sqlite import SqliteTimeSeriesStore

T0 = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def offering(price: float, *, external_id: str = "m1") -> InstanceOffering:
    return InstanceOffering(
        provider="vast",
        external_id=external_id,
        instance_type="8xH100_SXM_80GB",
        gpu_model="H100_SXM_80GB",
        gpu_count=8,
        region="Japan, JP",
        price_usd_hr=price,
        price_kind=PriceKind.ON_DEMAND,
        availability=Availability.AVAILABLE,
        observed_at=T0,
    )


def test_old_segments_are_dropped(tmp_path) -> None:
    store = SqliteTimeSeriesStore(str(tmp_path / "p.db"))
    try:
        store.write([offering(16.0)], now=T0)
        store.write([offering(12.0)], now=T0 + timedelta(days=5))

        removed = store.prune(T0 + timedelta(days=2))

        assert removed == 1
        survivors = store.history(
            OfferingFilter(), TimeRange(T0 - timedelta(days=1), T0 + timedelta(days=10))
        )
        assert [r.offering.price_usd_hr for r in survivors] == [12.0]
    finally:
        store.close()


def test_an_open_segment_is_never_pruned_just_because_it_started_long_ago(tmp_path) -> None:
    """It is current state. Deleting it would blank the live row."""
    store = SqliteTimeSeriesStore(str(tmp_path / "p.db"), gap_ttl_s=10**7, max_span_s=10**7)
    try:
        store.write([offering(16.0)], now=T0)
        # Same price a week later: this EXTENDS the original segment, so
        # first_seen stays at T0 while last_seen moves forward.
        store.write([offering(16.0)], now=T0 + timedelta(days=7))

        removed = store.prune(T0 + timedelta(days=3))

        assert removed == 0
        current = store.latest(OfferingFilter(), now=T0 + timedelta(days=7))
        assert [r.offering.price_usd_hr for r in current] == [16.0]
    finally:
        store.close()


def test_pruning_an_empty_range_removes_nothing(tmp_path) -> None:
    store = SqliteTimeSeriesStore(str(tmp_path / "p.db"))
    try:
        store.write([offering(16.0)], now=T0)
        assert store.prune(T0 - timedelta(days=1)) == 0
    finally:
        store.close()
