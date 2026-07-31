"""Vast provider: the availability rule, the normalization guards, and GATE 0.

The rule and guard tests are pure logic over synthetic field dicts on purpose --
they pin the *decision*, which must not drift. The live test is the actual gate: it
pulls real H100 listings at test time and asserts normalization holds against
values nobody hand-invented.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from statistics import median

import httpx
import pytest

from ec2_spot_prices.gpu import is_canonical
from ec2_spot_prices.models import Availability, PriceKind, obtainability_rank
from ec2_spot_prices.providers.vast import VastProvider, derive_availability

NOW = datetime.now(UTC)

HEALTHY = {
    "rentable": True,
    "rented": False,
    "verification": "verified",
    "reliability2": 0.99,
}

# Shaped like a real Vast row; values from live machine 40024081 (8x H100 SXM).
MACHINE = HEALTHY | {
    "machine_id": 54813,
    "num_gpus": 8,
    "gpu_name": "H100 SXM",
    "gpu_ram": 81920,
    "dph_total": 16.0,
    "min_bid": 3.57,
    "is_bid": False,
    "geolocation": "Japan, JP",
}


# --- the availability rule ---------------------------------------------------


def test_healthy_on_demand_host_is_available() -> None:
    assert derive_availability(HEALTHY, PriceKind.ON_DEMAND) is Availability.AVAILABLE


def test_spot_is_never_better_than_constrained() -> None:
    """An interruptible bid is preemptible by design, however healthy the host."""
    assert derive_availability(HEALTHY, PriceKind.SPOT) is Availability.CONSTRAINED


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"rentable": False}, "host has no free capacity"),
        ({"rented": True}, "host is currently occupied"),
    ],
)
def test_unobtainable_hosts_are_unavailable(override: dict, reason: str) -> None:
    assert derive_availability(HEALTHY | override, PriceKind.ON_DEMAND) is (
        Availability.UNAVAILABLE
    ), reason


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"verification": "deverified"}, "Vast has flagged this host"),
        ({"reliability2": 0.80}, "host churns below the reliability floor"),
    ],
)
def test_obtainable_but_risky_hosts_are_constrained(override: dict, reason: str) -> None:
    assert derive_availability(HEALTHY | override, PriceKind.ON_DEMAND) is (
        Availability.CONSTRAINED
    ), reason


def test_vast_never_reports_unknown() -> None:
    """Vast tells the truth about obtainability; UNKNOWN is reserved for AWS."""
    for override in ({}, {"rentable": False}, {"rented": True}, {"reliability2": 0.1}):
        for kind in PriceKind:
            assert derive_availability(HEALTHY | override, kind) is not Availability.UNKNOWN


def test_obtainability_outranks_price() -> None:
    """The core thesis: a cheaper node you cannot get must never beat one you can."""
    assert obtainability_rank(Availability.AVAILABLE) < obtainability_rank(
        Availability.CONSTRAINED
    ) < obtainability_rank(Availability.UNKNOWN) < obtainability_rank(Availability.UNAVAILABLE)


# --- normalization guards ----------------------------------------------------


def test_one_machine_yields_on_demand_and_spot_rows() -> None:
    offerings = VastProvider().normalize(MACHINE, NOW)

    by_kind = {o.price_kind: o for o in offerings}
    assert by_kind[PriceKind.ON_DEMAND].price_usd_hr == 16.0
    assert by_kind[PriceKind.SPOT].price_usd_hr == 3.57
    # Price is stored per node; per-GPU is derived.
    assert by_kind[PriceKind.ON_DEMAND].price_per_gpu_hr == 2.0
    assert all(o.external_id == "54813" for o in offerings)
    assert all(o.gpu_model == "H100_SXM_80GB" for o in offerings)


def test_region_is_kept_provider_native() -> None:
    """'Japan, JP' is not mapped onto an AWS-style region. There is no honest mapping."""
    assert VastProvider().normalize(MACHINE, NOW)[0].region == "Japan, JP"


def test_spot_priced_above_on_demand_is_kept_not_dropped() -> None:
    """A bid floor above the on-demand rate is a real market state, not a field mixup.

    ~10% of live machines report one -- the box is contended, or the host set a high
    bid floor to discourage interruptible use. The quote is true and simply
    unattractive, so it never wins the floor. Dropping it would understate contention
    and undercount supply.
    """
    contended = MACHINE | {"min_bid": 99.0, "dph_total": 16.0}
    by_kind = {o.price_kind: o for o in VastProvider().normalize(contended, NOW)}
    assert by_kind[PriceKind.SPOT].price_usd_hr == 99.0
    assert by_kind[PriceKind.ON_DEMAND].price_usd_hr == 16.0


def test_is_bid_listing_emits_no_on_demand_row() -> None:
    """When is_bid is set, dph_total is itself a bid quote -- there is no on-demand product."""
    kinds = {o.price_kind for o in VastProvider().normalize(MACHINE | {"is_bid": True}, NOW)}
    assert kinds == {PriceKind.SPOT}


def test_machines_get_distinct_series_keys() -> None:
    """Two hosts with the same GPU in the same region are different sellable things."""
    a = VastProvider().normalize(MACHINE, NOW)[0]
    b = VastProvider().normalize(MACHINE | {"machine_id": 999}, NOW)[0]
    assert a.series_key != b.series_key


def test_fetch_unions_both_sort_orders() -> None:
    """A single sort order cannot see the spot floor -- verified live against RTX 4090.

    Each sort key returns a different server-side slice, so the cheapest bid can be
    absent from the price-sorted slice entirely. fetch() must query both and union.
    """
    seen_orders: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.url.params["q"])
        order_by = query["order"][0][0]
        seen_orders.append(order_by)
        # Each slice exposes a machine the other one does not.
        machine = {"dph_total": 54813, "min_bid": 60001}[order_by]
        return httpx.Response(200, json={"offers": [MACHINE | {"machine_id": machine}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    offerings = VastProvider(watchlist=("H100 SXM",), client=client).fetch()

    assert sorted(seen_orders) == ["dph_total", "min_bid"]
    assert {o.external_id for o in offerings} == {"54813", "60001"}, (
        "machines unique to one sort order were lost"
    )


# --- GATE 0 ------------------------------------------------------------------


@pytest.mark.live
def test_gate_0_live_normalization() -> None:
    """GATE 0: fetch H100s live and assert normalization holds on real values."""
    offerings = VastProvider(watchlist=("H100 SXM",)).fetch()

    assert offerings, "Vast returned no H100 SXM offerings"

    for o in offerings:
        assert o.provider == "vast"
        assert o.gpu_model == "H100_SXM_80GB"
        assert is_canonical(o.gpu_model)
        assert o.gpu_count > 0
        assert o.region
        assert o.external_id
        assert o.instance_type
        assert o.observed_at.tzinfo is not None
        assert isinstance(o.availability, Availability)
        assert o.availability is not Availability.UNKNOWN

        # Sane band: a real H100 is neither free nor $100/hr/GPU.
        assert 0 < o.price_per_gpu_hr < 100, f"{o.price_per_gpu_hr} $/GPU/hr is not credible"

        if o.availability_score is not None:
            assert 0.0 <= o.availability_score <= 1.0

    # Both price kinds must survive normalization -- the spot floor is the product.
    kinds = {o.price_kind for o in offerings}
    assert kinds == {PriceKind.SPOT, PriceKind.ON_DEMAND}

    # Interruptible is cheaper than on-demand *in aggregate*. This is deliberately a
    # median rather than a per-machine invariant: individual contended machines
    # genuinely quote a bid floor above their on-demand rate. A median that inverted
    # would mean we had swapped dph_total and min_bid.
    spot = median([o.price_per_gpu_hr for o in offerings if o.price_kind is PriceKind.SPOT])
    on_demand = median(
        [o.price_per_gpu_hr for o in offerings if o.price_kind is PriceKind.ON_DEMAND]
    )
    assert spot < on_demand, f"median spot {spot} >= on-demand {on_demand}; fields likely swapped"


@pytest.mark.live
def test_min_bid_is_quoted_per_node() -> None:
    """GATE 0: pin the units of min_bid against live data.

    This is the assumption that would silently misprice every multi-GPU spot quote.
    If min_bid were per-GPU while dph_total is per-node, then dph_total/min_bid would
    grow roughly in proportion to node size. It does not: the ratio is flat from 1 to
    8 GPUs, so both fields share the same per-node basis.
    """
    offers: list[dict] = []
    provider = VastProvider()
    for gpu_name in ("RTX 4090", "A100 SXM4"):  # models with enough listings to be stable
        offers.extend(provider._search(gpu_name, "dph_total"))

    def ratios(predicate) -> list[float]:
        return [
            o["dph_total"] / o["min_bid"]
            for o in offers
            if o.get("min_bid") and o.get("dph_total") and predicate(o["num_gpus"])
        ]

    single = ratios(lambda n: n == 1)
    multi = ratios(lambda n: n >= 4)
    assert single and multi, "not enough live listings to compare node sizes"

    # Under a per-GPU reading this ratio would be ~4-8x larger for multi-GPU nodes.
    assert median(multi) < 3 * median(single), (
        f"dph_total/min_bid scales with node size (1 GPU: {median(single):.2f}, "
        f">=4 GPU: {median(multi):.2f}) -- min_bid is per-GPU, not per-node"
    )
