"""GATE 1 (honesty): AWS must report UNKNOWN availability under app credentials.

The load-bearing test here is
:func:`test_app_creds_never_call_the_placement_score_api` -- an assertion, not a
comment. The Spot Placement Score API returns a number computed against the
*calling account's* quota and history, so a score fetched with our credentials
describes our account, not the user's odds of getting capacity. Shipping it as a
market signal would be fabrication, so the code must not even ask.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from spotfloor.models import Availability, PriceKind
from spotfloor.providers.aws import AwsProvider, CredsOwner

NOW = datetime.now(UTC)

INSTANCE_TYPES = {
    "InstanceTypes": [
        {
            "InstanceType": "p5.48xlarge",
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
    ]
}

# A history stream: several quotes per (type, AZ). Only the newest may survive.
PRICE_HISTORY = {
    "SpotPriceHistory": [
        {
            "InstanceType": "p5.48xlarge",
            "AvailabilityZone": "us-east-1b",
            "SpotPrice": "20.2547",
            "Timestamp": NOW,
        },
        {
            "InstanceType": "p5.48xlarge",
            "AvailabilityZone": "us-east-1b",
            "SpotPrice": "31.9999",
            "Timestamp": NOW - timedelta(hours=2),
        },
        {
            "InstanceType": "p5.48xlarge",
            "AvailabilityZone": "us-east-1f",
            "SpotPrice": "19.3147",
            "Timestamp": NOW - timedelta(hours=1),
        },
    ]
}


def fake_ec2(**overrides) -> MagicMock:
    ec2 = MagicMock()

    def paginator(operation: str) -> MagicMock:
        p = MagicMock()
        pages = {
            "describe_instance_types": [INSTANCE_TYPES],
            "describe_spot_price_history": [PRICE_HISTORY],
        }[operation]
        p.paginate.return_value = pages
        return p

    ec2.get_paginator.side_effect = paginator
    for name, value in overrides.items():
        getattr(ec2, name).return_value = value
    return ec2


def test_app_creds_never_call_the_placement_score_api() -> None:
    """THE HONESTY GATE. A score from our account is not a fact about the user."""
    ec2 = fake_ec2()
    offerings = AwsProvider(ec2, creds_owner=CredsOwner.APP).fetch()

    ec2.get_spot_placement_scores.assert_not_called()

    assert offerings
    for o in offerings:
        assert o.availability is Availability.UNKNOWN, "AWS fabricated an availability signal"
        assert o.availability_score is None


def test_gpu_catalog_is_derived_from_the_official_api() -> None:
    """p5 is 8x H100 SXM per DescribeInstanceTypes -- not per a hand-maintained table."""
    catalog = AwsProvider(fake_ec2()).gpu_catalog()
    assert catalog["p5.48xlarge"] == ("H100_SXM_80GB", 8)
    # AWS reports a 24GB L4 as 22888 MiB; bucketing must recover the real SKU.
    assert catalog["g6.12xlarge"] == ("L4_24GB", 4)


def test_only_the_most_recent_quote_per_az_survives() -> None:
    """describe_spot_price_history is a history stream; the naive read is stale."""
    offerings = AwsProvider(fake_ec2()).fetch()

    by_az = {o.region: o for o in offerings}
    assert set(by_az) == {"us-east-1b", "us-east-1f"}
    assert by_az["us-east-1b"].price_usd_hr == 20.2547, "returned a stale quote"

    p5 = by_az["us-east-1b"]
    assert p5.price_kind is PriceKind.SPOT
    assert p5.gpu_count == 8
    assert p5.price_per_gpu_hr == pytest.approx(2.5318, abs=1e-4)
    # The AZ verbatim. It is not mapped onto Vast's geolocations.
    assert p5.region == "us-east-1b"


def test_user_creds_may_use_placement_scores() -> None:
    """With the user's own credentials the score is genuinely about them, so it counts.

    A score of 1/10 -- what p5.48xlarge really returned live -- means unavailable.
    """
    ec2 = fake_ec2(
        get_spot_placement_scores={
            "SpotPlacementScores": [{"Region": "us-east-1", "Score": 1}]
        }
    )
    offerings = AwsProvider(ec2, creds_owner=CredsOwner.USER).fetch()

    ec2.get_spot_placement_scores.assert_called()
    assert all(o.availability is Availability.UNAVAILABLE for o in offerings)
    assert all(o.availability_score == 0.1 for o in offerings)


def test_unknown_instance_types_are_dropped_not_guessed() -> None:
    ec2 = fake_ec2()
    provider = AwsProvider(ec2)
    provider._catalog = {}  # simulate an instance type missing from the catalog
    assert provider.fetch() == []


@pytest.mark.live
def test_gate_1_live_aws_is_unknown_without_user_creds() -> None:
    """GATE 1: against the real AWS API, availability is UNKNOWN and priced sanely."""
    boto3 = pytest.importorskip("boto3")
    ec2 = boto3.client("ec2", region_name="us-east-1")

    offerings = AwsProvider(
        ec2, creds_owner=CredsOwner.APP, instance_types=("p5.48xlarge", "p4d.24xlarge")
    ).fetch()

    assert offerings, "AWS returned no spot quotes"
    for o in offerings:
        assert o.availability is Availability.UNKNOWN
        assert o.availability_score is None
        assert o.price_kind is PriceKind.SPOT
        assert 0 < o.price_per_gpu_hr < 100
        assert o.gpu_count == 8
        assert o.gpu_model in {"H100_SXM_80GB", "A100_SXM4_40GB"}
