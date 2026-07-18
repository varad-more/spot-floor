"""AWS provider -- pricing-first, and honest about what it cannot know.

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

This asymmetry is the whole reason Vast is the availability showcase and AWS is
the pricing case, and it must be stated in the UI wherever AWS availability would
otherwise render as blank.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from spotfloor.gpu import canonical_gpu_model
from spotfloor.models import Availability, GpuOffering, PriceKind

logger = logging.getLogger(__name__)

# describe_spot_price_history is a *history* API: it returns a stream of past
# quotes, so we pull a short window and reduce to the newest row per (type, AZ).
_HISTORY_WINDOW = timedelta(hours=3)

# Spot Placement Score is 1..10. Thresholds are deliberately conservative: AWS
# states the score is a likelihood, never a guarantee of capacity.
_SPS_AVAILABLE = 8
_SPS_CONSTRAINED = 4


class CredsOwner(StrEnum):
    """Whose AWS account the credentials belong to.

    This is not a config knob so much as a truth claim: it decides whether an
    availability signal is *about the user* or about us.
    """

    APP = "app"
    USER = "user"


def _instance_family(instance_type: str) -> str:
    """'p5.48xlarge' -> 'p5'; 'p6-b200.48xlarge' -> 'p6-b200'."""
    return instance_type.split(".", 1)[0]


class AwsProvider:
    """EC2 spot pricing via official APIs. Availability is UNKNOWN unless user-owned."""

    name = "aws"

    def __init__(
        self,
        ec2_client: Any,
        *,
        creds_owner: CredsOwner = CredsOwner.APP,
        instance_types: tuple[str, ...] | None = None,
    ) -> None:
        self._ec2 = ec2_client
        self._creds_owner = creds_owner
        self._instance_types = instance_types
        self._catalog: dict[str, tuple[str, int]] | None = None

    def gpu_catalog(self) -> dict[str, tuple[str, int]]:
        """Map instance type -> (canonical gpu model, gpu count), from the official API.

        ``DescribeInstanceTypes`` is authoritative about GPU name, count and VRAM, so
        the mapping is derived rather than hand-maintained -- a new GPU instance
        family shows up on its own instead of silently going missing from a static
        table. AWS omits the interconnect from the GPU name ("H100"), so the instance
        family supplies it (``p5`` -> SXM); see :func:`canonical_gpu_model`.
        """
        if self._catalog is not None:
            return self._catalog

        catalog: dict[str, tuple[str, int]] = {}
        paginator = self._ec2.get_paginator("describe_instance_types")
        for page in paginator.paginate(
            Filters=[{"Name": "instance-type", "Values": ["p*", "g*"]}]
        ):
            for spec in page["InstanceTypes"]:
                gpu_info = spec.get("GpuInfo")
                if not gpu_info:
                    continue
                gpu = gpu_info["Gpus"][0]
                if gpu.get("Manufacturer") != "NVIDIA":
                    continue
                instance_type = spec["InstanceType"]
                catalog[instance_type] = (
                    canonical_gpu_model(
                        gpu["Name"],
                        gpu.get("MemoryInfo", {}).get("SizeInMiB"),
                        aws_instance_family=_instance_family(instance_type),
                    ),
                    gpu["Count"],
                )

        self._catalog = catalog
        return catalog

    def _availability(self, instance_type: str) -> tuple[Availability, float | None]:
        """Availability, or an honest admission that we cannot know it.

        With app credentials this returns UNKNOWN *without calling* the
        placement-score API, because a score computed against our account would be a
        statement about our quota, not about the user's odds of getting capacity.
        """
        if self._creds_owner is CredsOwner.APP:
            return Availability.UNKNOWN, None

        # User-owned credentials: the score is now genuinely about them.
        try:
            response = self._ec2.get_spot_placement_scores(
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

    def fetch(self) -> list[GpuOffering]:
        """Return the most recent spot quote per (instance type, availability zone)."""
        observed_at = datetime.now(UTC)
        catalog = self.gpu_catalog()
        instance_types = list(self._instance_types or catalog)

        # Reduce the history stream to the newest quote per (type, AZ).
        newest: dict[tuple[str, str], dict[str, Any]] = {}
        paginator = self._ec2.get_paginator("describe_spot_price_history")
        for page in paginator.paginate(
            InstanceTypes=instance_types,
            ProductDescriptions=["Linux/UNIX"],
            StartTime=observed_at - _HISTORY_WINDOW,
        ):
            for quote in page["SpotPriceHistory"]:
                key = (quote["InstanceType"], quote["AvailabilityZone"])
                incumbent = newest.get(key)
                if incumbent is None or quote["Timestamp"] > incumbent["Timestamp"]:
                    newest[key] = quote

        offerings: list[GpuOffering] = []
        for (instance_type, az), quote in newest.items():
            spec = catalog.get(instance_type)
            if spec is None:
                # Unknown silicon is dropped, never guessed at.
                logger.warning("aws: %s is not in the GPU catalog; dropping", instance_type)
                continue

            gpu_model, gpu_count = spec
            availability, score = self._availability(instance_type)
            offerings.append(
                GpuOffering(
                    provider=self.name,
                    instance_type=instance_type,
                    gpu_model=gpu_model,
                    gpu_count=gpu_count,
                    # The AZ verbatim. Provider-native: 'us-east-1a' is not
                    # comparable to Vast's 'Japan, JP' and we do not pretend it is.
                    region=az,
                    price_usd_hr=float(quote["SpotPrice"]),
                    price_kind=PriceKind.SPOT,
                    availability=availability,
                    availability_score=score,
                    observed_at=observed_at,
                )
            )
        return offerings
