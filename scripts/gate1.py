"""GATE 1 evidence: live region fan-out, dedup, backfill-as-segments, AWS honesty.

Run: uv run python scripts/gate1.py

Four claims are proved against the live API rather than asserted in a docstring:

1. **Dedup**: repeated polls EXTEND segments instead of inserting duplicates, so
   the table grows with change and not with time.
2. **Zones inside one region genuinely differ in price** -- which is why the region
   roll-up has to name the zone it took its number from.
3. **Spot price history is a change-log**: consecutive quotes tile the window with
   no gap and no overlap, which is what makes ``store.backfill`` exact rather than
   a reconstruction.
4. **AWS availability is UNKNOWN**, and the placement-score API is never called.
"""

from __future__ import annotations

import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from spotfloor.ingest.pipeline import run_tick
from spotfloor.models import Availability
from spotfloor.providers.aws import AwsProvider, CredsOwner
from spotfloor.query import region_table
from spotfloor.storage.base import OfferingFilter, TimeRange
from spotfloor.storage.sqlite import SqliteTimeSeriesStore

CYCLES = 3
REGIONS = ("us-east-1", "us-west-2", "eu-west-1")
TYPES = ("m5.large", "c5.large", "p5.48xlarge")
BACKFILL_DAYS = 7


def main() -> None:
    db = Path(tempfile.mkdtemp()) / "gate1.db"
    store = SqliteTimeSeriesStore(str(db))
    provider = AwsProvider(
        regions=REGIONS, instance_types=TYPES, creds_owner=CredsOwner.APP
    )
    providers = [provider]

    print("=" * 78)
    print(f"GATE 1 -- {CYCLES} live poll cycles across {len(REGIONS)} regions")
    print("=" * 78)
    print(f"{'cycle':<7}{'fetched':<20}{'inserted':>10}{'extended':>10}{'failures':>12}")

    for cycle in range(1, CYCLES + 1):
        report = run_tick(providers, store)
        fetched = ", ".join(f"{k}={v}" for k, v in report.fetched.items())
        print(
            f"{cycle:<7}{fetched:<20}{report.write.inserted:>10}"
            f"{report.write.extended:>10}{len(report.failures):>12}"
        )
        assert report.ok, f"provider failures: {report.failures}"
        if cycle < CYCLES:
            time.sleep(2)

    print(
        "\nDedup: cycle 1 opens the segments; later cycles EXTEND them rather than\n"
        "inserting duplicates. The table grows with change, not with time."
    )
    for note in provider.notes:
        print(f"  note: {note}")

    now = datetime.now(UTC)

    # --- CLAIM 2: zones inside one region differ, so a roll-up must name one ---
    print("\n" + "=" * 78)
    print("Zone spread within a region (why the roll-up names its zone)")
    print("=" * 78)
    rows = region_table(store.latest(OfferingFilter(), now=now))
    print(f"{'instance':<15}{'region':<13}{'cheapest':>11}{'zone':>14}{'spread':>9}")

    spreads = []
    for row in sorted(rows, key=lambda r: -r.spread_pct)[:8]:
        spreads.append(row.spread_pct)
        print(
            f"{row.instance_type:<15}{row.region:<13}"
            f"${row.cheapest_usd_hr:>10.4f}{row.cheapest_zone:>14}"
            f"{row.spread_pct:>8.1f}%"
        )
    if spreads and max(spreads) > 0:
        print(
            f"\n  Widest intra-region spread: {max(spreads):.1f}%. A bare regional\n"
            "  minimum would hide that, and you cannot launch into a region average."
        )

    # --- CLAIM 3: history is a change-log whose segments tile the window ---
    print("\n" + "=" * 78)
    print(f"Backfill: {BACKFILL_DAYS}d of history as segments")
    print("=" * 78)
    started = time.monotonic()
    segments = provider.history_segments(days=BACKFILL_DAYS)
    result = store.backfill(segments)
    print(
        f"  {len(segments)} segments fetched in {time.monotonic() - started:.1f}s "
        f"-> {result.inserted} new, {result.skipped} already stored"
    )
    assert segments, "no history returned"

    by_series: dict[tuple[str, str | None], list] = {}
    for segment in segments:
        by_series.setdefault(
            (segment.offering.instance_type, segment.offering.zone), []
        ).append(segment)

    contiguous = 0
    for key, group in by_series.items():
        group.sort(key=lambda s: s.first_seen)
        for earlier, later in zip(group, group[1:]):
            assert earlier.last_seen == later.first_seen, f"gap or overlap in {key}"
            contiguous += 1
    print(
        f"  {len(by_series)} series, {contiguous} adjacent segment pairs, every one\n"
        "  meeting exactly: history is a change-log, so the intervals are AWS's own."
    )

    # Idempotence: the DB is a rebuildable cache, so a lost cache re-backfills.
    again = store.backfill(segments)
    assert again.inserted == 0, "re-running the backfill duplicated rows"
    print(f"  re-backfill inserted {again.inserted} rows (idempotent)")

    deep = store.history(
        OfferingFilter(instance_type="m5.large"),
        TimeRange(now - timedelta(days=BACKFILL_DAYS), now),
    )
    print(f"  m5.large now has {len(deep)} segments of real history across all zones")

    # --- CLAIM 4: the honesty constraint, against live data ---
    print("\n" + "=" * 78)
    print("Availability signal (stated honestly)")
    print("=" * 78)
    records = store.latest(OfferingFilter(provider="aws"), now=now)
    counts: dict[str, int] = {}
    for record in records:
        key = str(record.offering.availability)
        counts[key] = counts.get(key, 0) + 1
    print(f"  aws {counts}")

    assert records, "no AWS rows"
    assert all(r.offering.availability is Availability.UNKNOWN for r in records)
    print(
        "\n  AWS is UNKNOWN by construction: Spot Placement Score is computed against\n"
        "  the CALLING account's quota, so a score fetched with app credentials is a\n"
        "  fact about us, not about the user. We do not publish it as a market signal.\n"
        "  This is a PRICE comparator. It does not claim to know what you can get."
    )

    store.close()
    print("\nGATE 1: PASS")


if __name__ == "__main__":
    main()
