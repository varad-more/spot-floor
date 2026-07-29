"""Read model: turning stored segments into the two things a UI shows.

Pure functions over :class:`OfferingRecord` -- no I/O, no clock, no SQL -- for the
same reason ``step()`` is pure: the interesting claims ("this is the cheapest
obtainable node", "the floor was not observed in this window") become assertable
without a database or an HTTP client.

Two rules from the model layer survive into every aggregate here, because a UI is
exactly where they get quietly broken:

**Rows are grouped per provider, never merged across providers.** GPU models are
normalized, so comparing an AWS ``H100_SXM_80GB`` price to a Vast one is honest.
Regions are *not* normalized, so a row never spans providers and a region is never
compared across them.

**Absence is not a value.** A bucket with no observation is ``None``, not zero and
not a carried-forward price. A chart that interpolates across a gap is asserting we
observed something we did not.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Mapping, Sequence

from spotfloor.models import Availability, PriceKind, obtainability_rank
from spotfloor.storage.base import OfferingRecord, TimeRange

# "Obtainable" means confirmed gettable right now. UNKNOWN is deliberately absent:
# it is an admission of ignorance, not a weak yes, so AWS never counts as supply.
OBTAINABLE: tuple[Availability, ...] = (Availability.AVAILABLE, Availability.CONSTRAINED)


@dataclass(frozen=True, slots=True)
class MarketRow:
    """The cheapest offer for one (gpu_model, provider, price_kind), plus supply depth.

    Both price fields are carried because they answer different questions and the
    difference between them is itself the product's point: ``cheapest_per_gpu_hr``
    is the headline number a price tracker shows, and
    ``cheapest_obtainable_per_gpu_hr`` is what you could actually rent. When the
    second is None the first is a price you cannot buy at.
    """

    gpu_model: str
    provider: str
    price_kind: PriceKind
    cheapest_per_gpu_hr: float
    cheapest_price_usd_hr: float
    cheapest_gpu_count: int
    cheapest_region: str
    cheapest_availability: Availability
    cheapest_obtainable_per_gpu_hr: float | None
    counts: Mapping[Availability, int]
    last_observed_at: datetime

    @property
    def node_count(self) -> int:
        return sum(self.counts.values())

    @property
    def obtainable_nodes(self) -> int:
        return sum(self.counts.get(a, 0) for a in OBTAINABLE)

    @property
    def availability_known(self) -> bool:
        """False when the provider cannot tell us anything -- the AWS case.

        The UI must render this as an explicit 'unknown', never as a blank cell or
        a zero, both of which read as 'none available'.
        """
        return any(a is not Availability.UNKNOWN for a in self.counts)


def market_table(records: Iterable[OfferingRecord]) -> list[MarketRow]:
    """Collapse per-machine segments into one row per (gpu_model, provider, kind).

    Ordered by GPU model, then by price, so the same silicon from different
    providers lands adjacent -- which is the only cross-provider comparison the
    data supports.
    """
    groups: dict[tuple[str, str, PriceKind], list[OfferingRecord]] = {}
    for record in records:
        offering = record.offering
        key = (offering.gpu_model, offering.provider, offering.price_kind)
        groups.setdefault(key, []).append(record)

    rows = [_row(key, members) for key, members in groups.items()]
    rows.sort(key=lambda r: (r.gpu_model, r.cheapest_per_gpu_hr))
    return rows


def _row(key: tuple[str, str, PriceKind], members: list[OfferingRecord]) -> MarketRow:
    gpu_model, provider, price_kind = key

    # Obtainability outranks price -- the sort key from models.py, applied to a
    # whole group. The cheapest *listed* node is a footnote if you cannot get it.
    best = min(
        members,
        key=lambda r: (
            obtainability_rank(r.offering.availability),
            r.offering.price_per_gpu_hr,
        ),
    )
    obtainable = [r for r in members if r.offering.availability in OBTAINABLE]

    return MarketRow(
        gpu_model=gpu_model,
        provider=provider,
        price_kind=price_kind,
        cheapest_per_gpu_hr=min(r.offering.price_per_gpu_hr for r in members),
        cheapest_price_usd_hr=best.offering.price_usd_hr,
        cheapest_gpu_count=best.offering.gpu_count,
        cheapest_region=best.offering.region,
        cheapest_availability=best.offering.availability,
        cheapest_obtainable_per_gpu_hr=(
            min(r.offering.price_per_gpu_hr for r in obtainable) if obtainable else None
        ),
        counts=Counter(r.offering.availability for r in members),
        last_observed_at=max(r.last_seen for r in members),
    )


@dataclass(frozen=True, slots=True)
class FloorPoint:
    """The floor price over one time bucket. ``None`` means *not observed*."""

    at: datetime
    floor_per_gpu_hr: float | None


def floor_series(
    records: Sequence[OfferingRecord],
    time_range: TimeRange,
    *,
    buckets: int = 48,
) -> list[FloorPoint]:
    """Cheapest observed $/GPU/hr per time bucket across the range.

    Segments carry an interval, so a bucket is an *overlap* test rather than a
    point sample: a price that held for six hours contributes to every bucket it
    spans, without being resampled or duplicated. That is what makes the chart a
    reading of the stored data rather than a reconstruction of it.

    Buckets with no overlapping segment are ``None`` and must be rendered as a
    gap. Carrying the last price forward would invent an observation.
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
            r.offering.price_per_gpu_hr
            for r in records
            if r.first_seen < end and r.last_seen >= start
        ]
        series.append(
            FloorPoint(at=start, floor_per_gpu_hr=min(prices) if prices else None)
        )
    return series
