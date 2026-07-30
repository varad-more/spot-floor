"""The read model: region roll-up, zone naming, volatility, and gaps.

The load-bearing tests here are the ones about *what a roll-up is allowed to hide*.
Collapsing a region's zones to one price is the natural thing for a table to do and
it produces a number you cannot act on, because capacity is bought per zone. So the
row must name the zone it took the price from, and it must state the spread it hid.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from spotfloor.models import Availability, InstanceOffering, PriceKind
from spotfloor.query import floor_series, region_table, volatility
from spotfloor.storage.base import OfferingRecord, TimeRange

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


def record(
    *,
    instance_type: str = "m5.large",
    region: str = "us-east-1",
    zone: str | None = "us-east-1a",
    price: float = 0.05,
    availability: Availability = Availability.UNKNOWN,
    gpu_count: int = 0,
    gpu_model: str | None = None,
    vcpus: int | None = 2,
    memory_gib: float | None = 8.0,
    provider: str = "aws",
    price_kind: PriceKind = PriceKind.SPOT,
    first_seen: datetime | None = None,
    last_seen: datetime | None = None,
) -> OfferingRecord:
    return OfferingRecord(
        offering=InstanceOffering(
            provider=provider,
            instance_type=instance_type,
            region=region,
            zone=zone,
            price_usd_hr=price,
            price_kind=price_kind,
            availability=availability,
            observed_at=last_seen or NOW,
            gpu_model=gpu_model,
            gpu_count=gpu_count,
            vcpus=vcpus,
            memory_gib=memory_gib,
        ),
        first_seen=first_seen or NOW - timedelta(minutes=30),
        last_seen=last_seen or NOW,
    )


# --- the region roll-up ------------------------------------------------------


def test_a_region_row_names_the_zone_its_price_came_from() -> None:
    """A bare regional minimum is unactionable: you launch into a zone."""
    rows = region_table(
        [
            record(zone="us-east-1a", price=0.080),
            record(zone="us-east-1d", price=0.051),
            record(zone="us-east-1f", price=0.062),
        ]
    )

    assert len(rows) == 1
    assert rows[0].cheapest_usd_hr == pytest.approx(0.051)
    assert rows[0].cheapest_zone == "us-east-1d"
    assert rows[0].zone_count == 3


def test_a_row_states_the_spread_it_hid() -> None:
    """The spread is the evidence that collapsing zones lost information."""
    rows = region_table(
        [record(zone="us-east-1a", price=0.10), record(zone="us-east-1d", price=0.05)]
    )

    row = rows[0]
    assert row.dearest_zone == "us-east-1a"
    assert row.dearest_usd_hr == pytest.approx(0.10)
    assert row.spread_pct == pytest.approx(100.0)


def test_zones_are_carried_so_the_rollup_is_inspectable() -> None:
    rows = region_table(
        [record(zone="us-east-1a", price=0.08), record(zone="us-east-1d", price=0.05)]
    )

    assert [z.zone for z in rows[0].zones] == ["us-east-1d", "us-east-1a"]
    assert [z.price_usd_hr for z in rows[0].zones] == [0.05, 0.08]


def test_one_zone_means_no_spread_rather_than_a_fake_range() -> None:
    rows = region_table([record(zone="us-east-1a", price=0.05)])
    assert rows[0].zone_count == 1
    assert rows[0].spread_pct == pytest.approx(0.0)


def test_regions_never_merge_into_one_row() -> None:
    """The entire point of the tool: us-east-1 and us-west-2 are separate markets."""
    rows = region_table(
        [
            record(region="us-east-1", zone="us-east-1a", price=0.08),
            record(region="us-west-2", zone="us-west-2b", price=0.05),
        ]
    )

    assert len(rows) == 2
    by_region = {r.region: r for r in rows}
    assert by_region["us-east-1"].cheapest_usd_hr == pytest.approx(0.08)
    assert by_region["us-west-2"].cheapest_usd_hr == pytest.approx(0.05)


def test_instance_types_never_merge_into_one_row() -> None:
    rows = region_table(
        [record(instance_type="m5.large"), record(instance_type="c5.large")]
    )
    assert {r.instance_type for r in rows} == {"m5.large", "c5.large"}


def test_rows_sort_by_type_then_price_so_regions_compare_adjacently() -> None:
    rows = region_table(
        [
            record(instance_type="m5.large", region="eu-west-1", zone="eu-west-1a", price=0.09),
            record(instance_type="c5.large", region="us-east-1", zone="us-east-1a", price=0.04),
            record(instance_type="m5.large", region="us-east-1", zone="us-east-1a", price=0.05),
        ]
    )

    assert [(r.instance_type, r.region) for r in rows] == [
        ("c5.large", "us-east-1"),
        ("m5.large", "us-east-1"),
        ("m5.large", "eu-west-1"),
    ]


# --- on-demand, and the savings it makes measurable --------------------------


def on_demand(*, price: float, **kwargs) -> OfferingRecord:
    """An on-demand record: one rate for the whole region, so no zone."""
    return record(price=price, zone=None, price_kind=PriceKind.ON_DEMAND, **kwargs)


def test_on_demand_is_a_column_on_the_spot_row_not_a_second_row() -> None:
    """The question is "how much does spot save me", and that needs them adjacent.

    Stored as its own series -- different product, different durability -- but a
    table twice as tall would answer the question worse, not better.
    """
    rows = region_table(
        [
            record(price=0.05, zone="us-east-1a"),
            on_demand(price=0.096),
        ]
    )

    assert len(rows) == 1
    assert rows[0].price_kind is PriceKind.SPOT
    assert rows[0].on_demand_usd_hr == pytest.approx(0.096)


def test_savings_are_measured_against_the_zone_you_would_actually_launch_into() -> None:
    """Not against a regional average, which is a price nobody can buy."""
    rows = region_table(
        [
            record(price=0.024, zone="us-east-1a"),
            record(price=0.072, zone="us-east-1d"),
            on_demand(price=0.096),
        ]
    )

    # 0.024 vs 0.096 is 75% off. Against the mean of the two zones it would read
    # 50%, which is a saving on a price that is not available in any zone.
    assert rows[0].savings_pct == pytest.approx(75.0)


def test_a_missing_on_demand_price_reads_as_unknown_not_as_no_saving() -> None:
    """0% would assert spot saves you nothing. None says we could not ask.

    This is the IAM path: a policy without pricing:GetProducts, or one of the
    regions AWS quotes in a currency other than USD.
    """
    row = region_table([record(price=0.05)])[0]

    assert row.on_demand_usd_hr is None
    assert row.savings_pct is None


def test_spot_above_the_list_price_is_reported_as_a_negative_saving() -> None:
    """A real market state during contention. Clamping it at zero would hide it."""
    rows = region_table([record(price=0.12), on_demand(price=0.096)])

    assert rows[0].savings_pct == pytest.approx(-25.0)


def test_an_on_demand_price_with_no_spot_counterpart_still_gets_a_row() -> None:
    """Folding it into a row that does not exist would drop the only price we have."""
    rows = region_table([on_demand(price=0.096, region="ap-south-1")])

    assert len(rows) == 1
    assert rows[0].price_kind is PriceKind.ON_DEMAND
    assert rows[0].cheapest_usd_hr == pytest.approx(0.096)
    # No saving to state: it is not cheaper than itself.
    assert rows[0].savings_pct is None


def test_on_demand_does_not_leak_across_regions() -> None:
    """One rate per region -- but the rates differ, and pairing them wrongly lies."""
    rows = region_table(
        [
            record(price=0.05, region="us-east-1", zone="us-east-1a"),
            record(price=0.06, region="eu-west-1", zone="eu-west-1a"),
            on_demand(price=0.096, region="us-east-1"),
        ]
    )

    by_region = {r.region: r for r in rows}
    assert by_region["us-east-1"].on_demand_usd_hr == pytest.approx(0.096)
    assert by_region["eu-west-1"].on_demand_usd_hr is None


# --- the honesty constraints -------------------------------------------------


def test_aws_availability_stays_unknown_and_is_never_counted_as_supply() -> None:
    row = region_table([record(availability=Availability.UNKNOWN)])[0]
    assert row.availability is Availability.UNKNOWN
    assert row.availability_known is False


def test_a_provider_that_reports_availability_still_wins_on_it_over_price() -> None:
    """Obtainability outranks price. AWS is all-unknown, but the ordering must survive.

    If this ever reduces to a plain price sort, a provider that *can* report
    availability would silently stop being preferred -- the product thesis quietly
    deleted by a refactor.
    """
    row = region_table(
        [
            record(zone="z-cheap", price=0.01, availability=Availability.UNAVAILABLE),
            record(zone="z-real", price=0.09, availability=Availability.AVAILABLE),
        ]
    )[0]

    # `availability` reflects the best *obtainable* offer...
    assert row.availability is Availability.AVAILABLE
    # ...while the price columns still report the observed range faithfully.
    assert row.cheapest_usd_hr == pytest.approx(0.01)


def test_a_provider_without_zones_labels_the_column_with_its_region() -> None:
    """Never a blank zone cell -- blank reads as missing data rather than N/A."""
    row = region_table(
        [record(provider="vast", region="Japan, JP", zone=None, price=2.0)]
    )[0]
    assert row.cheapest_zone == "Japan, JP"


# --- hardware spec passthrough -----------------------------------------------


def test_per_gpu_price_is_none_for_an_instance_with_no_gpu() -> None:
    """Dividing by "1 GPU" would invent a per-GPU price for a CPU box."""
    row = region_table([record(gpu_count=0, price=0.05)])[0]
    assert row.cheapest_per_gpu_hr is None


def test_per_gpu_and_per_vcpu_prices_are_derived_for_gpu_instances() -> None:
    row = region_table(
        [
            record(
                instance_type="p5.48xlarge",
                price=16.0,
                gpu_count=8,
                gpu_model="H100_SXM_80GB",
                vcpus=192,
            )
        ]
    )[0]

    assert row.cheapest_per_gpu_hr == pytest.approx(2.0)
    assert row.cheapest_per_vcpu_hr == pytest.approx(16.0 / 192)
    assert row.gpu_model == "H100_SXM_80GB"


def test_instance_family_is_derived_for_grouping() -> None:
    row = region_table([record(instance_type="m7i.xlarge")])[0]
    assert row.instance_family == "m7i"


# --- volatility --------------------------------------------------------------


def test_volatility_counts_transitions_not_segments() -> None:
    """N segments are N-1 changes: a segment *is* a price that held."""
    segments = [
        record(price=0.05, first_seen=NOW - timedelta(hours=3), last_seen=NOW - timedelta(hours=2)),
        record(price=0.07, first_seen=NOW - timedelta(hours=2), last_seen=NOW - timedelta(hours=1)),
        record(price=0.06, first_seen=NOW - timedelta(hours=1), last_seen=NOW),
    ]
    changes, cov = volatility(segments)

    assert changes == 2
    assert cov is not None and cov > 0


def test_volatility_of_an_unobserved_series_is_not_computed_rather_than_zero() -> None:
    """`None` means we did not measure. Zero would claim a perfectly stable price."""
    assert volatility([]) == (None, None)


def test_a_single_segment_has_zero_changes_and_zero_variation() -> None:
    changes, cov = volatility([record(price=0.05)])
    assert changes == 0
    assert cov == pytest.approx(0.0)


def test_volatility_is_scale_free_so_cheap_and_costly_rows_compare() -> None:
    """Coefficient of variation, not stdev: a $16 GPU box vs a $0.02 burstable."""
    cheap = [record(price=0.02), record(price=0.04)]
    costly = [record(price=16.0), record(price=32.0)]

    assert volatility(cheap)[1] == pytest.approx(volatility(costly)[1])


def test_region_table_wires_volatility_from_the_supplied_history() -> None:
    history = {
        ("m5.large", "us-east-1"): [
            record(price=0.05),
            record(price=0.06),
            record(price=0.05),
        ]
    }
    row = region_table([record()], history=history)[0]
    assert row.price_changes == 2


def test_omitting_history_leaves_volatility_uncomputed() -> None:
    row = region_table([record()])[0]
    assert row.price_changes is None
    assert row.coefficient_of_variation is None


# --- floor_series ------------------------------------------------------------


def test_unobserved_buckets_are_none_not_zero() -> None:
    """A gap is the absence of an observation, not a price of nothing."""
    window = TimeRange(NOW - timedelta(hours=4), NOW)
    records = [
        record(
            price=0.05,
            first_seen=NOW - timedelta(hours=4),
            last_seen=NOW - timedelta(hours=3),
        )
    ]

    series = floor_series(records, window, buckets=4)

    assert series[0].floor_usd_hr == pytest.approx(0.05)
    # Buckets entirely after the segment ended carry no observation. Bucket 1 is
    # excluded from this assertion on purpose -- see the boundary test below.
    assert [p.floor_usd_hr for p in series[2:]] == [None, None]


def test_a_segment_ending_on_a_bucket_boundary_counts_for_that_bucket() -> None:
    """The overlap test is `last_seen >= start`, i.e. touching counts.

    A segment whose ``last_seen`` is exactly a bucket's start instant was still a
    true price at that instant, so it registers there. Tightening this to a strict
    `>` would silently drop a real observation whenever a price change happened to
    land on a bucket edge -- which, with hourly buckets and hourly-ish AWS quotes,
    is not a rare alignment.
    """
    window = TimeRange(NOW - timedelta(hours=4), NOW)
    boundary = NOW - timedelta(hours=3)
    records = [record(price=0.05, first_seen=NOW - timedelta(hours=4), last_seen=boundary)]

    series = floor_series(records, window, buckets=4)

    assert series[1].at == boundary
    assert series[1].floor_usd_hr == pytest.approx(0.05)


def test_a_segment_spans_every_bucket_it_overlaps() -> None:
    """Segments carry intervals, so bucketing is an overlap test, not a sample."""
    window = TimeRange(NOW - timedelta(hours=4), NOW)
    records = [record(price=0.05, first_seen=NOW - timedelta(hours=4), last_seen=NOW)]

    series = floor_series(records, window, buckets=4)

    assert all(p.floor_usd_hr == pytest.approx(0.05) for p in series)


def test_the_series_reports_the_floor_when_zones_disagree() -> None:
    window = TimeRange(NOW - timedelta(hours=1), NOW)
    records = [
        record(zone="us-east-1a", price=0.09, first_seen=NOW - timedelta(hours=1)),
        record(zone="us-east-1d", price=0.04, first_seen=NOW - timedelta(hours=1)),
    ]

    series = floor_series(records, window, buckets=1)

    assert series[0].floor_usd_hr == pytest.approx(0.04)


def test_bucket_count_must_be_positive() -> None:
    window = TimeRange(NOW - timedelta(hours=1), NOW)
    with pytest.raises(ValueError):
        floor_series([], window, buckets=0)


def test_an_empty_window_is_rejected_rather_than_dividing_by_zero() -> None:
    with pytest.raises(ValueError):
        floor_series([], TimeRange(NOW, NOW), buckets=4)
