"""Vast.ai provider -- the live-inventory availability showcase.

Vast publishes real, per-machine inventory through a public search API with no
auth, which makes it the one provider where "can I actually get this right now"
is a fact rather than an inference. That is why it is the reference provider.

**Each machine yields two offerings.** ``dph_total`` is the on-demand rate for the
whole node; the separate ``min_bid`` field is the interruptible bid floor for that
same node. They are genuinely different products with different durability, so
they normalize into an ``ON_DEMAND`` and a ``SPOT`` offering. Both are quoted
**per node**, which was verified rather than assumed: if ``min_bid`` were per-GPU,
``dph_total / min_bid`` would scale with node size, and measured across live
listings it does not (RTX 4090: 1.09 at 1 GPU, 1.05 at 8). Getting this backwards
would misprice every multi-GPU spot quote by up to 8x.

**Every sort order returns a different slice, so we union two.** The search
endpoint does not return all matching machines -- it returns a server-side slice
whose membership depends on ``order``. Measured live on RTX 4090: ordering by
``dph_total`` reported a spot floor of $0.1200/GPU/hr when the true floor was
$0.1067 (an 11% overstatement), because 15 machines with cheaper bids were simply
absent from the price-sorted slice. Both slices were under the result cap, so this
is not truncation -- a single sort order *cannot* see the spot floor. We therefore
query once per sort key and union by ``machine_id``. In a product named spotfloor,
this is the one bias that cannot ship.

Offerings are emitted **per machine**, not pre-aggregated to a floor. The floor is
a read-time query (``MIN``), which keeps ingestion lossless and lets the store also
answer supply depth -- *how many* nodes you could actually get -- which is a
stronger availability signal than the price of the single cheapest one.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from spotfloor.gpu import canonical_gpu_model
from spotfloor.models import Availability, InstanceOffering, PriceKind

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://console.vast.ai/api/v0/search/asks/"

# Observed maximum rows the search endpoint returns per query.
_RESULT_CAP = 64

# Below this, Vast's own host-reliability score says the box churns. Obtainable,
# but you should not count on keeping it -- which is `constrained`, not `available`.
RELIABILITY_FLOOR = 0.95

# Exact `gpu_name` strings as Vast reports them; verified live against the API.
# Bounded on purpose: the endpoint caps results, so a targeted watchlist returns
# usable data per model instead of a truncated slice of "everything".
DEFAULT_WATCHLIST = (
    "H100 SXM",
    "H100 NVL",
    "H200",
    "A100 SXM4",
    "B200",
    "L40S",
    "RTX 4090",
    "RTX 5090",
)

# One query per sort key; the union is what makes the spot floor honest.
_SORT_KEYS = ("dph_total", "min_bid")


def derive_availability(offer: dict[str, Any], price_kind: PriceKind) -> Availability:
    """Decide whether a Vast listing can actually be obtained and held.

    The rule below is derived entirely from fields Vast publishes; nothing is
    inferred or guessed. It answers the two halves of the product question
    separately:

    *Can I get it now?* -- ``rentable`` is Vast's own statement that the host has
    free capacity, and ``rented`` says it is currently occupied. If either says no,
    the answer is ``UNAVAILABLE``, regardless of how good the price looks.

    *Will I keep it?* -- three things downgrade an obtainable box to
    ``CONSTRAINED`` rather than ``AVAILABLE``:

    1. ``verification == "deverified"``: Vast has actively flagged this host. You
       can still rent it; you should not rely on it.
    2. ``reliability2 < RELIABILITY_FLOOR``: Vast's own host-uptime score says it
       churns.
    3. ``price_kind == SPOT``: an interruptible bid is preemptible *by design* --
       another bidder outbids you and you lose the box. A spot listing is
       therefore never better than ``CONSTRAINED``, however healthy the host is.

    Anything obtainable that trips none of these is ``AVAILABLE``.

    Vast never yields ``UNKNOWN``: it tells us the truth about obtainability, which
    is exactly what AWS cannot do.
    """
    if not offer.get("rentable") or offer.get("rented"):
        return Availability.UNAVAILABLE

    if offer.get("verification") == "deverified":
        return Availability.CONSTRAINED

    reliability = offer.get("reliability2")
    if reliability is not None and reliability < RELIABILITY_FLOOR:
        return Availability.CONSTRAINED

    if price_kind is PriceKind.SPOT:
        return Availability.CONSTRAINED

    return Availability.AVAILABLE


def _region(offer: dict[str, Any]) -> str:
    """Vast reports a free-text geolocation ('Japan, JP'). Kept provider-native."""
    geo = (offer.get("geolocation") or "").strip()
    return re.sub(r"\s+", " ", geo) or "unknown"


class VastProvider:
    """Polls Vast.ai's public search API. No credentials required."""

    name = "vast"

    def __init__(
        self,
        watchlist: tuple[str, ...] = DEFAULT_WATCHLIST,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._watchlist = watchlist
        self._timeout = timeout
        self._client = client

    def _search(self, gpu_name: str, order_by: str) -> list[dict[str, Any]]:
        query = {
            "gpu_name": {"eq": gpu_name},
            "rentable": {"eq": True},
            "type": "on-demand",
            "order": [[order_by, "asc"]],
        }
        client = self._client or httpx.Client(timeout=self._timeout)
        try:
            response = client.get(_SEARCH_URL, params={"q": json.dumps(query)})
            response.raise_for_status()
            offers: list[dict[str, Any]] = response.json().get("offers", [])
        finally:
            if self._client is None:
                client.close()

        if len(offers) >= _RESULT_CAP:
            # We are seeing a slice, not the market. Anything downstream is "the
            # cheapest we observed", never "the cheapest that exists".
            logger.warning(
                "vast: %s sorted by %s hit the %d-row cap; results are a slice",
                gpu_name,
                order_by,
                _RESULT_CAP,
            )
        return offers

    def normalize(
        self, offer: dict[str, Any], observed_at: datetime
    ) -> list[InstanceOffering]:
        """Expand one Vast machine into its on-demand and interruptible offerings."""
        gpu_count = offer.get("num_gpus") or 0
        machine_id = offer.get("machine_id")
        if gpu_count <= 0 or machine_id is None:
            return []

        gpu_model = canonical_gpu_model(offer.get("gpu_name", ""), offer.get("gpu_ram"))

        # `is_bid` means dph_total is itself a bid quote rather than an on-demand
        # rate, so there is no on-demand product to emit for this listing.
        prices: list[tuple[PriceKind, Any]] = []
        if not offer.get("is_bid"):
            prices.append((PriceKind.ON_DEMAND, offer.get("dph_total")))

        # `min_bid` above `dph_total` is kept, not dropped: it is a real market state,
        # not a field mixup. Roughly 10% of listed machines report one, either because
        # the box is contended (you must outbid whoever holds it, which can exceed the
        # on-demand rate) or because the host set a high bid floor to discourage
        # interruptible use. Either way the quote is true and simply unattractive, so
        # it never wins the floor. Discarding it would understate contention and
        # undercount supply. Both fields are per-node -- see
        # `test_min_bid_is_quoted_per_node`, which pins that empirically.
        prices.append((PriceKind.SPOT, offer.get("min_bid")))

        region = _region(offer)
        offerings = []
        for price_kind, price in prices:
            if not price or price <= 0:
                continue
            offerings.append(
                InstanceOffering(
                    provider=self.name,
                    external_id=str(machine_id),
                    instance_type=f"{gpu_count}x{gpu_model}",
                    gpu_model=gpu_model,
                    gpu_count=gpu_count,
                    region=region,
                    # Vast sells a free-text geolocation with no sub-region concept;
                    # inventing a zone to fill the column would be fake precision.
                    zone=None,
                    price_usd_hr=float(price),
                    price_kind=price_kind,
                    availability=derive_availability(offer, price_kind),
                    # Vast's native host-reliability score, passed through unmodified.
                    # It is a durability signal ("will this host stay up"), NOT a
                    # fulfillment probability -- do not relabel it as one.
                    availability_score=offer.get("reliability2"),
                    observed_at=observed_at,
                )
            )
        return offerings

    def fetch(self) -> list[InstanceOffering]:
        """Return every machine currently listed for the watchlisted GPU models."""
        observed_at = datetime.now(UTC)

        # Union the sort orders, then dedup by machine: the same host appears in both
        # slices, and the two slices are not the same set of machines.
        machines: dict[Any, dict[str, Any]] = {}
        for gpu_name in self._watchlist:
            for order_by in _SORT_KEYS:
                for offer in self._search(gpu_name, order_by):
                    machine_id = offer.get("machine_id")
                    if machine_id is not None:
                        machines[machine_id] = offer

        offerings: list[InstanceOffering] = []
        for offer in machines.values():
            offerings.extend(self.normalize(offer, observed_at))
        return offerings
