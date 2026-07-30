"""FastAPI dashboard: AWS spot prices compared across regions, with history.

Read-only by design. The page renders what the poller and the backfill already
stored; a page load never triggers a provider fetch. That keeps rendering fast,
keeps API quota tied to the poll schedule rather than to traffic, and means the
table and the charts are two views of one set of stored facts rather than two
independent fetches that can disagree.

The template's job is to render :mod:`spotfloor.query` output faithfully. Four
things it must not do, all of which a dashboard does by default:

* show a blank cell for availability -- it renders an explicit ``unknown``, because
  AWS cannot tell us whether you would get the instance and a blank cell reads as
  "none available";
* present a bare regional price -- you launch into a *zone*, so the zone that
  produced the number is named, and the intra-region spread is shown next to it;
* present volatility as an availability signal -- it is a price fact and a
  contention proxy, nothing more;
* interpolate a chart across a gap -- unobserved buckets stay gaps.

**Why the page window is shorter than the stored window.** The store holds
``backfill_days`` (30 by default) because AWS serves ~89 days on demand and deep
history is nearly free to acquire. The *page* charts ``history_days`` (7) because
680 rows x 30 days is well over a million segments to load and bucket per render.
Deeper history stays available per instance type through
``/api/history/{instance_type}?days=N``, which filters to one series and is cheap.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from spotfloor.ingest.pipeline import run_tick
from spotfloor.ingest.poller import Poller
from spotfloor.providers.aws import DEFAULT_INSTANCE_TYPES
from spotfloor.providers.base import Provider
from spotfloor.query import FloorPoint, RegionRow, floor_series, region_table
from spotfloor.storage.base import OfferingFilter, OfferingRecord, TimeRange, TimeSeriesStore
from spotfloor.storage.sqlite import SqliteTimeSeriesStore
from spotfloor.web.sparkline import sparkline_svg

logger = logging.getLogger(__name__)

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@dataclass(frozen=True, slots=True)
class WebConfig:
    db_path: str = "spotfloor.db"
    # None means "every region this account has enabled", discovered at runtime.
    regions: tuple[str, ...] | None = None
    instance_types: tuple[str, ...] = DEFAULT_INSTANCE_TYPES
    poll_interval_s: int = 300
    # What the page charts. See the module docstring for why it is not backfill_days.
    history_days: int = 7
    # How deep the initial backfill goes. AWS retains ~89 days.
    backfill_days: int = 30
    # 3-hourly over the 7-day window. A sparkline renders 168px wide, so finer
    # bucketing is sub-pixel: it costs page weight (each point is ~12 bytes of
    # coordinate text, times ~650 rows) and renders identically.
    buckets: int = 56

    @classmethod
    def from_env(cls) -> WebConfig:
        """Environment overrides; anything unset keeps the field default.

        Only *present* variables become keyword arguments, so the defaults above
        stay the single source of truth. Reading them back off the class instead --
        ``os.getenv("X", cls.history_days)`` -- looks equivalent and is not: this is
        a ``slots=True`` dataclass, so class-level attribute access returns the slot
        descriptor rather than the default value, and the descriptor sails silently
        into the config until something tries to do arithmetic with it.
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
        csv("SPOTFLOOR_REGIONS", "regions")
        csv("SPOTFLOOR_INSTANCE_TYPES", "instance_types")
        integer("SPOTFLOOR_POLL_INTERVAL_S", "poll_interval_s")
        integer("SPOTFLOOR_HISTORY_DAYS", "history_days")
        integer("SPOTFLOOR_BACKFILL_DAYS", "backfill_days")
        integer("SPOTFLOOR_BUCKETS", "buckets")

        return cls(**overrides)


def build_providers(config: WebConfig) -> tuple[list[Provider], list[str]]:
    """Assemble the AWS provider, degrading to nothing rather than failing to boot.

    Returns the providers and a list of human-readable notes about anything that
    could not be configured. Those notes are rendered on the page: a dashboard that
    silently omits data looks identical to one where there is no capacity, and those
    are very different claims.
    """
    providers: list[Provider] = []
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
                "AWS is not configured (no credentials found), so no prices can be "
                "shown. Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY."
            )
        else:
            from spotfloor.providers.aws import AwsProvider, CredsOwner

            providers.append(
                AwsProvider(
                    regions=config.regions,
                    instance_types=config.instance_types,
                    creds_owner=CredsOwner.APP,
                )
            )
    except Exception as exc:  # noqa: BLE001 - a broken AWS setup must not stop the app
        logger.warning("aws provider unavailable: %s", exc)
        notes.append(f"AWS provider unavailable: {exc}")

    return providers, notes


# --- view models -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TableEntry:
    """One region row plus its rendered history, ready for the template."""

    row: RegionRow
    series: list[FloorPoint]
    spark: str

    @property
    def observed_buckets(self) -> int:
        return sum(1 for p in self.series if p.floor_usd_hr is not None)


def _series_key(record: OfferingRecord) -> tuple[str, str]:
    return (record.offering.instance_type, record.offering.region)


def build_entries(
    store: TimeSeriesStore, config: WebConfig, *, now: datetime
) -> list[TableEntry]:
    """Current rows, each paired with its price history over the page window.

    History is fetched **once** for the whole window and grouped in memory rather
    than queried per row: one range scan beats 680 queries, and every row is then
    guaranteed to be bucketed against the identical time grid, so the sparklines are
    comparable to each other. The same grouping feeds the volatility columns, so the
    numbers next to a chart describe exactly the window the chart draws.
    """
    window = TimeRange(now - timedelta(days=config.history_days), now)

    grouped: dict[tuple[str, str], list[OfferingRecord]] = {}
    for record in store.history(OfferingFilter(), window):
        grouped.setdefault(_series_key(record), []).append(record)

    rows = region_table(store.latest(OfferingFilter(), now=now), history=grouped)

    entries = []
    for row in rows:
        series = floor_series(
            grouped.get((row.instance_type, row.region), []),
            window,
            buckets=config.buckets,
        )
        entries.append(
            TableEntry(
                row=row,
                series=series,
                spark=sparkline_svg([p.floor_usd_hr for p in series]),
            )
        )
    return entries


def series_payload(
    entries: Sequence[TableEntry], *, now: datetime, config: WebConfig
) -> dict[str, Any]:
    """Every row's price history, compact enough to embed in the page.

    The chart plots arbitrary combinations of (instance type, region), so the data
    has to be present before the user picks -- fetching per selection would break
    the static export, which has no server. Embedding it also means the chart works
    offline once the page is loaded.

    Compact by construction: a shared time axis (``start`` + ``step_s``) instead of a
    timestamp per point, and prices rounded to 6 significant digits. Emitting
    ``{"at": iso, "price": x}`` per point would be roughly 6x the bytes for identical
    pixels -- 646 series x 56 buckets is 36k points either way.

    ``null`` means *not observed* and must be drawn as a break in the line. It is not
    zero and not the previous price carried forward.
    """
    step_s = int(timedelta(days=config.history_days).total_seconds() / config.buckets)
    return {
        "start": (now - timedelta(days=config.history_days)).isoformat(),
        "step_s": step_s,
        "buckets": config.buckets,
        "series": {
            f"{e.row.instance_type}|{e.row.region}": [
                None if p.floor_usd_hr is None else round(p.floor_usd_hr, 6)
                for p in e.series
            ]
            for e in entries
        },
    }


def _row_json(entry: TableEntry) -> dict[str, Any]:
    row = entry.row
    return {
        "instance_type": row.instance_type,
        "instance_family": row.instance_family,
        "region": row.region,
        "price_kind": str(row.price_kind),
        "cheapest_usd_hr": row.cheapest_usd_hr,
        "cheapest_zone": row.cheapest_zone,
        "dearest_usd_hr": row.dearest_usd_hr,
        "dearest_zone": row.dearest_zone,
        "spread_pct": round(row.spread_pct, 2),
        "zone_count": row.zone_count,
        "zones": [
            {"zone": z.zone, "price_usd_hr": z.price_usd_hr} for z in row.zones
        ],
        "vcpus": row.vcpus,
        "memory_gib": row.memory_gib,
        "gpu_model": row.gpu_model,
        "gpu_count": row.gpu_count,
        "cheapest_per_gpu_hr": row.cheapest_per_gpu_hr,
        "cheapest_per_vcpu_hr": row.cheapest_per_vcpu_hr,
        # Always 'unknown' on AWS. Present explicitly so a consumer cannot mistake
        # a missing key for an absence of capacity.
        "availability": str(row.availability),
        "availability_known": row.availability_known,
        "price_changes": row.price_changes,
        "coefficient_of_variation": row.coefficient_of_variation,
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
    provider_factory: Callable[[WebConfig], tuple[list[Provider], list[str]]] | None = None,
) -> FastAPI:
    """Build the app.

    An injected ``store`` is treated as borrowed: the caller owns its lifetime and
    the app will not close it. That is what lets tests drive the real routes against
    a fixture store without the app tearing it down.

    ``snapshot=True`` renders for a static host (GitHub Pages): no auto-refresh,
    relative API paths, and the page states in its own words that it is a
    point-in-time snapshot rather than a live view. A stale page that *looks* live is
    the same kind of unearned claim this project refuses to make about availability,
    so the mode is explicit rather than inferred. It also drops the refresh route and
    button entirely -- a static file has no server to scan with, and a button that
    silently does nothing is worse than no button.

    ``notes`` are caller-supplied caveats rendered on the page -- for a caller that
    assembled the providers itself and so knows what is missing. They must be passed
    here rather than assigned to ``app.state`` afterwards, because lifespan startup
    runs later and would overwrite them.

    ``provider_factory`` builds providers from a (possibly narrowed) config. It exists
    because ``POST /api/refresh`` scans a *scope* rather than everything, so providers
    have to be constructible per request instead of once at startup.
    """
    config = config or WebConfig.from_env()
    owns_store = store is None
    factory = provider_factory or build_providers

    # Serializes on-demand scans. A user clicking Refresh twice, or two browser tabs
    # doing it at once, must not double-poll the same regions -- rate limits are per
    # region and a duplicated scan spends quota to learn nothing.
    scan_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Iterator[None]:
        app.state.notes = list(notes or [])
        app.state.store = SqliteTimeSeriesStore(config.db_path) if owns_store else store

        poller = None
        if poll:
            selected = list(providers) if providers is not None else None
            if selected is None:
                selected, discovered = factory(config)
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
                "history_days": config.history_days,
                "rows": [_row_json(e) for e in entries],
                "notes": request.app.state.notes,
            }
        )

    @app.get("/api/history/{instance_type}")
    def api_history(
        request: Request,
        instance_type: str,
        region: str | None = None,
        days: int = Query(default=0, ge=0, le=90),
        buckets: int = Query(default=0, ge=0, le=1000),
    ) -> JSONResponse:
        now = datetime.now(UTC)
        days = days or config.history_days
        buckets = buckets or config.buckets
        window = TimeRange(now - timedelta(days=days), now)
        records = request.app.state.store.history(
            OfferingFilter(instance_type=instance_type, region=region), window
        )
        if not records:
            raise HTTPException(
                status_code=404,
                detail=f"no observations for {instance_type!r} in the last {days}d",
            )
        series = floor_series(records, window, buckets=buckets)
        return JSONResponse(
            {
                "instance_type": instance_type,
                "region": region,
                "days": days,
                # `null` means not observed. It is not zero and not the previous
                # price; clients must render it as a gap.
                "points": [
                    {"at": p.at.isoformat(), "floor_usd_hr": p.floor_usd_hr}
                    for p in series
                ],
            }
        )

    # Registered only outside snapshot mode: a static file has no server behind it,
    # so a refresh button there would be a control that silently does nothing.
    if not snapshot:

        @app.post("/api/refresh")
        def api_refresh(
            request: Request,
            instance_types: list[str] | None = Body(default=None),
            regions: list[str] | None = Body(default=None),
        ) -> JSONResponse:
            """Scan now, optionally narrowed to specific instance types and regions.

            **This is the one route that contacts AWS.** Every GET reads storage only,
            which is what keeps API quota tied to the schedule rather than to traffic.
            A scan is something you explicitly ask for, so it is a POST -- and the
            read-only guarantee for GETs stays intact and tested.

            Narrowing is about rate limits and latency, not money: EC2 describe calls
            are free, but throttles are per region, so a scan of 2 regions instead of
            17 returns in a fraction of the time and leaves the rest of your quota
            alone. The page sends whatever is currently filtered, so "scan just the
            p5 rows I'm looking at" is one click.
            """
            if not scan_lock.acquire(blocking=False):
                raise HTTPException(
                    status_code=409,
                    detail="a scan is already running; wait for it to finish",
                )
            try:
                scoped = replace(
                    config,
                    regions=tuple(regions) if regions else config.regions,
                    instance_types=(
                        tuple(instance_types) if instance_types else config.instance_types
                    ),
                )
                selected, discovered = factory(scoped)
                if not selected:
                    raise HTTPException(
                        status_code=503,
                        detail="; ".join(discovered) or "no provider is configured",
                    )

                started = datetime.now(UTC)
                report = run_tick(selected, request.app.state.store, now=started)

                # Per-region failures reach the caller for the same reason they reach
                # the page: a region that vanished is not a region with no capacity.
                provider_notes = [n for p in selected for n in getattr(p, "notes", [])]

                return JSONResponse(
                    {
                        "scanned_at": started.isoformat(),
                        "duration_s": round(
                            (datetime.now(UTC) - started).total_seconds(), 2
                        ),
                        "regions": list(scoped.regions) if scoped.regions else "all enabled",
                        "instance_types": len(scoped.instance_types),
                        "fetched": report.fetched,
                        "inserted": report.write.inserted,
                        "extended": report.write.extended,
                        "failures": report.failures,
                        "notes": [*discovered, *provider_notes],
                    }
                )
            finally:
                scan_lock.release()

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Any:
        now = datetime.now(UTC)
        entries = build_entries(request.app.state.store, config, now=now)
        return _TEMPLATES.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "entries": entries,
                "generated_at": now,
                "config": config,
                "notes": request.app.state.notes,
                "regions_seen": sorted({e.row.region for e in entries}),
                "families_seen": sorted({e.row.instance_family for e in entries}),
                "types_seen": sorted({e.row.instance_type for e in entries}),
                "chart_data": json.dumps(
                    series_payload(entries, now=now, config=config), separators=(",", ":")
                ),
                "snapshot": snapshot,
            },
        )

    return app
