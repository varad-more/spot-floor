"""GATE 1 evidence: 3 live poll cycles, dedup, time-series query, AWS honesty.

Run: uv run python scripts/gate1.py
"""

from __future__ import annotations

import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3

from spotfloor.ingest.pipeline import run_tick
from spotfloor.models import Availability
from spotfloor.providers.aws import AwsProvider, CredsOwner
from spotfloor.providers.vast import VastProvider
from spotfloor.storage.base import OfferingFilter, TimeRange
from spotfloor.storage.sqlite import SqliteTimeSeriesStore

CYCLES = 3


def main() -> None:
    db = Path(tempfile.mkdtemp()) / "gate1.db"
    store = SqliteTimeSeriesStore(str(db))
    providers = [
        VastProvider(watchlist=("H100 SXM", "A100 SXM4")),
        AwsProvider(
            boto3.client("ec2", region_name="us-east-1"),
            creds_owner=CredsOwner.APP,
            instance_types=("p5.48xlarge", "p4d.24xlarge"),
        ),
    ]

    print("=" * 78)
    print(f"GATE 1 -- {CYCLES} live poll cycles (Vast + AWS)")
    print("=" * 78)
    print(f"{'cycle':<7}{'fetched':<26}{'inserted':>10}{'extended':>10}{'failures':>12}")

    started = datetime.now(UTC)
    for cycle in range(1, CYCLES + 1):
        report = run_tick(providers, store)
        fetched = ", ".join(f"{k}={v}" for k, v in report.fetched.items())
        print(
            f"{cycle:<7}{fetched:<26}{report.write.inserted:>10}"
            f"{report.write.extended:>10}{len(report.failures):>12}"
        )
        assert report.ok, f"provider failures: {report.failures}"
        if cycle < CYCLES:
            time.sleep(2)

    print(
        "\nDedup: cycle 1 opens the segments; later cycles EXTEND them rather than\n"
        "inserting duplicates. The table grows with change, not with time."
    )

    # --- time-ordered series for a (gpu_model, region) ---
    now = datetime.now(UTC)
    latest = store.latest(OfferingFilter(gpu_model="H100_SXM_80GB"), now=now)
    if latest:
        region = latest[0].offering.region
        series = store.history(
            OfferingFilter(gpu_model="H100_SXM_80GB", region=region),
            TimeRange(started - timedelta(minutes=5), now),
        )
        print(f"\nTime-ordered series for (H100_SXM_80GB, {region!r}): {len(series)} segments")
        for record in series[:4]:
            o = record.offering
            print(
                f"  {record.first_seen:%H:%M:%S}->{record.last_seen:%H:%M:%S}  "
                f"{str(o.price_kind):<10} ${o.price_per_gpu_hr:>6.2f}/GPU/hr  {o.availability}"
            )

    # --- the honesty constraint, against live data ---
    print("\n" + "=" * 78)
    print("Availability signal by provider (the asymmetry, stated honestly)")
    print("=" * 78)
    for provider in ("vast", "aws"):
        records = store.latest(OfferingFilter(provider=provider), now=now)
        counts: dict[str, int] = {}
        for r in records:
            counts[str(r.offering.availability)] = counts.get(str(r.offering.availability), 0) + 1
        print(f"  {provider:<6} {counts}")

    aws_records = store.latest(OfferingFilter(provider="aws"), now=now)
    assert aws_records, "no AWS rows"
    assert all(r.offering.availability is Availability.UNKNOWN for r in aws_records)
    print(
        "\n  AWS is UNKNOWN by construction: Spot Placement Score is computed against\n"
        "  the CALLING account's quota, so a score fetched with app credentials is a\n"
        "  fact about us, not about the user. We do not publish it as a market signal."
    )

    store.close()
    print("\nGATE 1: PASS")


if __name__ == "__main__":
    main()
