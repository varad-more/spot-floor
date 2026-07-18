"""Normalized cross-provider GPU offering model.

Two conventions are load-bearing and deliberately chosen; read before extending.

**Price is per-node, not per-GPU.** ``price_usd_hr`` is the total hourly cost of
the whole node, because that is the number a provider actually bills. Per-GPU is
a derived view (:attr:`GpuOffering.price_per_gpu_hr`) used for comparison. Storing
the derived value instead would lose information and invite rounding drift.

**Region is provider-native and NOT cross-comparable.** Vast reports
``"Japan, JP"``; AWS reports ``"us-east-1"``. There is no honest mapping between a
consumer marketplace's geolocation and a hyperscaler's region, so we do not invent
one. Filter regions within a provider, never across.
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


class GpuOffering(BaseModel):
    """A single observation of one rentable GPU configuration at a point in time."""

    provider: str
    instance_type: str
    gpu_model: str
    gpu_count: int = Field(gt=0)
    region: str
    price_usd_hr: float = Field(gt=0, description="Total $/hr for the whole node.")
    price_kind: PriceKind
    availability: Availability
    availability_score: float | None = Field(default=None, ge=0.0, le=1.0)
    observed_at: datetime
    external_id: str | None = Field(
        default=None,
        description="Provider-stable id of the underlying machine (Vast machine_id). "
        "None where the provider sells fungible capacity rather than named hosts (AWS).",
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def price_per_gpu_hr(self) -> float:
        """Comparison axis across providers and node sizes."""
        return self.price_usd_hr / self.gpu_count

    @property
    def series_key(self) -> str:
        """Identity of the *thing being offered*, excluding price, availability and time.

        The store uses this to decide whether an observation continues an existing
        series or begins a new one, so it must be stable across polls and unique per
        sellable thing.

        ``external_id`` is what makes it unique on a marketplace. Vast sells named
        physical hosts, and dozens of distinct machines share a ``gpu_model`` and a
        ``region`` -- without the machine id they would collapse into one series that
        appears to thrash its price on every poll, which would both destroy dedup and
        fire alerts on phantom price changes. AWS sells fungible capacity, so
        ``(instance_type, region)`` is already unique and ``external_id`` is None.

        ``price_kind`` is part of the identity because a node's on-demand and
        interruptible listings are different products with different durability; they
        are separate series, not two states of one series.
        """
        return "|".join(
            (
                self.provider,
                self.external_id or "-",
                self.instance_type,
                self.gpu_model,
                str(self.gpu_count),
                self.region,
                str(self.price_kind),
            )
        )
