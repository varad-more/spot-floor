"""Normalized instance offering model.

Four conventions are load-bearing and deliberately chosen; read before extending.

**Price is per-instance, not per-unit.** ``price_usd_hr`` is the total hourly cost
of the whole instance, because that is the number a provider actually bills.
Per-GPU (:attr:`InstanceOffering.price_per_gpu_hr`) and per-vCPU are derived views
used for comparison. Storing a derived value instead would lose information and
invite rounding drift.

**Region and zone are separate fields.** AWS quotes spot prices per *availability
zone* (``us-east-1a``), and zones within one region routinely differ in price. A
region comparator therefore cannot key on a field that secretly holds an AZ, so
``region`` is the region and ``zone`` is the zone. Rolling up is a read-time
decision (see :func:`spotfloor.query.region_table`), and the roll-up always names
the zone that produced the number.

**Region is provider-native and NOT cross-comparable.** Vast reports
``"Japan, JP"``; AWS reports ``"us-east-1"``. There is no honest mapping between a
consumer marketplace's geolocation and a hyperscaler's region, so we do not invent
one. Filter regions within a provider, never across.

**GPU fields are optional.** Only ~5% of EC2's 1,354 instance types carry a GPU
(measured in us-east-1), so ``gpu_model``/``gpu_count`` describe a *property some
instances have* rather than the schema's spine. The spine is ``instance_type``,
which within a single provider is already canonical -- that is why cross-provider
SKU normalization (:mod:`spotfloor.gpu`) is enrichment here rather than the
grouping key it had to be when comparing Vast against AWS.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, computed_field


class PriceKind(StrEnum):
    """How the price is obtained, which determines how it can be lost.

    ``SPOT`` prices are preemptible: the capacity can be reclaimed. ``ON_DEMAND``
    is held until released. ``COMMUNITY`` is a peer-marketplace listing whose
    durability depends on the host rather than a provider SLA.
    """

    SPOT = "spot"
    ON_DEMAND = "on_demand"
    COMMUNITY = "community"


class Availability(StrEnum):
    """Whether the capacity can actually be obtained right now.

    ``UNKNOWN`` is a first-class value, not a failure mode. A provider that does
    not expose a truthful availability signal must report ``UNKNOWN`` rather than
    a fabricated one -- see the AWS provider for the canonical case.
    """

    AVAILABLE = "available"
    CONSTRAINED = "constrained"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


# Preference order when choosing the "best" offer for a config. Obtainability
# outranks price: a $1.20/hr node you cannot get is not a price, it is a mirage.
# This is the product thesis expressed as a sort key.
_OBTAINABILITY_RANK = {
    Availability.AVAILABLE: 0,
    Availability.CONSTRAINED: 1,
    Availability.UNKNOWN: 2,
    Availability.UNAVAILABLE: 3,
}


def obtainability_rank(availability: Availability) -> int:
    """Lower is more obtainable. Used to rank offers before price is considered."""
    return _OBTAINABILITY_RANK[availability]


class InstanceOffering(BaseModel):
    """A single observation of one rentable instance configuration at a point in time."""

    provider: str
    instance_type: str
    region: str
    # None where a provider does not subdivide a region. AWS always sets it,
    # because AWS prices spot per zone and the difference is the whole point.
    zone: str | None = None
    price_usd_hr: float = Field(gt=0, description="Total $/hr for the whole instance.")
    price_kind: PriceKind
    availability: Availability
    availability_score: float | None = Field(default=None, ge=0.0, le=1.0)
    observed_at: datetime

    # --- hardware spec, from the provider's own catalog API -------------------
    # Optional because most instance families have no GPU. `gpu_count == 0` with
    # `gpu_model is None` means "this instance has no GPU", which is a fact; None
    # for vcpus/memory means "the catalog did not tell us", which is ignorance.
    # Do not collapse those two into one sentinel.
    gpu_model: str | None = None
    gpu_count: int = Field(default=0, ge=0)
    vcpus: int | None = Field(default=None, gt=0)
    memory_gib: float | None = Field(default=None, gt=0)

    external_id: str | None = Field(
        default=None,
        description="Provider-stable id of the underlying machine (Vast machine_id). "
        "None where the provider sells fungible capacity rather than named hosts (AWS).",
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def price_per_gpu_hr(self) -> float | None:
        """Comparison axis across node sizes, for GPU instances only.

        ``None`` rather than falling back to ``price_usd_hr``: dividing by "1 GPU"
        on an instance with no GPU would silently invent a per-GPU price for a CPU
        box, and that number would then sort against real ones.
        """
        if not self.gpu_count:
            return None
        return self.price_usd_hr / self.gpu_count

    @property
    def price_per_vcpu_hr(self) -> float | None:
        """Secondary axis for comparing sizes within a family. None if vCPUs unknown."""
        if not self.vcpus:
            return None
        return self.price_usd_hr / self.vcpus

    @property
    def has_gpu(self) -> bool:
        return bool(self.gpu_count)

    @property
    def instance_family(self) -> str:
        """'m5.large' -> 'm5'; 'p6-b200.48xlarge' -> 'p6-b200'. Groups the UI."""
        return self.instance_type.split(".", 1)[0]

    @property
    def series_key(self) -> str:
        """Identity of the *thing being offered*, excluding price, availability and time.

        The store uses this to decide whether an observation continues an existing
        series or begins a new one, so it must be stable across polls and unique per
        sellable thing.

        ``zone`` is what makes it unique on AWS: ``m5.large`` in ``us-east-1a`` and
        in ``us-east-1d`` are separately priced products, and collapsing them would
        make one series appear to thrash between zone prices on every poll.

        ``external_id`` plays the same role on a marketplace. Vast sells named
        physical hosts, and dozens of distinct machines share a ``gpu_model`` and a
        ``region`` -- without the machine id they would collapse into one series that
        appears to thrash its price, which would both destroy dedup and fire alerts
        on phantom price changes.

        ``price_kind`` is part of the identity because an instance's on-demand and
        interruptible listings are different products with different durability; they
        are separate series, not two states of one series.
        """
        return "|".join(
            (
                self.provider,
                self.external_id or "-",
                self.instance_type,
                self.region,
                self.zone or "-",
                str(self.price_kind),
            )
        )
