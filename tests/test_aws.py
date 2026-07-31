"""The AWS provider: region fan-out, history-as-segments, and the honesty gate.

Two tests here carry more weight than the rest.

:func:`test_app_creds_never_call_the_placement_score_api` is the honesty gate -- an
assertion, not a comment. The Spot Placement Score API returns a number computed
against the *calling account's* quota and history, so a score fetched with our
credentials describes our account, not the user's odds of getting capacity. Shipping
it as a market signal would be fabrication, so the code must not even ask.

:func:`test_a_failed_region_is_reported_not_silently_dropped` guards the other way a
region comparator lies: a region missing from the table is indistinguishable from a
region with no capacity, and this account has 17 opt-in regions that raise
``AuthFailure`` on every call.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from ec2_spot_prices.models import Availability, PriceKind
from ec2_spot_prices.providers.aws import AwsProvider, CredsOwner, enabled_regions

NOW = datetime.now(UTC)

INSTANCE_TYPES = {
    "InstanceTypes": [
        {
            "InstanceType": "p5.48xlarge",
            "VCpuInfo": {"DefaultVCpus": 192},
            "MemoryInfo": {"SizeInMiB": 2097152},
            "GpuInfo": {
                "Gpus": [
                    {
                        "Name": "H100",
                        "Manufacturer": "NVIDIA",
                        "Count": 8,
                        "MemoryInfo": {"SizeInMiB": 81920},
                    }
                ]
            },
        },
        {
            "InstanceType": "g6.12xlarge",
            "VCpuInfo": {"DefaultVCpus": 48},
            "MemoryInfo": {"SizeInMiB": 196608},
            "GpuInfo": {
                "Gpus": [
                    {
                        "Name": "L4",
                        "Manufacturer": "NVIDIA",
                        "Count": 4,
                        "MemoryInfo": {"SizeInMiB": 22888},
                    }
                ]
            },
        },
        {
            # No GpuInfo at all -- the common case, ~95% of the catalog.
            "InstanceType": "m5.large",
            "VCpuInfo": {"DefaultVCpus": 2},
            "MemoryInfo": {"SizeInMiB": 8192},
        },
    ]
}

WATCHLIST = ("p5.48xlarge", "g6.12xlarge", "m5.large")


def history(region: str) -> dict:
    """A quote stream for one region: several quotes per (type, AZ)."""
    return {
        "SpotPriceHistory": [
            {
                "InstanceType": "m5.large",
                "AvailabilityZone": f"{region}a",
                "SpotPrice": "0.0510",
                "Timestamp": NOW,
            },
            {
                "InstanceType": "m5.large",
                "AvailabilityZone": f"{region}a",
                "SpotPrice": "0.0880",
                "Timestamp": NOW - timedelta(hours=2),
            },
            {
                "InstanceType": "m5.large",
                "AvailabilityZone": f"{region}d",
                "SpotPrice": "0.0620",
                "Timestamp": NOW - timedelta(hours=1),
            },
            {
                "InstanceType": "p5.48xlarge",
                "AvailabilityZone": f"{region}b",
                "SpotPrice": "20.2547",
                "Timestamp": NOW,
            },
        ]
    }


def fake_client(region: str, *, fail: str | None = None, **overrides) -> MagicMock:
    ec2 = MagicMock(name=f"ec2:{region}")

    # Memoized per operation, so a test that reaches for `get_paginator(op)`
    # inspects the same object the code under test actually called.
    built: dict[str, MagicMock] = {}

    def paginator(operation: str) -> MagicMock:
        if operation in built:
            return built[operation]
        p = MagicMock()
        if operation == "describe_instance_types":
            p.paginate.return_value = [INSTANCE_TYPES]
        elif operation == "describe_spot_price_history":
            if fail:
                p.paginate.side_effect = _boto_error(fail)
            else:
                p.paginate.return_value = [history(region)]
        else:  # pragma: no cover - a new call would be a behaviour change
            raise AssertionError(f"unexpected paginator {operation!r}")
        built[operation] = p
        return p

    ec2.get_paginator.side_effect = paginator
    ec2.describe_regions.return_value = {
        "Regions": [{"RegionName": "us-east-1"}, {"RegionName": "eu-west-1"}]
    }
    for name, value in overrides.items():
        getattr(ec2, name).return_value = value
    return ec2


def _boto_error(code: str) -> Exception:
    error = Exception(f"{code}: simulated")
    error.response = {"Error": {"Code": code}}  # type: ignore[attr-defined]
    return error


# The on-demand list price per region, as the Price List Query API reports it: no
# zone dimension, and one rate for the whole region.
ON_DEMAND_USD_HR = {
    ("m5.large", "us-east-1"): 0.096,
    ("m5.large", "eu-west-1"): 0.107,
    ("p5.48xlarge", "us-east-1"): 98.32,
    ("g6.12xlarge", "us-east-1"): 4.6014,
}


def _price_list_entry(instance_type: str, region: str, usd: float | None, cny=None) -> str:
    """One `PriceList` blob, shaped exactly like the real API's JSON string."""
    per_unit = {}
    if usd is not None:
        per_unit["USD"] = f"{usd:.10f}"
    if cny is not None:
        per_unit["CNY"] = f"{cny:.10f}"
    return json.dumps(
        {
            "product": {"attributes": {"instanceType": instance_type, "regionCode": region}},
            "terms": {
                "OnDemand": {
                    "SKU.JRTCKXETXF": {
                        "priceDimensions": {
                            "SKU.JRTCKXETXF.6YS6EN2CT7": {
                                "unit": "Hrs",
                                "pricePerUnit": per_unit,
                            }
                        }
                    }
                }
            },
        }
    )


def fake_pricing_client(*, fail: str | None = None) -> MagicMock:
    """A Price List Query client that answers with every region in one page.

    Mirrors the real shape that makes this affordable: the request carries no
    ``regionCode`` filter, so one call covers all regions for one instance type.

    It answers the *unfiltered* sweep too -- no ``instanceType`` filter means every
    type, which is how the provider prices a full-catalogue watchlist in one cursor
    instead of 1,339 calls.
    """
    client = MagicMock(name="pricing")

    def paginate(*, ServiceCode: str, Filters: list[dict]) -> list[dict]:
        if fail:
            raise _boto_error(fail)
        wanted = next(
            (f["Value"] for f in Filters if f["Field"] == "instanceType"), None
        )
        entries = [
            _price_list_entry(t, r, usd)
            for (t, r), usd in ON_DEMAND_USD_HR.items()
            if wanted is None or t == wanted
        ]
        # A China region on every request, quoted in CNY. It must be dropped, not
        # converted -- we did not observe an exchange rate.
        for instance_type in {wanted} if wanted else {t for t, _ in ON_DEMAND_USD_HR}:
            entries.append(_price_list_entry(instance_type, "cn-north-1", None, cny=0.7))
        return [{"PriceList": entries}]

    paginator = MagicMock()
    paginator.paginate.side_effect = paginate
    client.get_paginator.return_value = paginator
    return client


def provider(
    *,
    regions=("us-east-1", "eu-west-1"),
    failing: set[str] | None = None,
    pricing_fails: str | None = None,
    **kwargs,
) -> AwsProvider:
    """An AwsProvider over fake clients, with the clients exposed for assertions.

    The pricing client is faked here rather than left to the default factory on
    purpose: without it every one of these tests would build a real boto3 client and
    the offline suite would quietly start calling AWS.
    """
    failing = failing or set()
    clients: dict[str, MagicMock] = {}

    def factory(region: str) -> MagicMock:
        if region not in clients:
            clients[region] = fake_client(
                region, fail="AuthFailure" if region in failing else None
            )
        return clients[region]

    pricing = fake_pricing_client(fail=pricing_fails)
    kwargs.setdefault("pricing_factory", lambda: pricing)
    # `setdefault`, not a fixed argument: `instance_types=None` is a real and
    # different mode ("every type EC2 offers") that tests have to be able to ask for.
    kwargs.setdefault("instance_types", WATCHLIST)

    p = AwsProvider(
        regions=regions,
        client_factory=factory,
        max_workers=2,
        **kwargs,
    )
    p._test_clients = clients  # type: ignore[attr-defined]
    p._test_pricing = pricing  # type: ignore[attr-defined]
    return p


# --- the honesty gate --------------------------------------------------------


def test_app_creds_never_call_the_placement_score_api() -> None:
    """THE HONESTY GATE. A score from our account is not a fact about the user."""
    p = provider(creds_owner=CredsOwner.APP)
    offerings = p.fetch()

    for client in p._test_clients.values():  # type: ignore[attr-defined]
        client.get_spot_placement_scores.assert_not_called()

    assert offerings
    for o in offerings:
        assert o.availability is Availability.UNKNOWN, "AWS fabricated an availability signal"
        assert o.availability_score is None


def test_user_creds_may_use_placement_scores() -> None:
    """With the user's own credentials the score is genuinely about them, so it counts.

    A score of 1/10 -- what p5.48xlarge really returned live -- means unavailable.
    """
    clients: dict[str, MagicMock] = {}

    def factory(region: str) -> MagicMock:
        clients.setdefault(
            region,
            fake_client(
                region,
                get_spot_placement_scores={
                    "SpotPlacementScores": [{"Region": region, "Score": 1}]
                },
            ),
        )
        return clients[region]

    p = AwsProvider(
        regions=("us-east-1",),
        instance_types=WATCHLIST,
        client_factory=factory,
        pricing_factory=fake_pricing_client,
        creds_owner=CredsOwner.USER,
    )
    offerings = p.fetch()

    clients["us-east-1"].get_spot_placement_scores.assert_called()
    spot = [o for o in offerings if o.price_kind is PriceKind.SPOT]
    assert spot
    assert all(o.availability is Availability.UNAVAILABLE for o in spot)
    assert all(o.availability_score == 0.1 for o in spot)

    # Spot Placement Score describes the *spot* capacity pool. On-demand is a
    # different pool, so the score says nothing about it and must not be attached --
    # even with the user's own credentials, where the score is otherwise legitimate.
    on_demand = [o for o in offerings if o.price_kind is PriceKind.ON_DEMAND]
    assert on_demand
    assert all(o.availability is Availability.UNKNOWN for o in on_demand)
    assert all(o.availability_score is None for o in on_demand)


def test_placement_scores_are_fetched_once_per_instance_type() -> None:
    """The score is per instance type, and this runs once per *offering*.

    `history_segments` builds ~172k offerings from a 30-day backfill, so an
    unmemoized USER-credential run would fire six figures of API calls to ask the
    same three questions. The failure mode is a bill, not a wrong answer, which is
    exactly the kind that no correctness test would catch.
    """
    clients: dict[str, MagicMock] = {}

    def factory(region: str) -> MagicMock:
        clients.setdefault(
            region,
            fake_client(
                region,
                get_spot_placement_scores={
                    "SpotPlacementScores": [{"Region": region, "Score": 1}]
                },
            ),
        )
        return clients[region]

    p = AwsProvider(
        regions=("us-east-1",),
        instance_types=WATCHLIST,
        client_factory=factory,
        pricing_factory=fake_pricing_client,
        creds_owner=CredsOwner.USER,
    )
    segments = p.history_segments(days=1)

    assert len(segments) > 3, "not enough segments to prove anything about caching"
    distinct_types = {s.offering.instance_type for s in segments}
    assert clients["us-east-1"].get_spot_placement_scores.call_count == len(distinct_types)


# --- failure reporting -------------------------------------------------------


def test_a_failed_region_is_reported_not_silently_dropped() -> None:
    """An absent region reads as "no capacity"; it must read as "we could not ask".

    This account has 17 opt-in regions that raise AuthFailure on every call, so this
    is the normal path, not an edge case.
    """
    p = provider(failing={"eu-west-1"})
    offerings = p.fetch()

    # The healthy region still returns data -- and the failed one contributes
    # nothing at all, not even the on-demand list price the global Price List API
    # would happily quote for it. The note below promises it is absent; an
    # on-demand-only row would make that promise false.
    assert {o.region for o in offerings} == {"us-east-1"}
    # ...and the broken one is named, with its reason, for the page to render.
    assert len(p.notes) == 1
    assert "eu-west-1" in p.notes[0]
    assert "AuthFailure" in p.notes[0]


def test_one_bad_region_does_not_fail_the_others() -> None:
    p = provider(regions=("us-east-1", "eu-west-1"), failing={"us-east-1"})
    assert p.fetch(), "a single region failure emptied the whole result"


def test_notes_are_cleared_between_runs() -> None:
    """A stale note would keep warning about a region that has since recovered."""
    p = provider(failing={"eu-west-1"})
    p.fetch()
    assert p.notes

    p._regions = ["us-east-1"]  # the failing region is no longer queried
    p.fetch()
    assert p.notes == []


# --- the catalog -------------------------------------------------------------


def test_catalog_is_derived_from_the_official_api() -> None:
    """p5 is 8x H100 SXM per DescribeInstanceTypes -- not a hand-maintained table."""
    catalog = provider().catalog()

    assert catalog["p5.48xlarge"].gpu_model == "H100_SXM_80GB"
    assert catalog["p5.48xlarge"].gpu_count == 8
    assert catalog["p5.48xlarge"].vcpus == 192
    # AWS reports a 24GB L4 as 22888 MiB; bucketing must recover the real SKU.
    assert catalog["g6.12xlarge"].gpu_model == "L4_24GB"


def test_an_instance_with_no_gpu_gets_zero_count_and_no_model() -> None:
    """95% of the catalog. `gpu_count == 0` is a fact, not missing data."""
    spec = provider().catalog()["m5.large"]

    assert spec.gpu_count == 0
    assert spec.gpu_model is None
    assert spec.vcpus == 2
    assert spec.memory_gib == pytest.approx(8.0)


def test_the_catalog_is_fetched_once_for_all_regions() -> None:
    """Specs are global -- m5.large is 2 vCPU everywhere -- so 17 fetches is waste."""
    p = provider()
    p.fetch()

    calls = [
        call
        for client in p._test_clients.values()  # type: ignore[attr-defined]
        for call in client.get_paginator.call_args_list
        if call.args and call.args[0] == "describe_instance_types"
    ]
    assert len(calls) == 1


# --- fetch: current price ----------------------------------------------------


def test_only_the_most_recent_quote_per_zone_survives() -> None:
    """describe_spot_price_history is a stream; the naive read is stale."""
    offerings = [o for o in provider(regions=("us-east-1",)).fetch()]

    by_zone = {
        o.zone: o
        for o in offerings
        if o.instance_type == "m5.large" and o.price_kind is PriceKind.SPOT
    }
    assert set(by_zone) == {"us-east-1a", "us-east-1d"}
    assert by_zone["us-east-1a"].price_usd_hr == pytest.approx(0.0510), "stale quote"


def test_region_and_zone_are_separate_fields() -> None:
    """A region comparator cannot key on a field that secretly holds an AZ."""
    offerings = provider(regions=("us-east-1",)).fetch()
    o = next(o for o in offerings if o.zone == "us-east-1a")

    assert o.region == "us-east-1"
    assert o.zone == "us-east-1a"


def test_every_region_is_queried() -> None:
    offerings = provider(regions=("us-east-1", "eu-west-1")).fetch()
    assert {o.region for o in offerings} == {"us-east-1", "eu-west-1"}


def test_the_whole_watchlist_goes_in_one_call_per_region() -> None:
    """O(regions), not O(regions x types): InstanceTypes takes a list."""
    p = provider(regions=("us-east-1",))
    p.fetch()

    paginate = p._test_clients["us-east-1"].get_paginator("describe_spot_price_history").paginate  # type: ignore[attr-defined]
    kwargs = paginate.call_args.kwargs
    assert set(kwargs["InstanceTypes"]) == set(WATCHLIST)


def test_gpu_spec_reaches_the_offering() -> None:
    offerings = provider(regions=("us-east-1",)).fetch()
    p5 = next(o for o in offerings if o.instance_type == "p5.48xlarge")

    assert p5.price_kind is PriceKind.SPOT
    assert p5.gpu_count == 8
    assert p5.gpu_model == "H100_SXM_80GB"
    assert p5.price_per_gpu_hr == pytest.approx(20.2547 / 8)


def test_a_cpu_instance_has_no_per_gpu_price() -> None:
    offerings = provider(regions=("us-east-1",)).fetch()
    m5 = next(o for o in offerings if o.instance_type == "m5.large")

    assert m5.gpu_count == 0
    assert m5.price_per_gpu_hr is None
    assert m5.price_per_vcpu_hr == pytest.approx(0.0510 / 2)


def test_unknown_instance_types_are_dropped_not_guessed() -> None:
    p = provider()
    p._catalog = {}  # simulate every instance type missing from the catalog
    assert p.fetch() == []


# --- on-demand list prices ---------------------------------------------------


def test_on_demand_offerings_carry_no_zone() -> None:
    """AWS charges one on-demand rate per region. A zone here would be invented.

    The per-AZ spread is the whole justification for this tool's roll-up, and it
    does not exist for this product -- so the field stays None rather than being
    filled with an arbitrary zone or with the region's name.
    """
    offerings = provider(regions=("us-east-1",)).fetch()
    on_demand = [o for o in offerings if o.price_kind is PriceKind.ON_DEMAND]

    assert on_demand
    assert all(o.zone is None for o in on_demand)
    assert {o.region for o in on_demand} == {"us-east-1"}

    m5 = next(o for o in on_demand if o.instance_type == "m5.large")
    assert m5.price_usd_hr == pytest.approx(0.096)


def test_a_region_quoted_in_another_currency_is_dropped_not_converted() -> None:
    """cn-north-1 bills in CNY. Turning that into USD invents an exchange rate.

    Same rule as an unobserved bucket staying None: absence is not a value, and a
    number we did not observe must not be manufactured to fill a column.
    """
    p = provider(regions=("us-east-1", "cn-north-1"))
    prices = p.on_demand_prices()

    assert ("m5.large", "us-east-1") in prices
    assert not any(region == "cn-north-1" for _type, region in prices)


def test_on_demand_costs_one_call_per_type_not_one_per_type_and_region() -> None:
    """Omitting the regionCode filter is what makes this affordable.

    One paginated call returns every region for an instance type, so a 40-type
    watchlist across 17 regions is 40 calls, not 680.
    """
    p = provider(regions=("us-east-1", "eu-west-1"))
    p.fetch()

    calls = p._test_pricing.get_paginator.return_value.paginate.call_args_list  # type: ignore[attr-defined]
    assert len(calls) == len(WATCHLIST)
    for call in calls:
        fields = {f["Field"] for f in call.kwargs["Filters"]}
        assert "regionCode" not in fields, "a per-region filter multiplies the call count"


def test_on_demand_prices_are_fetched_once_not_per_poll() -> None:
    """List prices move a few times a year; the poller ticks every five minutes."""
    p = provider(regions=("us-east-1",))
    p.fetch()
    p.fetch()

    calls = p._test_pricing.get_paginator.return_value.paginate.call_args_list  # type: ignore[attr-defined]
    assert len(calls) == len(WATCHLIST)


def test_losing_on_demand_prices_does_not_take_the_spot_table_down() -> None:
    """The IAM path: a policy with the three EC2 actions but no pricing:GetProducts.

    The savings column going blank is a degradation. An empty page is an outage,
    and one must not become the other.
    """
    p = provider(regions=("us-east-1",), pricing_fails="AccessDeniedException")
    offerings = p.fetch()

    spot = [o for o in offerings if o.price_kind is PriceKind.SPOT]
    assert spot, "a pricing failure emptied the spot table"
    assert not [o for o in offerings if o.price_kind is PriceKind.ON_DEMAND]

    # Blank must say why it is blank, or it reads as "spot saves you nothing".
    note = next(n for n in p.notes if "on-demand" in n.lower())
    assert "AccessDeniedException" in note
    assert "pricing:GetProducts" in note


# --- history_segments: the backfill path -------------------------------------


def test_history_becomes_closed_segments() -> None:
    """AWS emits a row when the price *changes*, so quotes bound intervals.

    m5.large in us-east-1a has two quotes: 0.0880 at -2h and 0.0510 at now. That is
    one closed segment at the old price and one still-open segment at the new one.
    """
    segments = provider(regions=("us-east-1",)).history_segments(days=1)

    m5a = sorted(
        (
            s
            for s in segments
            if s.offering.instance_type == "m5.large" and s.offering.zone == "us-east-1a"
        ),
        key=lambda s: s.first_seen,
    )

    assert len(m5a) == 2
    old, new = m5a
    assert old.offering.price_usd_hr == pytest.approx(0.0880)
    assert new.offering.price_usd_hr == pytest.approx(0.0510)
    # The old segment closes exactly where the new one opens -- no gap, no overlap.
    assert old.last_seen == new.first_seen


def test_segments_carry_the_interval_the_price_actually_held() -> None:
    segments = provider(regions=("us-east-1",)).history_segments(days=1)
    old = min(
        (
            s
            for s in segments
            if s.offering.instance_type == "m5.large" and s.offering.zone == "us-east-1a"
        ),
        key=lambda s: s.first_seen,
    )

    assert old.first_seen == NOW - timedelta(hours=2)
    assert old.last_seen == NOW


def test_equal_consecutive_prices_are_coalesced_into_one_segment() -> None:
    """AWS does re-emit an unchanged price; two touching segments at one price is one.

    Without this, a re-quoted-but-unchanged price would register as a price *move*
    and inflate the volatility column.
    """
    repeated = {
        "SpotPriceHistory": [
            {
                "InstanceType": "m5.large",
                "AvailabilityZone": "us-east-1a",
                "SpotPrice": "0.0510",
                "Timestamp": NOW - timedelta(hours=h),
            }
            for h in (3, 2, 1)
        ]
    }

    def factory(region: str) -> MagicMock:
        ec2 = MagicMock()

        def paginator(operation: str) -> MagicMock:
            p = MagicMock()
            p.paginate.return_value = [
                INSTANCE_TYPES if operation == "describe_instance_types" else repeated
            ]
            return p

        ec2.get_paginator.side_effect = paginator
        return ec2

    segments = AwsProvider(
        regions=("us-east-1",), instance_types=WATCHLIST, client_factory=factory
    ).history_segments(days=1)

    assert len(segments) == 1
    assert segments[0].first_seen == NOW - timedelta(hours=3)


def test_backfilled_history_is_spot_only() -> None:
    """AWS publishes no on-demand price history, so there is none to backfill.

    It also protects the volatility column: the read model groups history by
    (type, region), so an on-demand segment folded in here would be counted as a
    spot price change that never happened.
    """
    segments = provider(regions=("us-east-1",)).history_segments(days=1)

    assert segments
    assert all(s.offering.price_kind is PriceKind.SPOT for s in segments)


# --- region discovery --------------------------------------------------------


def test_enabled_regions_lists_only_what_the_account_can_call() -> None:
    """Opt-in regions would each raise AuthFailure; listing them is worse than scope."""
    assert enabled_regions(fake_client("us-east-1")) == ["eu-west-1", "us-east-1"]


# --- live gates --------------------------------------------------------------


@pytest.mark.live
def test_gate_1_live_aws_is_unknown_without_user_creds() -> None:
    """GATE 1: against the real AWS API, availability is UNKNOWN and priced sanely."""
    pytest.importorskip("boto3")

    offerings = AwsProvider(
        regions=("us-east-1",),
        instance_types=("p5.48xlarge", "m5.large"),
        creds_owner=CredsOwner.APP,
    ).fetch()

    assert offerings, "AWS returned no quotes"
    for o in offerings:
        # The honesty gate, live: neither price kind gets a fabricated availability.
        assert o.availability is Availability.UNKNOWN
        assert o.availability_score is None
        assert o.region == "us-east-1"
        assert 0 < o.price_usd_hr < 200

    spot = [o for o in offerings if o.price_kind is PriceKind.SPOT]
    on_demand = [o for o in offerings if o.price_kind is PriceKind.ON_DEMAND]

    assert spot, "AWS returned no spot quotes"
    for o in spot:
        assert o.zone and o.zone.startswith("us-east-1")

    # Validates the on-demand normalization against live values rather than against a
    # fixture we wrote ourselves -- which is the whole point of the live gates.
    assert on_demand, "the Price List API returned no on-demand rates"
    for o in on_demand:
        # No zone dimension exists for this product. A fixture can be made to agree
        # with a wrong belief about that; AWS cannot.
        assert o.zone is None, "an on-demand offering invented a zone"

    # One rate per (type, region), not one per zone.
    assert len({o.instance_type for o in on_demand}) == len(on_demand)


@pytest.mark.live
def test_live_history_is_a_change_log_not_a_sample_series() -> None:
    """The claim the backfill rests on: consecutive quotes tile the window."""
    pytest.importorskip("boto3")

    segments = AwsProvider(
        regions=("us-east-1",), instance_types=("m5.large",)
    ).history_segments(days=7)

    assert segments, "no history returned"

    by_series: dict[tuple[str, str | None], list] = {}
    for s in segments:
        by_series.setdefault((s.offering.instance_type, s.offering.zone), []).append(s)

    for key, group in by_series.items():
        group.sort(key=lambda s: s.first_seen)
        for earlier, later in zip(group, group[1:]):
            assert earlier.last_seen == later.first_seen, f"gap or overlap in {key}"


# --- the whole catalogue -----------------------------------------------------
# `instance_types=None` means every type EC2 offers. It exists because a curated
# watchlist is wrong the moment someone looks for a type nobody curated -- which is
# exactly how g5.2xlarge came to be missing from a page listing g5.xlarge and
# g5.12xlarge.


def test_all_types_asks_the_history_api_for_no_types_at_all() -> None:
    """Not a 1,339-entry `InstanceTypes` list -- no filter.

    `DescribeSpotPriceHistory` paginates the whole region regardless, so naming
    every type buys nothing and costs a request body per page. Measured unfiltered
    at 3.0s for us-east-1's 1,320 types, the same as the 40-type filter.
    """
    p = provider(instance_types=None, regions=("us-east-1",))
    p.fetch()

    paginate = p._test_clients["us-east-1"].get_paginator(
        "describe_spot_price_history"
    ).paginate
    kwargs = paginate.call_args.kwargs
    assert "InstanceTypes" not in kwargs, "an all-types scan named types anyway"
    assert kwargs["ProductDescriptions"] == ["Linux/UNIX"]


def test_all_types_reads_the_catalog_unfiltered() -> None:
    """An empty `Values` filter matches nothing; it does not mean everything.

    Filtering by `instance-type` with no values would hand back an empty catalog,
    and `fetch` drops every quote whose type is not in the catalog -- so the table
    would come back silently empty rather than obviously broken.
    """
    p = provider(instance_types=None, regions=("us-east-1",))
    catalog = p.catalog()

    assert set(catalog) == {"p5.48xlarge", "g6.12xlarge", "m5.large"}
    paginate = p._test_clients["us-east-1"].get_paginator(
        "describe_instance_types"
    ).paginate
    assert "Filters" not in paginate.call_args.kwargs


def test_all_types_prices_on_demand_in_one_sweep_not_one_call_per_type() -> None:
    """Past the threshold the `instanceType` filter is dropped as well.

    Measured: 254 pages and 53.7s for all 24,383 (type, region) pairs, against
    1,339 separate calls for the same answer.
    """
    p = provider(instance_types=None, regions=("us-east-1",))
    prices = p.on_demand_prices()

    assert p._test_pricing.get_paginator.return_value.paginate.call_count == 1
    filters = p._test_pricing.get_paginator.return_value.paginate.call_args.kwargs["Filters"]
    assert not [f for f in filters if f["Field"] == "instanceType"]
    # The four SKU-pinning filters stay: without them one type returns a dozen SKUs
    # and "the on-demand price" becomes whichever sorted first.
    assert {f["Field"] for f in filters} == {
        "operatingSystem", "tenancy", "preInstalledSw", "capacitystatus"
    }
    assert prices[("m5.large", "us-east-1")] == pytest.approx(0.096)
    assert prices[("p5.48xlarge", "us-east-1")] == pytest.approx(98.32)


def test_a_small_watchlist_still_prices_per_type() -> None:
    """The sweep is a fixed ~54s; three types individually are three fast calls.

    The threshold picks whichever shape is cheaper rather than committing the
    provider to one of them.
    """
    p = provider()
    p.on_demand_prices()

    calls = p._test_pricing.get_paginator.return_value.paginate.call_args_list
    assert len(calls) == len(WATCHLIST)
    assert {
        next(f["Value"] for f in c.kwargs["Filters"] if f["Field"] == "instanceType")
        for c in calls
    } == set(WATCHLIST)


def test_the_bulk_sweep_still_drops_non_usd_regions() -> None:
    """CNY-quoted regions are skipped, never converted, on both paths."""
    p = provider(instance_types=None, regions=("us-east-1",))
    assert not [r for (_t, r) in p.on_demand_prices() if r.startswith("cn-")]


def test_a_failed_bulk_sweep_costs_the_column_not_the_table() -> None:
    p = provider(instance_types=None, regions=("us-east-1",), pricing_fails="AccessDenied")
    offerings = p.fetch()

    assert p.on_demand_prices() == {}
    assert [o for o in offerings if o.price_kind is PriceKind.SPOT], "spot rows vanished"
    assert any("AccessDenied" in note for note in p.notes)


# --- regions the account cannot reach ----------------------------------------


def test_regions_the_account_has_not_opted_into_are_named_not_hidden() -> None:
    """17 of AWS's 34 commercial regions are opt-in. Showing 17 without saying so
    would let a reader read "no capacity" where the truth is "never asked"."""
    client = fake_client("us-east-1")
    client.describe_regions.side_effect = lambda **kw: (
        {"Regions": [{"RegionName": "us-east-1"}, {"RegionName": "eu-west-1"}]}
        if not kw.get("AllRegions")
        else {"Regions": [
            {"RegionName": "us-east-1"}, {"RegionName": "eu-west-1"},
            {"RegionName": "af-south-1"}, {"RegionName": "ap-east-1"},
            # Separate partitions these credentials cannot reach at all, and not
            # something an account owner opts into from here.
            {"RegionName": "cn-north-1"}, {"RegionName": "us-gov-west-1"},
        ]}
    )
    p = AwsProvider(
        regions=None,
        instance_types=WATCHLIST,
        client_factory=lambda region: client,
        pricing_factory=fake_pricing_client,
    )

    assert p.regions() == ["eu-west-1", "us-east-1"]
    note = next(n for n in p.notes if "not enabled" in n)
    assert "af-south-1" in note and "ap-east-1" in note
    assert "cn-north-1" not in note and "us-gov-west-1" not in note
    assert "no capacity" not in note.lower() or "not because" in note


def test_an_explicit_region_list_gets_no_opt_in_caveat() -> None:
    """You asked for two regions; the other 32 are not a caveat about your request."""
    p = provider(regions=("us-east-1",))
    p.fetch()
    assert not [n for n in p.notes if "not enabled" in n]
