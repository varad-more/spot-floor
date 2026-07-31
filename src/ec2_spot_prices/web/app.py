"""FastAPI dashboard: AWS spot prices compared across regions, with history.

Read-only by design. The page renders what the poller and the backfill already
stored; a page load never triggers a provider fetch. That keeps rendering fast,
keeps API quota tied to the poll schedule rather than to traffic, and means the
table and the charts are two views of one set of stored facts rather than two
independent fetches that can disagree.

**The table is rendered by the browser, not by Jinja.** The scope is every instance
type EC2 offers, which is ~15,000 (type, region) rows; server-rendering those is a
~36 MB document and roughly a million DOM nodes, so the page ships a compact dataset
(:func:`table_payload`) and paints a page of rows at a time. Filtering and sorting
still run over every row -- only painting is capped, and the page says so rather than
truncating quietly.

That moves two things client-side that used to be assertable Python: the row markup
and the sparkline. Both keep their rules, and the tests follow them there -- what the
page *knows* is asserted against the payload, what it *does* against the rendering
code.

The template's job is to render :mod:`ec2_spot_prices.query` output faithfully. Four
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
15,000 rows x 30 days is tens of millions of segments to load and bucket per render.
Deeper history stays available per instance type through
``/api/history/{instance_type}?days=N``, which filters to one series and is cheap.

At full catalogue scope a 30-day backfill is ~7M segments and a multi-gigabyte
database; 7 days is ~1.7M and matches what the page actually draws. Set
``EC2_SPOT_PRICES_BACKFILL_DAYS`` deliberately rather than inheriting 30 by accident.
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

from ec2_spot_prices.ingest.pipeline import run_tick
from ec2_spot_prices.ingest.poller import Poller
from ec2_spot_prices.models import PriceKind
from ec2_spot_prices.providers.aws import DEFAULT_INSTANCE_TYPES
from ec2_spot_prices.providers.base import Provider
from ec2_spot_prices.query import FloorPoint, RegionRow, floor_series, region_table
from ec2_spot_prices.storage.base import OfferingFilter, OfferingRecord, TimeRange, TimeSeriesStore
from ec2_spot_prices.storage.sqlite import SqliteTimeSeriesStore
from ec2_spot_prices.web.sparkline import sparkline_svg

logger = logging.getLogger(__name__)

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# The exact note emitted when boto3 resolves no credentials. A constant rather than
# a string the page greps for, because the page turns it into a modal that tells the
# reader how to fix it -- and "did we mean *this* note" must not depend on wording.
NO_CREDENTIALS_NOTE = (
    "AWS is not configured (no credentials found), so no prices can be shown. "
    "Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY, or run `aws configure`."
)


@dataclass(frozen=True, slots=True)
class WebConfig:
    db_path: str = "ec2-spot-prices.db"
    # None means "every region this account has enabled", discovered at runtime.
    regions: tuple[str, ...] | None = None
    # None means "every instance type EC2 offers", discovered the same way. It is
    # the default because the alternative -- a curated list -- is wrong the moment
    # someone looks for a type nobody thought to curate, which is exactly how
    # g5.2xlarge came to be missing from a page that showed g5.xlarge and
    # g5.12xlarge. Narrow it with EC2_SPOT_PRICES_INSTANCE_TYPES when you want a small,
    # fast local run; see DEFAULT_INSTANCE_TYPES for a ready-made short list.
    instance_types: tuple[str, ...] | None = None
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

        if db := os.getenv("EC2_SPOT_PRICES_DB"):
            overrides["db_path"] = db
        csv("EC2_SPOT_PRICES_REGIONS", "regions")
        csv("EC2_SPOT_PRICES_INSTANCE_TYPES", "instance_types")
        integer("EC2_SPOT_PRICES_POLL_INTERVAL_S", "poll_interval_s")
        integer("EC2_SPOT_PRICES_HISTORY_DAYS", "history_days")
        integer("EC2_SPOT_PRICES_BACKFILL_DAYS", "backfill_days")
        integer("EC2_SPOT_PRICES_BUCKETS", "buckets")

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
            notes.append(NO_CREDENTIALS_NOTE)
        else:
            from ec2_spot_prices.providers.aws import AwsProvider, CredsOwner

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
    """One region row plus its price history over the page window."""

    row: RegionRow
    series: list[FloorPoint]

    @property
    def spark(self) -> str:
        """This row's history as inline SVG.

        A property rather than a stored field since the page went client-rendered:
        building 15,078 sparklines to embed in HTML that no longer carries them cost
        seconds per render and ~15 MB of strings for nothing. The API is kept because
        the drawing is still a pure function worth asserting on, and callers outside
        the page (tests, any consumer wanting server-rendered SVG) still want it.
        """
        return sparkline_svg([p.floor_usd_hr for p in self.series])

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

    **History is spot-only; ``latest`` is not.** The current state has to include
    on-demand, because that is the row's comparison column. The *history* must not:
    the grouping key is (type, region), so an on-demand segment folded in here would
    be counted as a spot price change and inflate the volatility column with moves
    that never happened. On-demand has no published history anyway -- its stored
    series accumulates forward from the first poll and is served, unmixed, by
    ``/api/history``.
    """
    window = TimeRange(now - timedelta(days=config.history_days), now)

    grouped: dict[tuple[str, str], list[OfferingRecord]] = {}
    for record in store.history(OfferingFilter(price_kind=PriceKind.SPOT), window):
        grouped.setdefault(_series_key(record), []).append(record)

    rows = region_table(store.latest(OfferingFilter(), now=now), history=grouped)

    entries = []
    for row in rows:
        series = floor_series(
            grouped.get((row.instance_type, row.region), []),
            window,
            buckets=config.buckets,
        )
        entries.append(TableEntry(row=row, series=series))
    return entries


# The floor this project is named after: AWS will not sell a spot instance for less
# than a tenth of its on-demand list price. It is not documented as a hard rule so
# much as observable as one, which is why `floor_stats` measures it on every render
# instead of asserting it -- see that function.
SPOT_FLOOR_RATIO = 0.10

# Prices arrive quantized to micro-dollars, so a row sitting exactly on the floor
# lands a hair either side of 0.1 after division. Sized from the measurement, not
# guessed: the lowest ratio observed across 15,277 real pairs was 0.099494, so a
# tighter band (0.0005 was the first attempt) reports genuine floor rows as having
# broken through it. Still nowhere near wide enough to sweep in a row that is merely
# cheap -- the band tops out at 0.101 and the next histogram bucket starts at 0.11.
_FLOOR_TOLERANCE = 0.001


def floor_stats(entries: Sequence[TableEntry]) -> dict[str, Any]:
    """How hard the spot floor is biting, measured on the rows being rendered.

    **Measured, never asserted.** The page makes a claim about market structure --
    "spot cannot go below 10% of on-demand" -- and a claim like that has to be
    computed from the data it is printed next to, or it is folklore with a number
    attached. If AWS changes the rule, these figures move and the prose stays true;
    a hardcoded "90% off!" would quietly become a lie.

    Measured over 15,277 (type, region) pairs on 2026-07-30: the minimum ratio was
    0.0995, exactly three rows fell below 0.0999 (float noise on a micro-dollar
    quantization), 836 sat in the 0.10 bucket against ~130-250 in each neighbouring
    one, and the maximum was 1.000045 -- so the distribution has a hard wall at one
    tenth and a ceiling at parity.
    """
    ratios = [
        (e.row.cheapest_usd_hr / e.row.on_demand_usd_hr, e.row)
        for e in entries
        if e.row.on_demand_usd_hr
    ]
    if not ratios:
        return {"priced": 0, "at_floor": 0, "at_floor_pct": 0.0}

    at_floor = [row for ratio, row in ratios if ratio <= SPOT_FLOOR_RATIO + _FLOOR_TOLERANCE]
    by_region: dict[str, int] = {}
    for row in at_floor:
        by_region[row.region] = by_region.get(row.region, 0) + 1
    totals: dict[str, int] = {}
    for _ratio, row in ratios:
        totals[row.region] = totals.get(row.region, 0) + 1

    return {
        "priced": len(ratios),
        "at_floor": len(at_floor),
        "at_floor_pct": 100 * len(at_floor) / len(ratios),
        "min_ratio": min(r for r, _ in ratios),
        "max_ratio": max(r for r, _ in ratios),
        "below_floor": sum(1 for r, _ in ratios if r < SPOT_FLOOR_RATIO - _FLOOR_TOLERANCE),
        "above_list": sum(1 for r, _ in ratios if r > 1 + _FLOOR_TOLERANCE),
        # Where it bites hardest, as a share of each region's own rows -- a raw count
        # would just rank regions by how many instance types they offer.
        "regions": sorted(
            (
                {"region": region, "at_floor": n, "of": totals[region],
                 "pct": 100 * n / totals[region]}
                for region, n in by_region.items()
                if totals.get(region)
            ),
            key=lambda d: d["pct"],
            reverse=True,
        )[:5],
        "example": min(at_floor, key=lambda r: -r.on_demand_usd_hr) if at_floor else None,
    }


def _rle(values: Sequence[float | None]) -> str:
    """Run-length encode a bucketed price series into one short string.

    Spot price history is a *change-log*: AWS emits a row when the price moves, so a
    56-bucket series is typically a handful of long runs of one repeated number.
    Writing them out individually is what made the payload unaffordable at full
    catalogue size -- 15,078 rows x 56 buckets is 844k numbers.

    Format: comma-separated tokens, each a run.

    ==================  ===================================
    ``0.0612``          one bucket at $0.0612
    ``0.0612:12``       twelve consecutive buckets at $0.0612
    ``:3``              three consecutive unobserved buckets
    ``:1``              one unobserved bucket
    ==================  ===================================

    The count is omitted for a run of one because most runs *are* one: measured over
    a real 7-day window, a fixed ``value:count`` shape cost 204 bytes per row against
    109 for this one, and at full catalogue size that difference is ~1.4 MB of page.

    **A gap always carries its count, even at length one.** Letting it degrade to an
    empty token would make ``_rle([None])`` and ``_rle([])`` both the empty string,
    and "one bucket we did not observe" is a different claim from "no window at all".

    The gap survives the encoding as a gap. It is not zero and not the previous price
    carried forward -- the same rule the sparkline and the chart both draw by.
    """
    parts: list[str] = []
    run_value: float | None = None
    run_length = 0

    def flush() -> None:
        if not run_length:
            return
        if run_value is None:
            parts.append(f":{run_length}")
        else:
            head = f"{run_value:g}"
            parts.append(head if run_length == 1 else f"{head}:{run_length}")

    for value in values:
        rounded = None if value is None else round(value, 6)
        if run_length and rounded == run_value:
            run_length += 1
            continue
        flush()
        run_value, run_length = rounded, 1
    flush()
    return ",".join(parts)


def table_payload(
    entries: Sequence[TableEntry], *, now: datetime, config: WebConfig
) -> dict[str, Any]:
    """Every row the table can show, compact enough to hand a browser in one go.

    **The page stopped server-rendering rows because it had to.** One row is ~2.4 KiB
    of HTML including its inline sparkline; at the full EC2 catalogue that is a 36 MB
    document and roughly a million DOM nodes, which no browser renders usefully. So
    the server ships data and the client renders the slice that is on screen.

    Compact by construction, in three ways that together turn ~40 MB into ~2 MB:

    * **hardware specs are per *type*, not per row.** ``g5.2xlarge`` is 8 vCPU / 1
      A10G in all 17 regions, so the spec is emitted once and referenced by index.
    * **type, region and zone names are interned.** ``us-east-1a`` appears in
      thousands of rows; here it appears once.
    * **derived columns are not shipped at all.** AZ spread, savings and $/GPU are
      arithmetic on numbers already present, so sending them would be sending the
      same fact twice and inviting the two copies to disagree.

    History rides along RLE-encoded (see :func:`_rle`) rather than as a separate
    fetch, because the static snapshot has no server to fetch from and the chart must
    work offline once loaded.
    """
    type_index: dict[str, int] = {}
    specs: list[list[Any]] = []
    region_index: dict[str, int] = {}
    regions: list[str] = []
    zone_index: dict[str, int] = {}
    zones: list[str] = []

    def intern_type(row: RegionRow) -> int:
        if row.instance_type not in type_index:
            type_index[row.instance_type] = len(specs)
            specs.append(
                [
                    row.instance_type,
                    row.instance_family,
                    row.vcpus,
                    row.memory_gib,
                    row.gpu_model,
                    row.gpu_count,
                ]
            )
        return type_index[row.instance_type]

    def intern(value: str | None, index: dict[str, int], pool: list[str]) -> int:
        # -1, not 0, for absent: an on-demand row genuinely has no zone, and index 0
        # is a real zone that would otherwise be named as its location.
        if value is None:
            return -1
        if value not in index:
            index[value] = len(pool)
            pool.append(value)
        return index[value]

    rows: list[list[Any]] = []
    for entry in entries:
        row = entry.row
        rows.append(
            [
                intern_type(row),
                intern(row.region, region_index, regions),
                round(row.cheapest_usd_hr, 6),
                intern(row.cheapest_zone, zone_index, zones),
                row.zone_count,
                intern(row.dearest_zone, zone_index, zones),
                round(row.dearest_usd_hr, 6),
                None if row.on_demand_usd_hr is None else round(row.on_demand_usd_hr, 6),
                row.price_changes,
                None
                if row.coefficient_of_variation is None
                else round(row.coefficient_of_variation, 4),
                _rle([p.floor_usd_hr for p in entry.series]),
                str(row.price_kind),
            ]
        )

    return {
        "start": (now - timedelta(days=config.history_days)).isoformat(),
        "step_s": int(
            timedelta(days=config.history_days).total_seconds() / config.buckets
        ),
        "buckets": config.buckets,
        "specs": specs,
        "regions": regions,
        "zones": zones,
        "rows": rows,
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
        # `null` means the on-demand price was not observed (no pricing:GetProducts,
        # or a region AWS quotes in a currency other than USD). It is not zero, and
        # a null savings figure is not "spot saves you nothing".
        "on_demand_usd_hr": row.on_demand_usd_hr,
        "savings_pct": None if row.savings_pct is None else round(row.savings_pct, 2),
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
        # Only ever set to False by an actual failed assembly below. A caller that
        # opted out of polling has not told us anything about AWS, and guessing
        # "broken" there would nag every snapshot render and every test.
        app.state.aws_ready = True

        poller = None
        if poll:
            selected = list(providers) if providers is not None else None
            if selected is None:
                selected, discovered = factory(config)
                app.state.notes = [*app.state.notes, *discovered]
            app.state.aws_ready = bool(selected)
            poller = Poller(selected, app.state.store, interval_s=config.poll_interval_s)
            poller.start()

        try:
            yield
        finally:
            if poller is not None:
                poller.stop()
            if owns_store:
                app.state.store.close()

    app = FastAPI(title="ec2_spot_prices", lifespan=lifespan)

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

        @app.post("/api/catalog")
        def api_catalog(request: Request) -> JSONResponse:
            """Everything the scan picker can offer: every instance type, every region.

            **A POST for something that reads.** It has to be, because it asks AWS --
            ``DescribeInstanceTypes`` unfiltered is the only honest source for "which
            instance types exist", and this project's read-only guarantee is that
            *every GET touches storage only*. Hardcoding a 1,354-entry list to keep it
            a GET would be the same mistake as hardcoding the region list: a table
            that is wrong the moment AWS ships a new family.

            Memoized on app state after the first call (~1.9s), because the answer
            changes when AWS launches hardware, not between two clicks.

            Degrades rather than fails: with no credentials, or no permission, the
            picker still opens offering what the store has already seen, plus a note
            saying the full catalog was unreachable.
            """
            cached = getattr(request.app.state, "catalog", None)
            if cached is not None:
                return JSONResponse(cached)

            now = datetime.now(UTC)
            observed = build_entries(request.app.state.store, config, now=now)
            # An all-types watchlist has no list to union in or to echo back: what
            # is stored *is* the watchlist, and `sorted(None)` would take the picker
            # down with a TypeError on the one path that exists to keep it alive.
            watchlist = set(config.instance_types or ())
            payload: dict[str, Any] = {
                "instance_types": sorted(
                    {e.row.instance_type for e in observed} | watchlist
                ),
                "regions": sorted({e.row.region for e in observed}),
                "watchlist": sorted(watchlist),
                "complete": False,
                "note": "",
            }

            selected, discovered = factory(config)
            provider = next(
                (p for p in selected if hasattr(p, "full_catalog")), None
            )
            if provider is None:
                payload["note"] = (
                    "; ".join(discovered)
                    or "No AWS provider is configured, so only types already stored are listed."
                )
                return JSONResponse(payload)

            try:
                payload["instance_types"] = sorted(provider.full_catalog())
                payload["regions"] = sorted(provider.regions())
                payload["complete"] = True
            except Exception as exc:  # noqa: BLE001 - a shorter list, not a dead picker
                logger.warning("catalog unavailable: %s", exc)
                payload["note"] = (
                    f"The full instance-type catalog could not be read ({exc}), so this "
                    "list is only what has already been stored."
                )
                return JSONResponse(payload)

            request.app.state.catalog = payload
            return JSONResponse(payload)

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
                        "instance_types": (
                            len(scoped.instance_types)
                            if scoped.instance_types is not None
                            else "all"
                        ),
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
                # Drives a modal rather than another line of prose. With no
                # credentials every table on the page is empty, and an empty table
                # reads as "there is nothing to show" rather than "you have not
                # finished setting this up" -- which is the single most common
                # first-run failure.
                #
                # Two signals, because there are two ways to arrive here and the
                # advice is the same for both: boto3 resolved no key at all, or the
                # provider could not be assembled (a profile that does not exist, a
                # malformed config). Matching only the first would leave the second
                # staring at the same empty table with no prompt.
                "needs_credentials": (
                    NO_CREDENTIALS_NOTE in request.app.state.notes
                    or not getattr(request.app.state, "aws_ready", True)
                ),
                "row_count": len(entries),
                "floor": floor_stats(entries),
                "floor_ratio_pct": int(SPOT_FLOOR_RATIO * 100),
                "floor_ratio": SPOT_FLOOR_RATIO,
                # The client's "At floor" toggle filters on this, so it is rendered
                # from the same constant `floor_stats` counts with rather than
                # retyped in JavaScript. The two used to be independent literals
                # (0.10 + 0.001 here, 0.101 there): they agreed by coincidence, and
                # editing the tolerance would have left the floor card reporting one
                # count while the toggle showed a different set of rows.
                "floor_threshold": SPOT_FLOOR_RATIO + _FLOOR_TOLERANCE,
                "region_count": len({e.row.region for e in entries}),
                "type_count": len({e.row.instance_type for e in entries}),
                "gpu_type_count": len(
                    {e.row.instance_type for e in entries if e.row.gpu_count}
                ),
                # One payload, not a list of rows plus a separate series blob: the
                # table and the chart are two views of the same facts, and shipping
                # them separately is how they drift apart.
                # `<` escaped because this lands inside a <script> element, where
                # the parser looks for `</script>` before it looks for JSON. Every
                # string in here is an AWS identifier ([a-z0-9.-]) so nothing can
                # carry that sequence today -- escaped anyway, for the same reason
                # the client's `esc()` escapes row text rather than trusting the
                # API's character set to stay as it is.
                "table_data": json.dumps(
                    table_payload(entries, now=now, config=config),
                    separators=(",", ":"),
                ).replace("<", "\\u003c"),
                "snapshot": snapshot,
            },
        )

    return app
