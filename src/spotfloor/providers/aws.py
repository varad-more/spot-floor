"""AWS spot pricing across regions -- and honest about what it cannot know.

**AWS does not expose spot availability.** The closest thing is the Spot Placement
Score API, and it is not a market fact: AWS computes the score *against the calling
account's* quotas, limits and usage history. Measured live, ``p5.48xlarge`` scored
1/10 for this repo's account across every AZ -- which says something about this
account, not about whether *you* could get an H100.

So a score fetched with the application's own credentials is not merely imprecise,
it is **about the wrong account**. Publishing it as a market signal would be
fabrication. With app credentials this provider therefore reports
``Availability.UNKNOWN`` and never calls the placement-score API at all. That is
enforced by a test, not by a comment.

The only honest way to give a user a real AWS availability signal is to compute it
with *their* credentials (``CredsOwner.USER``), which is off by default.

Consequence for this tool: it is a **price** comparator, not an availability one,
and the UI must say so wherever a blank availability cell would otherwise read as
"none available".

---

**Spot price history is a change-log, not a sample series.** AWS emits a row
precisely when a price changes, and retains ~89 days (measured). Two things follow:

* the deep history a chart needs is one API call away, so charts are full-depth on
  a cold start instead of waiting for a poller to accumulate; and
* those rows *are* storage segments -- quote N's timestamp opens a segment and
  quote N+1's closes it -- so :meth:`AwsProvider.history_segments` feeds
  ``store.backfill`` directly, with boundaries given rather than inferred.

**Instance specs are global; only availability is regional.** ``m5.large`` has 2
vCPUs everywhere, so the catalog is fetched once and reused for every region.
Whether a type is *offered* in a region does vary -- and that needs no extra call,
because a type absent from a region simply returns no price history there.

---

**On-demand is a different API and a different shape.** Spot comes from EC2's
per-zone change-log; on-demand list prices come from the Price List Query API
(:meth:`AwsProvider.on_demand_prices`), which is first-party, free, and needs no
HTML scraping. Two consequences the rest of the code has to respect:

* **on-demand has no zone.** AWS charges one on-demand rate per *region*, so the
  intra-region spread that justifies this tool's whole roll-up simply does not
  exist for it. Its offerings carry ``zone=None``, and nothing may render one.
* **it has no published history.** There is no on-demand equivalent of
  ``DescribeSpotPriceHistory``, so :meth:`history_segments` stays spot-only and the
  on-demand series accumulates forward from the first poll. A backfilled chart that
  claimed to know last month's list price would be inventing it.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Callable, Iterable, Sequence

from spotfloor.gpu import canonical_gpu_model
from spotfloor.models import Availability, InstanceOffering, PriceKind
from spotfloor.storage.base import OfferingRecord

logger = logging.getLogger(__name__)

# How far back `fetch` looks to establish the *current* price. Long enough that a
# quiet instance type still has a quote, short enough to stay cheap.
_CURRENT_WINDOW = timedelta(hours=6)

# Spot Placement Score is 1..10. Thresholds are deliberately conservative: AWS
# states the score is a likelihood, never a guarantee of capacity.
_SPS_AVAILABLE = 8
_SPS_CONSTRAINED = 4

# Prices are compared at micro-dollar precision, matching the store's state_hash.
# Anything finer is float noise from JSON round-tripping, not a market move -- and
# treating noise as a change would shatter every segment into per-quote fragments.
_PRICE_QUANTUM = 1_000_000

# The Price List Query API is a global catalog served from only two endpoints.
# us-east-1 is the one every account can reach without opting a region in.
_PRICING_REGION = "us-east-1"

# Pins the on-demand SKU to the same product the spot side prices: shared-tenancy
# Linux, no bundled software, on capacity that is actually being used. All four are
# load-bearing -- without them one instance type returns a dozen SKUs (Windows,
# SUSE, dedicated tenancy, SQL Server, reserved-unused) and "the on-demand price"
# silently becomes whichever one happened to sort first.
_ON_DEMAND_FILTERS: tuple[tuple[str, str], ...] = (
    ("operatingSystem", "Linux"),
    ("tenancy", "Shared"),
    ("preInstalledSw", "NA"),
    ("capacitystatus", "Used"),
)

# A bounded watchlist across the families people actually shop between. Bounded on
# purpose: us-east-1 alone lists 1,354 instance types, and 17 regions x 1,354 types
# x ~2,000 history rows is ~46M rows and ~57k API calls -- neither pollable on a
# schedule nor publishable as a static page.
DEFAULT_INSTANCE_TYPES: tuple[str, ...] = (
    # GPU / accelerated
    "p5.48xlarge", "p4d.24xlarge", "g6.xlarge", "g6.12xlarge", "g6e.xlarge",
    "g5.xlarge", "g5.12xlarge", "g4dn.xlarge",
    # General purpose (incl. Graviton)
    "m5.large", "m5.xlarge", "m5.2xlarge", "m6i.large", "m6i.xlarge",
    "m7i.large", "m7i.xlarge", "m6g.large", "m7g.large", "m7g.xlarge",
    # Compute optimized
    "c5.large", "c5.xlarge", "c5.2xlarge", "c6i.large", "c6i.xlarge",
    "c7i.large", "c7i.xlarge", "c6g.large", "c7g.large", "c7g.xlarge",
    # Memory optimized
    "r5.large", "r5.xlarge", "r6i.large", "r6i.xlarge", "r7i.large", "r7g.large",
    # Burstable
    "t3.medium", "t3.large", "t4g.medium", "t4g.large",
    # Storage optimized
    "i4i.large", "i3.large",
)


class CredsOwner(StrEnum):
    """Whose AWS account the credentials belong to.

    This is not a config knob so much as a truth claim: it decides whether an
    availability signal is *about the user* or about us.
    """

    APP = "app"
    USER = "user"


@dataclass(frozen=True, slots=True)
class InstanceSpec:
    """Hardware facts from ``DescribeInstanceTypes``. Global, not per-region."""

    vcpus: int | None
    memory_gib: float | None
    gpu_model: str | None
    gpu_count: int


def _instance_family(instance_type: str) -> str:
    """'p5.48xlarge' -> 'p5'; 'p6-b200.48xlarge' -> 'p6-b200'."""
    return instance_type.split(".", 1)[0]


def _quantize(price: float) -> int:
    return round(price * _PRICE_QUANTUM)


def _error_code(exc: Exception) -> str:
    """botocore's error code if there is one, else the exception class name."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code")
        if code:
            return str(code)
    return type(exc).__name__


def _default_client_factory(region: str) -> Any:
    """One client per region, each from its own Session (thread-safe construction)."""
    import boto3

    return boto3.session.Session().client("ec2", region_name=region)


def _default_pricing_factory() -> Any:
    """A Price List Query client. Separate from the EC2 factory: different service."""
    import boto3

    return boto3.session.Session().client("pricing", region_name=_PRICING_REGION)


def enabled_regions(client: Any) -> list[str]:
    """Regions this account can actually call.

    ``describe_regions`` without ``AllRegions`` returns only enabled ones, which is
    what we want: this account has 17 opt-in regions it has not enabled, and every
    one of them would raise ``AuthFailure``. A comparator that lists regions it
    cannot price is worse than one that admits its scope.
    """
    return sorted(r["RegionName"] for r in client.describe_regions()["Regions"])


class AwsProvider:
    """EC2 spot pricing across regions. Availability is UNKNOWN unless user-owned."""

    name = "aws"

    def __init__(
        self,
        *,
        regions: Sequence[str] | None = None,
        instance_types: Sequence[str] = DEFAULT_INSTANCE_TYPES,
        client_factory: Callable[[str], Any] | None = None,
        pricing_factory: Callable[[], Any] | None = None,
        creds_owner: CredsOwner = CredsOwner.APP,
        catalog_region: str = "us-east-1",
        max_workers: int = 8,
    ) -> None:
        """``regions=None`` discovers every enabled region on first use.

        ``max_workers`` fans out across regions concurrently. Safe because EC2 API
        throttles are applied *per region*, so parallel calls to different regions
        do not contend for one another's token bucket. It is a real speedup, not a
        micro-optimisation: a 90-day backfill over 17 regions is ~10 minutes serial
        and well under two in parallel.
        """
        self._regions = list(regions) if regions is not None else None
        self._instance_types = tuple(instance_types)
        self._client_factory = client_factory or _default_client_factory
        self._pricing_factory = pricing_factory or _default_pricing_factory
        self._creds_owner = creds_owner
        self._catalog_region = catalog_region
        self._max_workers = max_workers

        self._clients: dict[str, Any] = {}
        self._catalog: dict[str, InstanceSpec] | None = None
        self._all_types: list[str] | None = None
        self._failures: dict[str, str] = {}
        self._pricing_failure: str | None = None
        self._on_demand: dict[tuple[str, str], float] | None = None
        self._scores: dict[str, tuple[Availability, float | None]] = {}

    # --- wiring --------------------------------------------------------------

    def _client(self, region: str) -> Any:
        if region not in self._clients:
            self._clients[region] = self._client_factory(region)
        return self._clients[region]

    def regions(self) -> list[str]:
        if self._regions is None:
            self._regions = enabled_regions(self._client(self._catalog_region))
        return self._regions

    @property
    def notes(self) -> list[str]:
        """Human-readable caveats for the page: which regions we could not price.

        A failed region must never just vanish from the table -- an absent region is
        indistinguishable from a region with no capacity, and those are very
        different claims. The same reasoning covers a blank savings column: it must
        say *why* it is blank rather than read as "spot saves you nothing".
        """
        notes = [
            f"{region} could not be priced ({error}), so it is absent from the table."
            for region, error in sorted(self._failures.items())
        ]
        if self._pricing_failure:
            notes.append(
                f"On-demand list prices are unavailable ({self._pricing_failure}), so "
                "the on-demand and savings columns are blank. Add pricing:GetProducts "
                "to the IAM policy (docs/iam-policy.json)."
            )
        return notes

    # --- catalog -------------------------------------------------------------

    def catalog(self) -> dict[str, InstanceSpec]:
        """Instance type -> hardware spec, from the official API, fetched once.

        Fetched from a single region because these facts are global: ``m5.large`` is
        2 vCPU / 8 GiB everywhere. Only *whether a type is offered* varies
        regionally, and that costs no call -- an unoffered type simply has no price
        history there.

        ``DescribeInstanceTypes`` is authoritative about GPU name, count and VRAM,
        so the GPU mapping is derived rather than hand-maintained. AWS omits the
        interconnect from the GPU name ("H100"), so the instance family supplies it
        (``p5`` -> SXM); see :func:`spotfloor.gpu.canonical_gpu_model`.
        """
        if self._catalog is not None:
            return self._catalog

        wanted = set(self._instance_types)
        catalog: dict[str, InstanceSpec] = {}
        paginator = self._client(self._catalog_region).get_paginator(
            "describe_instance_types"
        )
        # Filtered server-side rather than fetched-then-discarded: measured 3.52s ->
        # 1.85s for a 40-type watchlist against us-east-1's 1,354 types. A `Filters`
        # entry is used instead of the `InstanceTypes` argument on purpose -- passing
        # an unknown type in `InstanceTypes` raises `InvalidInstanceType` and takes
        # the whole catalog down, whereas a filter simply does not match it. That
        # matters because the watchlist is user-editable.
        for page in paginator.paginate(
            Filters=[{"Name": "instance-type", "Values": sorted(wanted)}]
        ):
            for spec in page["InstanceTypes"]:
                instance_type = spec["InstanceType"]
                if instance_type in wanted:
                    catalog[instance_type] = self._spec(instance_type, spec)

        missing = wanted - catalog.keys()
        if missing:
            logger.warning("aws: not in the instance catalog: %s", sorted(missing))

        self._catalog = catalog
        return catalog

    def full_catalog(self) -> list[str]:
        """Every instance type name EC2 offers, for the scan picker to offer.

        Deliberately not :meth:`catalog`, which is filtered to the watchlist and
        returns specs. This returns *names only* -- 1,354 of them in us-east-1 --
        because a picker has to be able to offer a type before anything has ever
        priced it. Without this you can only rescan what you already have, which is
        the opposite of what a picker is for.

        Discovered rather than hardcoded, for the same reason regions are: AWS ships
        new families, and a list baked into this repo would be wrong from the next
        launch announcement onward.

        One region is enough. Which types a region *offers* varies, but asking for
        one it does not offer costs nothing -- the scan simply returns no quotes for
        it, the same as any unpriced type.
        """
        if self._all_types is None:
            paginator = self._client(self._catalog_region).get_paginator(
                "describe_instance_types"
            )
            self._all_types = sorted(
                spec["InstanceType"]
                for page in paginator.paginate()
                for spec in page["InstanceTypes"]
            )
        return self._all_types

    @staticmethod
    def _spec(instance_type: str, spec: dict[str, Any]) -> InstanceSpec:
        gpu_model: str | None = None
        gpu_count = 0
        gpu_info = spec.get("GpuInfo")
        if gpu_info and gpu_info.get("Gpus"):
            gpu = gpu_info["Gpus"][0]
            if gpu.get("Manufacturer") == "NVIDIA":
                gpu_count = gpu.get("Count", 0)
                gpu_model = canonical_gpu_model(
                    gpu["Name"],
                    gpu.get("MemoryInfo", {}).get("SizeInMiB"),
                    aws_instance_family=_instance_family(instance_type),
                )

        memory_mib = spec.get("MemoryInfo", {}).get("SizeInMiB")
        return InstanceSpec(
            vcpus=spec.get("VCpuInfo", {}).get("DefaultVCpus"),
            memory_gib=round(memory_mib / 1024, 2) if memory_mib else None,
            gpu_model=gpu_model,
            gpu_count=gpu_count,
        )

    # --- availability --------------------------------------------------------

    def _availability(self, instance_type: str) -> tuple[Availability, float | None]:
        """Availability, or an honest admission that we cannot know it.

        With app credentials this returns UNKNOWN *without calling* the
        placement-score API, because a score computed against our account would be a
        statement about our quota, not about the user's odds of getting capacity.

        Memoized per instance type, which is what the call actually varies on. Not an
        optimisation: this runs once per *offering*, and ``history_segments`` builds
        ~172k of them, so an unmemoized USER-credential run would fire six figures of
        API calls to ask the same few questions.
        """
        if self._creds_owner is CredsOwner.APP:
            return Availability.UNKNOWN, None

        if instance_type in self._scores:
            return self._scores[instance_type]

        result = self._fetch_placement_score(instance_type)
        self._scores[instance_type] = result
        return result

    def _fetch_placement_score(
        self, instance_type: str
    ) -> tuple[Availability, float | None]:
        try:
            response = self._client(self._catalog_region).get_spot_placement_scores(
                InstanceTypes=[instance_type],
                TargetCapacity=1,
                TargetCapacityUnitType="units",
                SingleAvailabilityZone=True,
            )
        except Exception:
            logger.warning("aws: placement score unavailable for %s", instance_type)
            return Availability.UNKNOWN, None

        scores = [s["Score"] for s in response.get("SpotPlacementScores", [])]
        if not scores:
            return Availability.UNKNOWN, None

        best = max(scores)
        if best >= _SPS_AVAILABLE:
            availability = Availability.AVAILABLE
        elif best >= _SPS_CONSTRAINED:
            availability = Availability.CONSTRAINED
        else:
            availability = Availability.UNAVAILABLE
        return availability, best / 10

    # --- on-demand list prices -----------------------------------------------

    def on_demand_prices(self) -> dict[tuple[str, str], float]:
        """``(instance_type, region) -> on-demand $/hr`` from the Price List API.

        This is the official, free, first-party source. It needs no scraping and no
        hand-maintained price table, and the ``regionCode`` attribute means no
        long-name mapping ("US East (N. Virginia)") has to exist in order to be
        wrong.

        **Omitting the region filter is what makes it affordable.** One paginated
        call returns every region for an instance type -- measured at 0.76s and a
        single page for ``m5.large`` across 33 USD regions -- so the cost is
        O(types), not O(types x regions). A 40-type watchlist is 40 calls, not 680.

        **Non-USD regions are skipped, never converted.** The China regions quote
        CNY, and turning that into dollars would require an exchange rate we did not
        observe. That is the same rule that keeps an unobserved bucket ``None``
        rather than zero: absence is not a value.

        A failure here degrades to an empty mapping and a note rather than raising.
        Losing the savings column must not take the spot table down with it.
        """
        if self._on_demand is not None:
            return self._on_demand

        self._pricing_failure = None
        prices: dict[tuple[str, str], float] = {}
        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures = {
                pool.submit(self._on_demand_for_type, instance_type): instance_type
                for instance_type in self._instance_types
            }
            for future, instance_type in futures.items():
                try:
                    prices.update(future.result())
                except Exception as exc:  # noqa: BLE001 - a missing column, not a dead app
                    logger.warning(
                        "aws: on-demand price unavailable for %s: %s", instance_type, exc
                    )
                    self._pricing_failure = _error_code(exc)

        # ponytail: memoized for the life of the provider. On-demand list prices
        # change a few times a year, not every five minutes, and the poller holds one
        # provider for the process lifetime -- so a restart is the refresh. Add a TTL
        # if a long-running server ever needs to see a mid-run reprice.
        self._on_demand = prices
        return prices

    def _on_demand_for_type(self, instance_type: str) -> dict[tuple[str, str], float]:
        """Every region's on-demand rate for one instance type, in one paginated call.

        Builds its own client rather than sharing one across the pool: botocore
        recommends a client per thread, and this runs once per provider, so 40
        constructions cost less than reasoning about whether sharing is safe.
        """
        client = self._pricing_factory()
        filters = [
            {"Type": "TERM_MATCH", "Field": field, "Value": value}
            for field, value in (("instanceType", instance_type), *_ON_DEMAND_FILTERS)
        ]

        found: dict[tuple[str, str], float] = {}
        for page in client.get_paginator("get_products").paginate(
            ServiceCode="AmazonEC2", Filters=filters
        ):
            for blob in page["PriceList"]:
                product = json.loads(blob)
                region = product["product"]["attributes"].get("regionCode")
                if not region:
                    continue
                for term in product.get("terms", {}).get("OnDemand", {}).values():
                    for dimension in term.get("priceDimensions", {}).values():
                        per_unit = dimension.get("pricePerUnit", {})
                        if "USD" not in per_unit:
                            continue  # CNY-quoted region; see the docstring.
                        price = float(per_unit["USD"])
                        # A $0.00 dimension is a free-tier or placeholder SKU, not a
                        # price -- and `price_usd_hr` is constrained to be positive.
                        if price > 0:
                            found.setdefault((instance_type, region), price)
        return found

    def _on_demand_offerings(
        self, *, observed_at: datetime, regions: Iterable[str]
    ) -> list[InstanceOffering]:
        """On-demand prices as offerings, so they are stored as their own series.

        Its own :class:`PriceKind` rather than a column bolted onto the spot row,
        because ``series_key`` already treats price kind as identity: on-demand and
        spot are different products with different durability, and folding them into
        one series would make it look like a single price thrashing by 10x.

        ``regions`` is the set that actually answered, not every configured region.
        The Price List API is a global catalog and will happily quote a region this
        account cannot call, but :attr:`notes` has already promised that a failed
        region is "absent from the table" -- emitting an on-demand-only row for it
        would make that note false and would price capacity you cannot launch.
        """
        catalog = self.catalog()
        wanted = set(regions)

        offerings: list[InstanceOffering] = []
        for (instance_type, region), price in self.on_demand_prices().items():
            spec = catalog.get(instance_type)
            if spec is None or region not in wanted:
                continue
            offerings.append(
                InstanceOffering(
                    provider=self.name,
                    instance_type=instance_type,
                    region=region,
                    # No zone, deliberately: AWS charges one on-demand rate for the
                    # whole region. Naming a zone would fabricate a price difference
                    # between zones that does not exist for this product.
                    zone=None,
                    price_usd_hr=price,
                    price_kind=PriceKind.ON_DEMAND,
                    # On-demand capacity is not guaranteed either -- AWS returns
                    # InsufficientInstanceCapacity often enough that claiming
                    # otherwise would be the same unearned promise we refuse on spot.
                    availability=Availability.UNKNOWN,
                    availability_score=None,
                    observed_at=observed_at,
                    gpu_model=spec.gpu_model,
                    gpu_count=spec.gpu_count,
                    vcpus=spec.vcpus,
                    memory_gib=spec.memory_gib,
                )
            )
        return offerings

    # --- raw quotes ----------------------------------------------------------

    def _quotes(self, region: str, start: datetime) -> list[dict[str, Any]]:
        """Every spot quote in ``region`` since ``start``, for the watchlist.

        One paginated call covers the whole watchlist: ``InstanceTypes`` takes a
        list, so this is O(regions) API round-trips rather than O(regions x types).
        Types not offered in the region simply return nothing.
        """
        quotes: list[dict[str, Any]] = []
        paginator = self._client(region).get_paginator("describe_spot_price_history")
        for page in paginator.paginate(
            InstanceTypes=list(self._instance_types),
            ProductDescriptions=["Linux/UNIX"],
            StartTime=start,
        ):
            quotes.extend(page["SpotPriceHistory"])
        return quotes

    def _per_region(self, start: datetime) -> dict[str, list[dict[str, Any]]]:
        """Fan out across regions, recording failures instead of raising.

        One unreachable region must not take the other sixteen down with it. Opt-in
        regions the account has not enabled raise ``AuthFailure`` here, which is an
        expected path rather than a bug -- but it is *reported*, via :attr:`notes`.
        """
        self._failures = {}
        results: dict[str, list[dict[str, Any]]] = {}

        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures = {
                pool.submit(self._quotes, region, start): region
                for region in self.regions()
            }
            for future, region in futures.items():
                try:
                    results[region] = future.result()
                except Exception as exc:  # noqa: BLE001 - one region must not fail the rest
                    logger.warning("aws: region %s failed: %s", region, exc)
                    self._failures[region] = _error_code(exc)
        return results

    def _offering(
        self,
        quote: dict[str, Any],
        region: str,
        spec: InstanceSpec,
        *,
        price: float,
        observed_at: datetime,
    ) -> InstanceOffering:
        availability, score = self._availability(quote["InstanceType"])
        return InstanceOffering(
            provider=self.name,
            instance_type=quote["InstanceType"],
            region=region,
            # The AZ verbatim, alongside its region. Zones within one region are
            # separately priced, which is the whole reason both fields exist.
            zone=quote["AvailabilityZone"],
            price_usd_hr=price,
            price_kind=PriceKind.SPOT,
            availability=availability,
            availability_score=score,
            observed_at=observed_at,
            gpu_model=spec.gpu_model,
            gpu_count=spec.gpu_count,
            vcpus=spec.vcpus,
            memory_gib=spec.memory_gib,
        )

    # --- the two read paths --------------------------------------------------

    def fetch(self) -> list[InstanceOffering]:
        """Current spot prices per (type, zone), plus the on-demand rate per region.

        Both kinds come back from one call because both belong in one tick: the
        savings figure is only meaningful when the two prices were read together.
        """
        observed_at = datetime.now(UTC)
        catalog = self.catalog()
        offerings: list[InstanceOffering] = []

        per_region = self._per_region(observed_at - _CURRENT_WINDOW)
        for region, quotes in per_region.items():
            newest: dict[tuple[str, str], dict[str, Any]] = {}
            for quote in quotes:
                key = (quote["InstanceType"], quote["AvailabilityZone"])
                incumbent = newest.get(key)
                if incumbent is None or quote["Timestamp"] > incumbent["Timestamp"]:
                    newest[key] = quote

            for (instance_type, _zone), quote in newest.items():
                spec = catalog.get(instance_type)
                if spec is None:
                    # Unknown silicon is dropped, never guessed at.
                    continue
                price = float(quote["SpotPrice"])
                if price <= 0:
                    continue
                offerings.append(
                    self._offering(
                        quote, region, spec, price=price, observed_at=observed_at
                    )
                )

        offerings.extend(
            self._on_demand_offerings(observed_at=observed_at, regions=per_region)
        )
        return offerings

    def history_segments(self, *, days: int = 30) -> list[OfferingRecord]:
        """Deep history as storage segments, ready for ``store.backfill``.

        AWS emits a quote when a price *changes*, so consecutive quotes bound an
        interval over which that price held: quote N opens a segment, quote N+1
        closes it, and the newest stays open until ``now``. No bucketing, no
        interpolation -- the intervals are the ones AWS published.

        Equal consecutive prices are coalesced, because AWS does re-emit an
        unchanged price and two touching segments at one price are one segment.
        Coalescing here rather than in the store is deliberate: only the caller
        knows whether adjacent intervals are contiguous.

        Spot only. AWS publishes no on-demand price history, so backfilling one
        would mean inventing what last month's list price was.
        """
        now = datetime.now(UTC)
        catalog = self.catalog()
        segments: list[OfferingRecord] = []

        for region, quotes in self._per_region(now - timedelta(days=days)).items():
            series: dict[tuple[str, str], list[dict[str, Any]]] = {}
            for quote in quotes:
                key = (quote["InstanceType"], quote["AvailabilityZone"])
                series.setdefault(key, []).append(quote)

            for (instance_type, _zone), group in series.items():
                spec = catalog.get(instance_type)
                if spec is None:
                    continue
                segments.extend(self._segments_for_series(group, region, spec, now=now))
        return segments

    def _segments_for_series(
        self,
        quotes: list[dict[str, Any]],
        region: str,
        spec: InstanceSpec,
        *,
        now: datetime,
    ) -> Iterable[OfferingRecord]:
        """Turn one (type, zone) quote stream into closed segments."""
        ordered = sorted(quotes, key=lambda q: q["Timestamp"])

        # Keep only quotes where the price actually changed; each one is a boundary.
        boundaries: list[tuple[datetime, float]] = []
        for quote in ordered:
            price = float(quote["SpotPrice"])
            if price <= 0:
                continue
            if boundaries and _quantize(boundaries[-1][1]) == _quantize(price):
                continue
            boundaries.append((quote["Timestamp"], price))

        for index, (start, price) in enumerate(boundaries):
            end = boundaries[index + 1][0] if index + 1 < len(boundaries) else now
            yield OfferingRecord(
                offering=self._offering(
                    ordered[0],
                    region,
                    spec,
                    price=price,
                    # A historical segment was observed over its own interval; the
                    # wall clock is not when this price was true.
                    observed_at=end,
                ),
                first_seen=start,
                last_seen=end,
            )
