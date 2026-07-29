"""Read-model aggregation: the place where the honesty constraints get quietly lost.

Every assertion here is about a specific way a normal dashboard would lie:
merging providers that share a GPU model, treating `unknown` as a weak yes,
letting a cheap-but-ungettable node set the headline, or filling a gap in a chart.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from spotfloor.models import Availability, GpuOffering, PriceKind
from spotfloor.query import floor_series, market_table
from spotfloor.storage.base import OfferingRecord, TimeRange

T0 = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


def record(
    *,
    provider: str = "vast",
    gpu_model: str = "H100_SXM_80GB",
    gpu_count: int = 8,
    region: str = "Japan, JP",
    price: float = 16.0,
    kind: PriceKind = PriceKind.ON_DEMAND,
    availability: Availability = Availability.AVAILABLE,
    external_id: str | None = "m1",
    first: datetime = T0,
    last: datetime | None = None,
) -> OfferingRecord:
    return OfferingRecord(
        offering=GpuOffering(
            provider=provider,
            external_id=external_id,
            instance_type=f"{gpu_count}x{gpu_model}",
            gpu_model=gpu_model,
            gpu_count=gpu_count,
            region=region,
            price_usd_hr=price,
            price_kind=kind,
            availability=availability,
            observed_at=last or first,
        ),
        first_seen=first,
        last_seen=last or first,
    )


# --- market_table ------------------------------------------------------------


def test_rows_never_merge_across_providers() -> None:
    """Same silicon, two providers: two rows, because regions are not comparable."""
    rows = market_table(
        [
            record(provider="vast", region="Japan, JP", price=16.0),
            record(provider="aws", region="us-east-1a", price=24.0,
                   kind=PriceKind.ON_DEMAND, availability=Availability.UNKNOWN,
                   external_id=None),
        ]
    )
    assert len(rows) == 2
    assert {r.provider for r in rows} == {"vast", "aws"}
    # ...and each keeps its own provider-native region.
    assert {r.cheapest_region for r in rows} == {"Japan, JP", "us-east-1a"}


def test_on_demand_and_spot_are_separate_rows() -> None:
    """Different durability is a different product, not two states of one."""
    rows = market_table(
        [
            record(kind=PriceKind.ON_DEMAND, price=16.0),
            record(kind=PriceKind.SPOT, price=6.0,
                   availability=Availability.CONSTRAINED),
        ]
    )
    assert {r.price_kind for r in rows} == {PriceKind.ON_DEMAND, PriceKind.SPOT}


def test_obtainability_outranks_price_when_picking_the_headline_node() -> None:
    """The product thesis, at the row level.

    A cheaper node that cannot be held does not get to be the node we show. It
    still sets `cheapest_per_gpu_hr` -- the price is real -- but the node we
    describe (region, size, availability) is the one you could actually take.
    """
    rows = market_table(
        [
            record(external_id="cheap", price=8.0, region="Nowhere, XX",
                   availability=Availability.UNAVAILABLE),
            record(external_id="real", price=16.0, region="Japan, JP",
                   availability=Availability.AVAILABLE),
        ]
    )
    (row,) = rows
    assert row.cheapest_region == "Japan, JP"
    assert row.cheapest_availability is Availability.AVAILABLE
    assert row.cheapest_per_gpu_hr == pytest.approx(1.0)  # 8.0 / 8 GPUs, still reported
    assert row.cheapest_obtainable_per_gpu_hr == pytest.approx(2.0)


def test_unknown_is_never_counted_as_supply() -> None:
    """The AWS case: real prices, and no claim at all about obtainability."""
    rows = market_table(
        [
            record(provider="aws", region="us-east-1a", price=24.0,
                   availability=Availability.UNKNOWN, external_id=None),
            record(provider="aws", region="us-west-2b", price=20.0,
                   availability=Availability.UNKNOWN, external_id=None),
        ]
    )
    (row,) = rows
    assert row.node_count == 2
    assert row.obtainable_nodes == 0
    # The distinction the UI hangs on: "we cannot know" is not "there are none".
    assert row.availability_known is False
    assert row.cheapest_obtainable_per_gpu_hr is None
    assert row.cheapest_per_gpu_hr == pytest.approx(2.5)


def test_constrained_counts_as_obtainable_but_available_wins_the_headline() -> None:
    rows = market_table(
        [
            record(external_id="a", price=16.0, availability=Availability.AVAILABLE),
            record(external_id="b", price=12.0, availability=Availability.CONSTRAINED),
            record(external_id="c", price=99.0, availability=Availability.UNAVAILABLE),
        ]
    )
    (row,) = rows
    assert row.obtainable_nodes == 2
    assert row.node_count == 3
    assert row.cheapest_availability is Availability.AVAILABLE
    # The constrained box is cheaper and genuinely gettable, so it sets the
    # obtainable floor even though the headline node is the available one.
    assert row.cheapest_obtainable_per_gpu_hr == pytest.approx(1.5)


def test_rows_are_ordered_by_model_then_price() -> None:
    rows = market_table(
        [
            record(gpu_model="H100_SXM_80GB", provider="aws", price=32.0,
                   availability=Availability.UNKNOWN, external_id=None),
            record(gpu_model="A100_SXM_80GB", provider="vast", price=8.0),
            record(gpu_model="H100_SXM_80GB", provider="vast", price=16.0),
        ]
    )
    assert [(r.gpu_model, r.provider) for r in rows] == [
        ("A100_SXM_80GB", "vast"),
        ("H100_SXM_80GB", "vast"),
        ("H100_SXM_80GB", "aws"),
    ]


# --- floor_series ------------------------------------------------------------


def test_a_bucket_with_no_observation_is_none_not_zero() -> None:
    """Absence must survive all the way to the chart.

    Zero would render as a free GPU; carrying the previous price forward would
    invent an observation. Only None is true.
    """
    window = TimeRange(T0, T0 + timedelta(hours=4))
    records = [record(first=T0, last=T0 + timedelta(hours=1), price=16.0)]

    series = floor_series(records, window, buckets=4)

    assert [p.floor_per_gpu_hr for p in series] == [2.0, 2.0, None, None]


def test_a_segment_spans_every_bucket_it_overlaps() -> None:
    """Segments carry an interval, so a range query is overlap, not point sampling."""
    window = TimeRange(T0, T0 + timedelta(hours=4))
    records = [record(first=T0, last=T0 + timedelta(hours=4), price=16.0)]

    series = floor_series(records, window, buckets=4)

    assert all(p.floor_per_gpu_hr == pytest.approx(2.0) for p in series)


def test_the_floor_is_the_minimum_across_overlapping_segments() -> None:
    window = TimeRange(T0, T0 + timedelta(hours=2))
    records = [
        record(external_id="a", first=T0, last=T0 + timedelta(hours=2), price=16.0),
        record(external_id="b", first=T0 + timedelta(hours=1),
               last=T0 + timedelta(hours=2), price=8.0),
    ]

    series = floor_series(records, window, buckets=2)

    assert [p.floor_per_gpu_hr for p in series] == [2.0, 1.0]


def test_buckets_are_evenly_spaced_and_start_at_the_window_start() -> None:
    window = TimeRange(T0, T0 + timedelta(hours=6))
    series = floor_series([], window, buckets=6)

    assert len(series) == 6
    assert series[0].at == T0
    assert series[-1].at == T0 + timedelta(hours=5)


def test_a_degenerate_window_is_rejected_rather_than_silently_empty() -> None:
    with pytest.raises(ValueError):
        floor_series([], TimeRange(T0, T0), buckets=4)
    with pytest.raises(ValueError):
        floor_series([], TimeRange(T0, T0 + timedelta(hours=1)), buckets=0)
