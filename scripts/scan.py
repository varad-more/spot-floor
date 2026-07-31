"""Scan AWS spot prices once and exit. No server involved.

    uv run python scripts/scan.py                          # everything, current prices
    uv run python scripts/scan.py --types m5.large,c5.large
    uv run python scripts/scan.py --regions us-east-1,us-west-2
    uv run python scripts/scan.py --backfill --days 30     # deep history
    uv run python scripts/scan.py --show                   # print what is stored

Use this to refresh on demand, or from cron if you want your own schedule rather
than the poller's. It writes to the same database the dashboard reads, so a scan
here shows up on the next page load.

**Narrowing is about rate limits and time, not money.** ``DescribeSpotPriceHistory``,
``DescribeInstanceTypes`` and ``DescribeRegions`` are EC2 management-plane reads and
AWS does not bill per request -- a full 17-region scan costs nothing. What it spends
is request quota, and throttles are applied per region, so ``--regions
us-east-1,us-west-2`` finishes in a fraction of the time and leaves the rest of your
quota untouched.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from spotfloor.ingest.pipeline import run_tick
from spotfloor.query import region_table
from spotfloor.storage.base import OfferingFilter, TimeRange
from spotfloor.storage.sqlite import SqliteTimeSeriesStore
from spotfloor.web.app import WebConfig, build_providers

logger = logging.getLogger("scan")


def csv(value: str | None) -> tuple[str, ...] | None:
    if not value:
        return None
    parts = tuple(p.strip() for p in value.split(",") if p.strip())
    return parts or None


def show(
    store: SqliteTimeSeriesStore,
    config: WebConfig,
    limit: int,
    *,
    scope_types: tuple[str, ...] | None = None,
    scope_regions: tuple[str, ...] | None = None,
) -> None:
    """Print current rows the same way the dashboard groups them.

    Scoped to what was just scanned when a scope was given. Printing all 646 stored
    rows after a two-type scan is technically "what is stored" and reads as a bug --
    you ask for m5.large in two regions and the first thing on screen is c5.2xlarge
    in sa-east-1.
    """
    now = datetime.now(UTC)
    window = TimeRange(now - timedelta(days=config.history_days), now)

    grouped: dict[tuple[str, str], list] = {}
    for record in store.history(OfferingFilter(), window):
        key = (record.offering.instance_type, record.offering.region)
        grouped.setdefault(key, []).append(record)

    rows = region_table(store.latest(OfferingFilter(), now=now), history=grouped)
    if scope_types:
        rows = [r for r in rows if r.instance_type in set(scope_types)]
    if scope_regions:
        rows = [r for r in rows if r.region in set(scope_regions)]

    if not rows:
        print("  nothing stored for that scope yet -- run a scan first")
        return

    print(
        f"\n  {'instance':<15}{'region':<17}{'$/hr':>10}{'zone':>18}"
        f"{'spread':>9}{'moves':>7}"
    )
    print("  " + "-" * 76)
    for row in rows[:limit]:
        spread = f"+{row.spread_pct:.0f}%" if row.zone_count > 1 else "-"
        moves = "n/a" if row.price_changes is None else str(row.price_changes)
        print(
            f"  {row.instance_type:<15}{row.region:<17}"
            f"{row.cheapest_usd_hr:>10.4f}{row.cheapest_zone:>18}"
            f"{spread:>9}{moves:>7}"
        )
    if len(rows) > limit:
        print(f"  ... and {len(rows) - limit} more ({len(rows)} rows total)")

    print(
        f"\n  availability is 'unknown' on every row -- AWS does not publish it.\n"
        f"  'moves' counts price changes over {config.history_days}d: a contention\n"
        f"  hint, NOT a chance of getting the instance."
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scan AWS spot prices once. Describe calls are free; "
        "narrowing saves time and rate quota, not money."
    )
    parser.add_argument("--types", help="comma-separated instance types (default: watchlist)")
    parser.add_argument("--regions", help="comma-separated regions (default: all enabled)")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="also load deep price history (AWS retains ~89 days)",
    )
    parser.add_argument("--days", type=int, default=0, help="backfill depth in days")
    parser.add_argument("--show", action="store_true", help="print stored rows afterwards")
    parser.add_argument("--limit", type=int, default=25, help="rows to print with --show")
    parser.add_argument("--quiet", action="store_true", help="errors only")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )
    # One boto3 client per region means ~17 identical "Found credentials" lines.
    for noisy in ("botocore", "boto3", "urllib3", "s3transfer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    config = WebConfig.from_env()
    types = csv(args.types)
    regions = csv(args.regions)
    config = replace(
        config,
        instance_types=types or config.instance_types,
        regions=regions or config.regions,
        backfill_days=args.days or config.backfill_days,
    )

    providers, notes = build_providers(config)
    for note in notes:
        print(f"  ! {note}")
    if not providers:
        print("\n  No provider configured. Run: uv run python scripts/check_setup.py")
        return 1

    scope_regions = ",".join(config.regions) if config.regions else "all enabled"
    scope_types = (
        f"{len(config.instance_types)} instance type(s)"
        if config.instance_types else "all instance types"
    )
    print(f"scanning {scope_types} across {scope_regions}")

    store = SqliteTimeSeriesStore(config.db_path)
    try:
        if args.backfill:
            for provider in providers:
                loader = getattr(provider, "history_segments", None)
                if loader is None:
                    continue
                started = time.monotonic()
                segments = loader(days=config.backfill_days)
                result = store.backfill(segments)
                print(
                    f"  backfill: {len(segments)} segments over {config.backfill_days}d "
                    f"-> {result.inserted} new, {result.skipped} already stored "
                    f"({time.monotonic() - started:.1f}s)"
                )

        started = time.monotonic()
        report = run_tick(providers, store)
        elapsed = time.monotonic() - started

        fetched = sum(report.fetched.values())
        print(
            f"  scan: {fetched} quotes -> {report.write.inserted} new price(s), "
            f"{report.write.extended} unchanged ({elapsed:.1f}s)"
        )
        for name, error in report.failures.items():
            print(f"  ! {name} failed: {error}")
        for provider in providers:
            for note in getattr(provider, "notes", []):
                print(f"  ! {note}")

        if args.show:
            show(
                store,
                config,
                args.limit,
                scope_types=types,
                scope_regions=regions,
            )
    finally:
        store.close()

    # A scan that reached nothing is a failure worth a non-zero exit, so cron and
    # CI notice instead of logging "success" over an empty result.
    return 0 if report.fetched else 1


if __name__ == "__main__":
    sys.exit(main())
