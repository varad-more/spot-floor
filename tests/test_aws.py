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

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from spotfloor.models import Availability, PriceKind
from spotfloor.providers.aws import AwsProvider, CredsOwner, enabled_regions

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


def provider(
    *, regions=("us-east-1", "eu-west-1"), failing: set[str] | None = None, **kwargs
) -> AwsProvider:
    """An AwsProvider over fake clients, with the clients exposed for assertions."""
    failing = failing or set()
    clients: dict[str, MagicMock] = {}

    def factory(region: str) -> MagicMock:
        if region not in clients:
            clients[region] = fake_client(
                region, fail="AuthFailure" if region in failing else None
            )
        return clients[region]

    p = AwsProvider(
        regions=regions,
        instance_types=WATCHLIST,
        client_factory=factory,
        max_workers=2,
        **kwargs,
    )
    p._test_clients = clients  # type: ignore[attr-defined]
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
        creds_owner=CredsOwner.USER,
    )
    offerings = p.fetch()

    clients["us-east-1"].get_spot_placement_scores.assert_called()
    assert offerings
    assert all(o.availability is Availability.UNAVAILABLE for o in offerings)
    assert all(o.availability_score == 0.1 for o in offerings)


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

    # The healthy region still returns data...
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

    by_zone = {o.zone: o for o in offerings if o.instance_type == "m5.large"}
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

    assert offerings, "AWS returned no spot quotes"
    for o in offerings:
        assert o.availability is Availability.UNKNOWN
        assert o.availability_score is None
        assert o.price_kind is PriceKind.SPOT
        assert o.region == "us-east-1"
        assert o.zone and o.zone.startswith("us-east-1")
        assert 0 < o.price_usd_hr < 100


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
