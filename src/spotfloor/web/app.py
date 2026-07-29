"""FastAPI dashboard: live cross-provider prices plus per-series history.

Read-only by design. The page renders what the poller already stored; a page load
never triggers a provider fetch. That keeps rendering fast, keeps provider rate
limits tied to the poll interval rather than to traffic, and means the chart and
the table are two views of one set of stored facts rather than two independent
fetches that can disagree.

The template's job is to render :mod:`spotfloor.query` output faithfully. Three
things it must not do, all of which a normal dashboard does by default:

* show a blank cell for AWS availability -- it renders an explicit ``unknown``;
* rank AWS and Vast rows against each other by region -- rows group by GPU model,
  and region stays provider-native;
* claim the cheapest row is the cheapest that exists -- Vast returns a slice, so
  the page says "cheapest observed".
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Sequence

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from spotfloor.ingest.poller import Poller
from spotfloor.models import PriceKind
from spotfloor.providers.base import Provider
from spotfloor.query import FloorPoint, MarketRow, floor_series, market_table
from spotfloor.storage.base import OfferingFilter, OfferingRecord, TimeRange, TimeSeriesStore
from spotfloor.storage.sqlite import SqliteTimeSeriesStore
from spotfloor.web.sparkline import sparkline_svg

logger = logging.getLogger(__name__)

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Enough AWS GPU families to make a cross-region price comparison meaningful
# without pulling the entire catalog's spot history on every tick.
DEFAULT_AWS_INSTANCE_TYPES = (
    "p5.48xlarge",
    "p4d.24xlarge",
    "g6.xlarge",
    "g6e.xlarge",
    "g5.xlarge",
)


@dataclass(frozen=True, slots=True)
class WebConfig:
    db_path: str = "spotfloor.db"
    aws_regions: tuple[str, ...] = ("us-east-1",)
    aws_instance_types: tuple[str, ...] = DEFAULT_AWS_INSTANCE_TYPES
    poll_interval_s: int = 300
    history_hours: int = 6
    buckets: int = 48

    @classmethod
    def from_env(cls) -> WebConfig:
        """Environment overrides; anything unset keeps the field default.

        Only *present* variables become keyword arguments, so the defaults above
        stay the single source of truth. Reading them back off the class instead
        -- ``os.getenv("X", cls.history_hours)`` -- looks equivalent and is not:
        this is a ``slots=True`` dataclass, so class-level attribute access
        returns the slot descriptor rather than the default value, and the
        descriptor sails silently into the config until something tries to do
        arithmetic with it.
        """
        overrides: dict[str, Any] = {}

        def csv(env: str, field: str) -> None:
            raw = os.getenv(env)
            if raw:
                parts = tuple(p.strip() for p in raw.split(",") if p.strip())
                if parts:
                    overrides[field] = parts

        def integer(env: str, field: str) -> None:
            raw = os.getenv(env)
            if raw:
                overrides[field] = int(raw)

        if db := os.getenv("SPOTFLOOR_DB"):
            overrides["db_path"] = db
        csv("SPOTFLOOR_AWS_REGIONS", "aws_regions")
        csv("SPOTFLOOR_AWS_INSTANCE_TYPES", "aws_instance_types")
        integer("SPOTFLOOR_POLL_INTERVAL_S", "poll_interval_s")
        integer("SPOTFLOOR_HISTORY_HOURS", "history_hours")
        integer("SPOTFLOOR_BUCKETS", "buckets")

        return cls(**overrides)


def build_providers(config: WebConfig) -> tuple[list[Provider], list[str]]:
    """Assemble providers, degrading to Vast-only rather than failing to boot.

    Returns the providers and a list of human-readable notes about anything that
    could *not* be configured. Those notes are rendered on the page: a dashboard
    that silently omits AWS looks identical to one where AWS has no capacity, and
    those are very different claims.
    """
    from spotfloor.providers.vast import VastProvider

    providers: list[Provider] = [VastProvider()]
    notes: list[str] = []

    try:
        import boto3

        # An unset GitHub Actions secret arrives as an empty string rather than as
        # an absent variable, and botocore will hand back a Credentials object with
        # a blank access key instead of None. Test the key, not just the object,
        # or CI "configures" AWS and then fails every call.
        credentials = boto3.Session().get_credentials()
        if credentials is None or not credentials.access_key:
            notes.append(
                "AWS is not configured (no credentials found), so no AWS prices are "
                "shown. Vast data is unaffected."
            )
        else:
            from spotfloor.providers.aws import AwsProvider, CredsOwner

            for region in config.aws_regions:
                providers.append(
                    AwsProvider(
                        boto3.client("ec2", region_name=region),
                        creds_owner=CredsOwner.APP,
                        instance_types=config.aws_instance_types,
                    )
                )
    except Exception as exc:  # noqa: BLE001 - a missing/broken AWS setup must not stop the app
        logger.warning("aws provider unavailable: %s", exc)
        notes.append(f"AWS provider unavailable: {exc}")

    return providers, notes


# --- view models -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TableEntry:
    """One market row plus its rendered history, ready for the template."""

    row: MarketRow
    series: list[FloorPoint]
    spark: str

    @property
    def observed_buckets(self) -> int:
        return sum(1 for p in self.series if p.floor_per_gpu_hr is not None)


def _series_key(record: OfferingRecord) -> tuple[str, str, PriceKind]:
    offering = record.offering
    return (offering.gpu_model, offering.provider, offering.price_kind)


def build_entries(
    store: TimeSeriesStore, config: WebConfig, *, now: datetime
) -> list[TableEntry]:
    """Current rows, each paired with its floor history over the config window.

    History is fetched **once** for the whole window and grouped in memory rather
    than queried per row: one range scan beats N queries, and every row is then
    guaranteed to be bucketed against the identical time grid, so the sparklines
    are comparable to each other.
    """
    window = TimeRange(now - timedelta(hours=config.history_hours), now)
    rows = market_table(store.latest(OfferingFilter(), now=now))

    grouped: dict[tuple[str, str, PriceKind], list[OfferingRecord]] = {}
    for record in store.history(OfferingFilter(), window):
        grouped.setdefault(_series_key(record), []).append(record)

    entries = []
    for row in rows:
        key = (row.gpu_model, row.provider, row.price_kind)
        series = floor_series(grouped.get(key, []), window, buckets=config.buckets)
        entries.append(
            TableEntry(
                row=row,
                series=series,
                spark=sparkline_svg([p.floor_per_gpu_hr for p in series]),
            )
        )
    return entries


def _row_json(entry: TableEntry) -> dict[str, Any]:
    row = entry.row
    return {
        "gpu_model": row.gpu_model,
        "provider": row.provider,
        "price_kind": str(row.price_kind),
        "cheapest_per_gpu_hr": row.cheapest_per_gpu_hr,
        "cheapest_obtainable_per_gpu_hr": row.cheapest_obtainable_per_gpu_hr,
        "cheapest_price_usd_hr": row.cheapest_price_usd_hr,
        "cheapest_gpu_count": row.cheapest_gpu_count,
        "cheapest_region": row.cheapest_region,
        "cheapest_availability": str(row.cheapest_availability),
        "availability_known": row.availability_known,
        "availability_counts": {str(k): v for k, v in row.counts.items()},
        "node_count": row.node_count,
        "obtainable_nodes": row.obtainable_nodes,
        "last_observed_at": row.last_observed_at.isoformat(),
    }


# --- app ---------------------------------------------------------------------


def create_app(
    *,
    store: TimeSeriesStore | None = None,
    providers: Sequence[Provider] | None = None,
    config: WebConfig | None = None,
    poll: bool = True,
    snapshot: bool = False,
    notes: Sequence[str] | None = None,
) -> FastAPI:
    """Build the app.

    An injected ``store`` is treated as borrowed: the caller owns its lifetime and
    the app will not close it. That is what lets tests drive the real routes
    against a fixture store without the app tearing it down.

    ``snapshot=True`` renders for a static host (GitHub Pages): no auto-refresh,
    relative API paths, and the page states in its own words that it is a
    point-in-time snapshot rather than a live view. A stale page that *looks*
    live is the same kind of claim this project refuses to make about
    availability, so the mode is explicit rather than inferred.

    ``notes`` are caller-supplied caveats rendered on the page -- for a caller
    that assembled the providers itself and so knows what is missing. They must
    be passed here rather than assigned to ``app.state`` afterwards, because
    lifespan startup runs later and would overwrite them.
    """
    config = config or WebConfig.from_env()
    owns_store = store is None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Iterator[None]:
        app.state.notes = list(notes or [])
        if owns_store:
            app.state.store = SqliteTimeSeriesStore(config.db_path)
        else:
            app.state.store = store

        poller = None
        if poll:
            selected = list(providers) if providers is not None else None
            if selected is None:
                selected, discovered = build_providers(config)
                app.state.notes = [*app.state.notes, *discovered]
            poller = Poller(selected, app.state.store, interval_s=config.poll_interval_s)
            poller.start()

        try:
            yield
        finally:
            if poller is not None:
                poller.stop()
            if owns_store:
                app.state.store.close()

    app = FastAPI(title="spotfloor", lifespan=lifespan)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/market")
    def api_market(request: Request) -> JSONResponse:
        now = datetime.now(UTC)
        entries = build_entries(request.app.state.store, config, now=now)
        return JSONResponse(
            {
                "generated_at": now.isoformat(),
                "history_hours": config.history_hours,
                "rows": [_row_json(e) for e in entries],
                "notes": request.app.state.notes,
            }
        )

    @app.get("/api/history/{gpu_model}")
    def api_history(
        request: Request,
        gpu_model: str,
        provider: str | None = None,
        hours: int = Query(default=0, ge=0, le=24 * 30),
        buckets: int = Query(default=0, ge=0, le=1000),
    ) -> JSONResponse:
        now = datetime.now(UTC)
        hours = hours or config.history_hours
        buckets = buckets or config.buckets
        window = TimeRange(now - timedelta(hours=hours), now)
        records = request.app.state.store.history(
            OfferingFilter(gpu_model=gpu_model, provider=provider), window
        )
        if not records:
            raise HTTPException(
                status_code=404,
                detail=f"no observations for {gpu_model!r} in the last {hours}h",
            )
        series = floor_series(records, window, buckets=buckets)
        return JSONResponse(
            {
                "gpu_model": gpu_model,
                "provider": provider,
                "hours": hours,
                # `null` means not observed. It is not zero and not the previous
                # price; clients must render it as a gap.
                "points": [
                    {"at": p.at.isoformat(), "floor_per_gpu_hr": p.floor_per_gpu_hr}
                    for p in series
                ],
            }
        )

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Any:
        now = datetime.now(UTC)
        entries = build_entries(request.app.state.store, config, now=now)
        providers_seen = sorted({e.row.provider for e in entries})
        return _TEMPLATES.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "entries": entries,
                "generated_at": now,
                "config": config,
                "notes": request.app.state.notes,
                "providers_seen": providers_seen,
                "aws_present": "aws" in providers_seen,
                "snapshot": snapshot,
            },
        )

    return app
