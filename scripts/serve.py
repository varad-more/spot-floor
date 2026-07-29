"""Run the dashboard against live providers.

    uv run python scripts/serve.py

Environment:
    SPOTFLOOR_DB                 sqlite path            (default spotfloor.db)
    SPOTFLOOR_AWS_REGIONS        comma-separated        (default us-east-1)
    SPOTFLOOR_AWS_INSTANCE_TYPES comma-separated
    SPOTFLOOR_POLL_INTERVAL_S    seconds                (default 300)
    SPOTFLOOR_HISTORY_HOURS      chart window           (default 6)
    SPOTFLOOR_PORT               (default 8000)

Set several AWS regions to compare AWS against itself:

    SPOTFLOOR_AWS_REGIONS=us-east-1,us-west-2,eu-west-1 uv run python scripts/serve.py

Vast needs no credentials. Without AWS credentials the app still runs and says so
on the page rather than showing an empty AWS section that reads as "no capacity".
"""

from __future__ import annotations

import logging
import os

import uvicorn

from spotfloor.web.app import WebConfig, create_app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    config = WebConfig.from_env()
    port = int(os.getenv("SPOTFLOOR_PORT", "8000"))

    print(f"spotfloor -> http://127.0.0.1:{port}")
    print(f"  db={config.db_path}  poll={config.poll_interval_s}s  "
          f"aws_regions={','.join(config.aws_regions)}")
    print("  first tick runs immediately; the page fills in as it lands.\n")

    uvicorn.run(create_app(config=config), host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
