"""Read model: turning stored segments into the things a UI shows.

Pure functions over :class:`OfferingRecord` -- no I/O, no clock, no SQL -- for the
same reason ``step()`` is pure: the interesting claims ("this is the cheapest zone",
"the price was not observed in this window") become assertable without a database or
an HTTP client.

Three rules from the model layer survive into every aggregate here, because a UI is
exactly where they get quietly broken:

**A region row names the zone that produced its number.** AWS prices spot per
availability zone, and zones inside one region differ -- routinely by a few percent,
sometimes far more. Reporting a bare regional minimum would be a number you cannot
act on, because you must launch into a *zone*. So every row carries
``cheapest_zone``, and it also carries the intra-region spread, which is the
evidence that the roll-up was lossy in the first place.

**Absence is not a value.** A bucket with no observation is ``None``, not zero and
not a carried-forward price. A chart that interpolates across a gap is asserting we
observed something we did not.

**Volatility is not availability.** ``price_changes`` and ``coefficient_of_variation``
are real facts computed from published history, and they are a fair *proxy* for how
contended a zone is. They are not a fulfillment signal, and nothing here may present
them as one -- AWS availability stays ``unknown``.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Mapping, Sequence

from spotfloor.models import Availability, InstanceOffering, PriceKind, obtainability_rank
from spotfloor.storage.base import OfferingRecord, TimeRange


@dataclass(frozen=True, slots=True)
class ZonePrice:
    """One zone's current price within a region."""

    zone: str
    price_usd_hr: float
    availability: Availability


@dataclass(frozen=True, slots=True)
class RegionRow:
    """The cheapest current price for one (instance_type, region), zone named.

    ``spread_pct`` is what justifies the roll-up being honest: it states how much
    was lost by collapsing the zones. A row with a 40% spread is telling you the
    regional number is nearly meaningless on its own.
    """

    instance_type: str
    instance_family: str
    region: str
    price_kind: PriceKind

    cheapest_usd_hr: float
    cheapest_zone: str
    dearest_usd_hr: float
    dearest_zone: str
    zones: tuple[ZonePrice, ...]

    availability: Availability
    counts: Mapping[Availability, int]
    last_observed_at: datetime

    # Hardware spec, carried so the UI can filter and show $/GPU or $/vCPU.
    vcpus: int | None = None
    memory_gib: float | None = None
    gpu_model: str | None = None
    gpu_count: int = 0

    # Volatility over the history window. None when history was not supplied.
    price_changes: int | None = None
    coefficient_of_variation: float | None = None

    # The region's on-demand list price, when one was observed. None means "not
    # observed" -- an IAM policy without pricing:GetProducts, or a region AWS
    # quotes in a currency other than USD. It never means "on-demand is free".
    on_demand_usd_hr: float | None = None

    @property
    def zone_count(self) -> int:
        return len(self.zones)

    @property
    def savings_pct(self) -> float | None:
        """How much cheaper the cheapest zone is than paying on-demand.

        ``None`` rather than 0 when there is no on-demand price to compare against,
        because "we could not ask" and "spot saves you nothing" are different claims
        and a 0 in this column would assert the second one.

        Computed against ``cheapest_usd_hr``, so it is the saving on the zone the row
        actually tells you to launch into -- not against a regional average nobody
        can buy. Spot above list price is a real market state, so the figure is
        allowed to go negative rather than being clamped at zero.
        """
        if self.on_demand_usd_hr is None or self.on_demand_usd_hr <= 0:
            return None
        if self.price_kind is PriceKind.ON_DEMAND:
            return None
        return (1 - self.cheapest_usd_hr / self.on_demand_usd_hr) * 100

    @property
    def spread_pct(self) -> float:
        """How much dearer the worst zone is than the best, as a percentage."""
        if self.cheapest_usd_hr <= 0:
            return 0.0
        return (self.dearest_usd_hr / self.cheapest_usd_hr - 1) * 100

    @property
    def cheapest_per_gpu_hr(self) -> float | None:
        if not self.gpu_count:
            return None
        return self.cheapest_usd_hr / self.gpu_count

    @property
    def cheapest_per_vcpu_hr(self) -> float | None:
        if not self.vcpus:
            return None
        return self.cheapest_usd_hr / self.vcpus

    @property
    def availability_known(self) -> bool:
        """False when the provider cannot tell us anything -- the AWS case, always.

        The UI must render this as an explicit 'unknown', never as a blank cell or
        a zero, both of which read as 'none available'.
        """
        return any(a is not Availability.UNKNOWN for a in self.counts)


def region_table(
    records: Iterable[OfferingRecord],
    *,
    history: Mapping[tuple[str, str], Sequence[OfferingRecord]] | None = None,
) -> list[RegionRow]:
    """One row per (instance_type, region), cheapest zone named.

    Ordered by instance type then price, so the same hardware across regions lands
    adjacent -- which is the comparison this tool exists to make. Unlike the
    cross-provider case, no SKU normalization is needed to justify it: within one
    provider ``m5.large`` means exactly one thing everywhere.

    ``history`` is keyed by ``(instance_type, region)`` and drives the volatility
    columns. Omit it and those stay ``None``, which the UI must render as "not
    computed" rather than as zero changes.

    **On-demand is a column here, not a row.** It is stored as its own series --
    different product, different durability, its own history -- but the question a
    reader has is "how much does spot save me", and that is answered by putting the
    two prices side by side rather than by doubling the table's height. An
    on-demand price with no spot counterpart still gets its own row, because
    dropping it would hide the only price observed for that pair.
    """
    groups: dict[tuple[str, str, PriceKind], list[OfferingRecord]] = {}
    for record in records:
        offering = record.offering
        key = (offering.instance_type, offering.region, offering.price_kind)
        groups.setdefault(key, []).append(record)

    # AWS charges one on-demand rate per region, so there is nothing to roll up --
    # but `min` keeps this total in the face of a duplicate rather than picking
    # arbitrarily.
    on_demand = {
        (instance_type, region): min(r.offering.price_usd_hr for r in members)
        for (instance_type, region, kind), members in groups.items()
        if kind is PriceKind.ON_DEMAND
    }

    rows = [
        _region_row(
            key, members, history=history, on_demand_usd_hr=on_demand.get(key[:2])
        )
        for key, members in groups.items()
        if key[2] is not PriceKind.ON_DEMAND or _only_kind_for_pair(key, groups)
    ]
    rows.sort(key=lambda r: (r.instance_type, r.cheapest_usd_hr))
    return rows


def _only_kind_for_pair(
    key: tuple[str, str, PriceKind],
    groups: Mapping[tuple[str, str, PriceKind], Sequence[OfferingRecord]],
) -> bool:
    """True when no other price kind was observed for this (instance_type, region)."""
    instance_type, region, kind = key
    return not any(
        (instance_type, region, other) in groups for other in PriceKind if other is not kind
    )


def _region_row(
    key: tuple[str, str, PriceKind],
    members: list[OfferingRecord],
    *,
    history: Mapping[tuple[str, str], Sequence[OfferingRecord]] | None,
    on_demand_usd_hr: float | None = None,
) -> RegionRow:
    instance_type, region, price_kind = key

    zones = tuple(
        sorted(
            (
                ZonePrice(
                    # A provider without zones (Vast) collapses to the region name
                    # rather than an empty string, so the column is never blank.
                    zone=r.offering.zone or r.offering.region,
                    price_usd_hr=r.offering.price_usd_hr,
                    availability=r.offering.availability,
                )
                for r in members
            ),
            key=lambda z: z.price_usd_hr,
        )
    )

    # Obtainability outranks price -- the sort key from models.py applied to a group.
    # On AWS every row is UNKNOWN so this reduces to price, but it must stay: the
    # ordering is the product thesis, and a provider that *can* report availability
    # has to keep winning on it.
    best = min(
        members,
        key=lambda r: (
            obtainability_rank(r.offering.availability),
            r.offering.price_usd_hr,
        ),
    )
    spec: InstanceOffering = best.offering

    changes = cov = None
    if history is not None:
        segments = history.get((instance_type, region), ())
        changes, cov = volatility(segments)

    return RegionRow(
        instance_type=instance_type,
        instance_family=spec.instance_family,
        region=region,
        price_kind=price_kind,
        cheapest_usd_hr=zones[0].price_usd_hr,
        cheapest_zone=zones[0].zone,
        dearest_usd_hr=zones[-1].price_usd_hr,
        dearest_zone=zones[-1].zone,
        zones=zones,
        availability=best.offering.availability,
        counts=Counter(r.offering.availability for r in members),
        last_observed_at=max(r.last_seen for r in members),
        vcpus=spec.vcpus,
        memory_gib=spec.memory_gib,
        gpu_model=spec.gpu_model,
        gpu_count=spec.gpu_count,
        price_changes=changes,
        coefficient_of_variation=cov,
        on_demand_usd_hr=on_demand_usd_hr,
    )


def volatility(segments: Sequence[OfferingRecord]) -> tuple[int | None, float | None]:
    """Price-change count and coefficient of variation over the supplied segments.

    Both are facts about published price history, and together they are a fair proxy
    for how contended a zone is: a price that moved 40 times in a month is being
    bid against. **Neither is an availability signal** -- AWS still cannot tell us
    whether you would get the instance, and this must never be relabelled as if it
    could.

    The count is ``len(segments) - 1`` because a segment *is* a price that held: N
    segments have N-1 transitions between them. Returns ``(None, None)`` for an
    empty history, which is "not computed", not "never changed".

    Coefficient of variation (stdev / mean) rather than raw stdev, so a $20/hr GPU
    box and a $0.02/hr burstable are comparable on the same column.
    """
    if not segments:
        return None, None

    prices = [s.offering.price_usd_hr for s in segments]
    changes = len(segments) - 1

    if len(prices) < 2:
        return changes, 0.0

    mean = sum(prices) / len(prices)
    if mean <= 0:
        return changes, None
    variance = sum((p - mean) ** 2 for p in prices) / (len(prices) - 1)
    return changes, math.sqrt(variance) / mean


@dataclass(frozen=True, slots=True)
class FloorPoint:
    """The floor price over one time bucket. ``None`` means *not observed*."""

    at: datetime
    floor_usd_hr: float | None


def floor_series(
    records: Sequence[OfferingRecord],
    time_range: TimeRange,
    *,
    buckets: int = 48,
) -> list[FloorPoint]:
    """Cheapest observed $/hr per time bucket across the range.

    Segments carry an interval, so a bucket is an *overlap* test rather than a point
    sample: a price that held for six hours contributes to every bucket it spans,
    without being resampled or duplicated. That is what makes the chart a reading of
    the stored data rather than a reconstruction of it.

    Buckets with no overlapping segment are ``None`` and must be rendered as a gap.
    Carrying the last price forward would invent an observation.
    """
    if buckets <= 0:
        raise ValueError("buckets must be positive")

    span = (time_range.end - time_range.start).total_seconds()
    if span <= 0:
        raise ValueError("time_range must be non-empty")
    width = timedelta(seconds=span / buckets)

    series: list[FloorPoint] = []
    for index in range(buckets):
        start = time_range.start + width * index
        end = start + width
        prices = [
            r.offering.price_usd_hr
            for r in records
            if r.first_seen < end and r.last_seen >= start
        ]
        series.append(FloorPoint(at=start, floor_usd_hr=min(prices) if prices else None))
    return series
