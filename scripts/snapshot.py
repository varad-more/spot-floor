"""Render a static snapshot of the dashboard, for a static host (GitHub Pages).

    uv run python scripts/snapshot.py --out site

Polls the providers once, writes the observation to the store, then renders the
page and the JSON endpoints to files.

**It drives the real app over ASGI rather than re-rendering.** A second renderer
that "just" produced the same HTML would drift from the served one, and the first
thing to drift would be a caveat -- so the static files are literally the
responses the live app gives.

**GitHub Pages is a static host, so this is a snapshot and says so.** The page
renders in snapshot mode: no auto-refresh, and a banner stating when the prices
were read. A stale page that looks live is the same species of claim this project
refuses to make about AWS availability.

**The database is working state, not published output.** History has to survive
between runs or every sparkline is a single dot, but the store churns ~3,200
segments per poll and publishing that hourly would push tens of megabytes through
Pages to render a 150 KiB page. CI keeps the database in its own cache; the site
carries only what is meant to be read. Retention is tied to the chart window,
because nothing older than that is ever drawn.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from spotfloor.ingest.pipeline import run_tick
from spotfloor.storage.base import OfferingFilter
from spotfloor.storage.sqlite import SqliteTimeSeriesStore
from spotfloor.web.app import WebConfig, build_providers, create_app

logger = logging.getLogger("snapshot")


async def render(
    app, out: Path, instance_types: list[str], *, history_days: int
) -> list[Path]:
    """Fetch every route from the app over ASGI and write it to disk."""
    written: list[Path] = []
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://snapshot.local"
    ) as client:
        async with app.router.lifespan_context(app):
            index = await client.get("/")
            index.raise_for_status()
            (out / "index.html").write_text(index.text)
            written.append(out / "index.html")

            market = await client.get("/api/market")
            market.raise_for_status()
            (out / "api").mkdir(parents=True, exist_ok=True)
            (out / "api" / "market.json").write_text(json.dumps(market.json(), indent=2))
            written.append(out / "api" / "market.json")

            history_dir = out / "api" / "history"
            history_dir.mkdir(parents=True, exist_ok=True)
            for instance_type in instance_types:
                response = await client.get(
                    f"/api/history/{instance_type}", params={"days": history_days}
                )
                if response.status_code != 200:
                    continue
                path = history_dir / f"{instance_type}.json"
                path.write_text(json.dumps(response.json(), indent=2))
                written.append(path)

    return written


def main() -> int:
    parser = argparse.ArgumentParser(description="Render a static dashboard snapshot.")
    parser.add_argument("--out", default="site", help="output directory")
    parser.add_argument(
        "--retain-days",
        type=int,
        default=0,
        help="drop segments older than this; defaults to SPOTFLOOR_BACKFILL_DAYS",
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="load deep history from the provider before rendering",
    )
    parser.add_argument(
        "--skip-poll",
        action="store_true",
        help="render from the existing store without contacting providers",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
    # One boto3 client per region means ~17 identical "Found credentials" lines at
    # INFO, which buries the counts that actually matter in CI logs.
    for noisy in ("botocore", "boto3", "urllib3", "s3transfer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    config = WebConfig.from_env()
    # Retention matches the deepest window anything can ask for. Keeping more is
    # dead weight in the cache; keeping less would silently truncate the API.
    retain_days = args.retain_days or config.backfill_days

    store = SqliteTimeSeriesStore(config.db_path)
    now = datetime.now(UTC)

    try:
        notes: list[str] = []
        if not args.skip_poll:
            providers, notes = build_providers(config)

            # Deep history first, so a cold cache still renders full-depth charts.
            # AWS retains ~89 days and hands them over on request, so waiting for a
            # poller to accumulate what we could simply ask for would be perverse.
            if args.backfill:
                for provider in providers:
                    loader = getattr(provider, "history_segments", None)
                    if loader is None:
                        continue
                    segments = loader(days=config.backfill_days)
                    result = store.backfill(segments)
                    logger.info(
                        "backfilled %s: %d segments -> %d new, %d already stored",
                        provider.name,
                        len(segments),
                        result.inserted,
                        result.skipped,
                    )

            report = run_tick(providers, store, now=now)
            logger.info(
                "fetched=%s inserted=%d extended=%d failures=%s",
                report.fetched,
                report.write.inserted,
                report.write.extended,
                list(report.failures),
            )
            # Publishing a page with no data would replace a good snapshot with an
            # empty one. Failing the job keeps the last good deploy in place.
            if not report.fetched:
                logger.error("no provider returned data; refusing to publish an empty page")
                return 1
            for name, error in report.failures.items():
                notes.append(f"{name} could not be reached for this snapshot: {error}")

            # Per-region failures (opt-in regions raise AuthFailure) are the
            # provider's own notes, and they must reach the page: an absent region
            # is indistinguishable from a region with no capacity.
            for provider in providers:
                notes.extend(getattr(provider, "notes", []))

        removed = store.prune(now - timedelta(days=retain_days))
        if removed:
            logger.info("pruned %d segments older than %dd", removed, retain_days)

        records = store.latest(OfferingFilter(), now=now)
        instance_types = sorted({r.offering.instance_type for r in records})

        # Notes go through the constructor, not app.state: lifespan startup runs
        # later and resets state, which silently dropped the "AWS is not
        # configured" caveat from the first published snapshot -- leaving a page
        # with no AWS rows and no explanation, which is precisely what that note
        # exists to prevent.
        app = create_app(
            store=store, config=config, poll=False, snapshot=True, notes=notes
        )

        written = asyncio.run(
            render(app, out, instance_types, history_days=config.history_days)
        )
    finally:
        store.close()

    # Pages would otherwise run the output through Jekyll.
    (out / ".nojekyll").write_text("")

    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    print(
        f"\nwrote {len(written)} files to {out}/ "
        f"({size / 1024:.0f} KiB, {len(instance_types)} instance types, "
        f"{retain_days}d retained)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
