"""Run the dashboard against the live AWS API.

    uv run python scripts/serve.py

Environment:
    SPOTFLOOR_DB              sqlite path      (default spotfloor.db)
    SPOTFLOOR_REGIONS         comma-separated  (default: every enabled region)
    SPOTFLOOR_INSTANCE_TYPES  comma-separated  (default: the 40-type watchlist)
    SPOTFLOOR_POLL_INTERVAL_S seconds          (default 300)
    SPOTFLOOR_HISTORY_DAYS    chart window     (default 7)
    SPOTFLOOR_BACKFILL_DAYS   initial history  (default 30)
    SPOTFLOOR_PORT                             (default 8000)

Two regions and a couple of types, for a fast start:

    SPOTFLOOR_REGIONS=us-east-1,us-west-2 \\
    SPOTFLOOR_INSTANCE_TYPES=m5.large,p5.48xlarge \\
    uv run python scripts/serve.py

Pass ``--backfill`` to load deep history before serving. Without it the charts
start empty and fill in one poll at a time; with it they are full-depth
immediately, because AWS retains ~89 days and will hand them over on request.
AWS credentials are required -- without them the app still boots and says so on
the page rather than rendering an empty table that reads as "no capacity".
"""

from __future__ import annotations

import argparse
import logging
import os
import time

import uvicorn

from spotfloor.storage.sqlite import SqliteTimeSeriesStore
from spotfloor.web.app import WebConfig, build_providers, create_app

logger = logging.getLogger("serve")


def backfill(config: WebConfig) -> None:
    """Load deep history once, before the server starts taking requests."""
    providers, notes = build_providers(config)
    for note in notes:
        logger.warning("%s", note)
    if not providers:
        return

    store = SqliteTimeSeriesStore(config.db_path)
    try:
        for provider in providers:
            loader = getattr(provider, "history_segments", None)
            if loader is None:
                continue
            started = time.monotonic()
            segments = loader(days=config.backfill_days)
            result = store.backfill(segments)
            logger.info(
                "backfilled %s: %d segments -> %d new, %d already stored (%.1fs)",
                provider.name,
                len(segments),
                result.inserted,
                result.skipped,
                time.monotonic() - started,
            )
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the spotfloor dashboard.")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="load SPOTFLOOR_BACKFILL_DAYS of history before serving",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    config = WebConfig.from_env()
    port = int(os.getenv("SPOTFLOOR_PORT", "8000"))

    regions = ",".join(config.regions) if config.regions else "all enabled"
    print(f"spotfloor -> http://127.0.0.1:{port}")
    print(f"  db={config.db_path}  poll={config.poll_interval_s}s  regions={regions}")
    print(f"  {len(config.instance_types)} instance types  "
          f"chart window {config.history_days}d")
    if args.backfill:
        print(f"  backfilling {config.backfill_days}d of history first...\n")
        backfill(config)
    else:
        print("  no --backfill: charts start empty and fill in per poll.\n")

    uvicorn.run(create_app(config=config), host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
