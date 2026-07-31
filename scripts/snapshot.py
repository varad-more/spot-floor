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

from ec2_spot_prices.ingest.pipeline import run_tick
from ec2_spot_prices.storage.base import OfferingFilter
from ec2_spot_prices.storage.sqlite import SqliteTimeSeriesStore
from ec2_spot_prices.web.app import SITE_URL, WebConfig, build_providers, create_app

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
            # encoding pinned: the page carries em-dashes, arrows and ⤢/⟳, and
            # write_text defaults to the locale encoding.
            (out / "index.html").write_text(index.text, encoding="utf-8")
            written.append(out / "index.html")

            market = await client.get("/api/market")
            market.raise_for_status()
            (out / "api").mkdir(parents=True, exist_ok=True)
            # Compact, not pretty-printed. At the full EC2 catalogue this file is
            # 15,000+ rows and `indent=2` roughly doubles it for whitespace that no
            # consumer reads -- and every regeneration commits the whole thing again.
            (out / "api" / "market.json").write_text(
                json.dumps(market.json(), separators=(",", ":")), encoding="utf-8"
            )
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
                path.write_text(
                    json.dumps(response.json(), separators=(",", ":")), encoding="utf-8"
                )
                written.append(path)

    return written


def main() -> int:
    parser = argparse.ArgumentParser(description="Render a static dashboard snapshot.")
    parser.add_argument("--out", default="site", help="output directory")
    parser.add_argument(
        "--retain-days",
        type=int,
        default=0,
        help="drop segments older than this; defaults to EC2_SPOT_PRICES_BACKFILL_DAYS",
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

    # `--out` is deleted wholesale below, so refuse the paths where that means the
    # working tree rather than a build directory.
    out = Path(args.out).resolve()
    if out == Path.cwd() or out in Path.cwd().parents:
        parser.error(f"--out {args.out} would delete the working directory")
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

            # **The clock is read here, not at the top.** A full-catalogue backfill
            # takes ~20 minutes, and stamping this tick with a `now` captured before
            # it would claim these prices were read 20 minutes before they were.
            # `latest()` then discards them as stale (the freshness TTL is 15
            # minutes) and the page renders with zero rows -- while the log happily
            # reports "1,345 instance types", because it asked with the same stale
            # clock. Invisible at 40 types, where the backfill took 35 seconds.
            now = datetime.now(UTC)
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

        # Asked with `now` -- the instant the prices were read -- and handed to the
        # app below as its frozen clock, so this check and every route agree on what
        # "current" means.
        #
        # It used to ask with the wall clock, on the reasoning that the app would use
        # the wall clock a moment later. The app does, but "a moment" is ~14 minutes
        # at full catalogue scope, and `latest()` drops segments older than a
        # 15-minute freshness TTL. So this check passed, `/` rendered with data, and
        # `/api/market` -- fetched after the page was built -- crossed the TTL and
        # rendered zero rows. A published, empty API sitting next to a full page,
        # with every guard here reporting success.
        records = store.latest(OfferingFilter(), now=now)
        instance_types = sorted({r.offering.instance_type for r in records})
        if not records:
            logger.error(
                "nothing in the store is within the freshness window of %s; "
                "refusing to publish an empty page",
                now.isoformat(timespec="seconds"),
            )
            return 1

        # Notes go through the constructor, not app.state: lifespan startup runs
        # later and resets state, which silently dropped the "AWS is not
        # configured" caveat from the first published snapshot -- leaving a page
        # with no AWS rows and no explanation, which is precisely what that note
        # exists to prevent.
        app = create_app(
            store=store,
            config=config,
            poll=False,
            snapshot=True,
            snapshot_now=now,
            notes=notes,
        )

        written = asyncio.run(
            render(app, out, instance_types, history_days=config.history_days)
        )
    finally:
        store.close()

    # Pages would otherwise run the output through Jekyll.
    (out / ".nojekyll").write_text("")

    # Written here rather than kept in `public/`, because `--out` is deleted
    # wholesale at the top of this function -- a file committed straight into the
    # published directory survives exactly until the next snapshot.
    #
    # `/api/history/` is disallowed: it is 1,345 JSON files that nothing links to,
    # and letting a crawler walk them spends the site's crawl budget on documents
    # no one searches for. `market.json` stays crawlable -- Google Dataset Search
    # fetches it to verify the `distribution` declared in the page's structured
    # data, and blocking it would invalidate the one listing that costs nothing.
    (out / "robots.txt").write_text(
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /api/history/\n"
        "\n"
        f"Sitemap: {SITE_URL}/sitemap.xml\n",
        encoding="utf-8",
    )

    # One URL, so the sitemap is not for discovery -- it is for `lastmod`. The page
    # is republished with new prices far more often than a crawler would guess from
    # a static HTML file, and this is the only place that says so.
    (out / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        "  <url>\n"
        f"    <loc>{SITE_URL}/</loc>\n"
        f"    <lastmod>{now:%Y-%m-%d}</lastmod>\n"
        "  </url>\n"
        "</urlset>\n",
        encoding="utf-8",
    )

    # The social card is fixed artwork, not data, so it is a committed asset rather
    # than something rendered on every run -- which keeps a headless browser out of
    # this script's dependencies. It only changes when the wordmark does.
    og = Path(__file__).resolve().parent.parent / "assets" / "og.png"
    shutil.copyfile(og, out / "og.png")

    written.extend([out / "robots.txt", out / "sitemap.xml", out / "og.png"])

    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    print(
        f"\nwrote {len(written)} files to {out}/ "
        f"({size / 1024:.0f} KiB, {len(instance_types)} instance types, "
        f"{retain_days}d retained)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
