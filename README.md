# spotfloor

**AWS EC2 spot price tracker and cross-region comparator.** Any instance family, all
enabled regions, with real price history — and it names the availability zone behind
every number, because you launch into a zone, not into a region.

```bash
uv sync
uv run python scripts/serve.py --backfill    # http://127.0.0.1:8000
```

Status: **AWS-only price comparator.** 153 offline tests; gates 0, 1 and 2 pass
against live APIs.

---

## The one number that justifies the whole design

Rolling a region's zones up to a single price is the obvious thing for a table to
do. Here is what it hides, measured live and verified against the raw AWS API:

| Instance | Region | Cheapest AZ | Dearest AZ | Spread |
|---|---|---|---|---|
| `g6.12xlarge` | ca-central-1 | **$1.1336** (1b) | $5.1093 (1d) | **+350.7%** |
| `g6e.xlarge` | ap-south-1 | $0.5482 (1a) | $2.2352 (1b) | +307.7% |
| `p4d.24xlarge` | eu-west-2 | $7.0745 (2b) | $28.5488 (2a) | +303.5% |
| `p5.48xlarge` | ap-south-1 | $6.6048 (1c) | $20.2923 (1b) | +207.2% |

A 4.5× price difference **inside one region**. A regional average would be
actively misleading, and a bare regional minimum would be a number you cannot act
on. So every row names its zone and states the spread it hid.

The cross-region spread is just as real: `p5.48xlarge` (8×H100) is $6.60/hr in
ap-south-1 and $20.76/hr in us-east-1 — the same hardware, 3× the price.

---

## What this tool does not know

**AWS does not publish spot availability.** Every availability cell reads
`unknown`, deliberately, as a word rather than a blank — a blank cell reads as
"none available", which is a claim we have not earned.

The nearest thing AWS offers is the Spot Placement Score API, and it does not
return a market fact: AWS computes the score *against the calling account's* quotas
and usage history. Measured live from this repo's account, `p5.48xlarge` scored
**1/10 in every availability zone**. That number describes our account, not your
odds. So a score fetched with the app's credentials is not merely imprecise, it is
**about the wrong account** — and publishing it as a market signal would be
fabrication.

spotfloor therefore reports `unknown` and **does not call that API at all** under
app credentials. Enforced by a test
(`test_app_creds_never_call_the_placement_score_api`), not by a comment. The only
honest path to a real AWS availability signal is to compute it with *your own*
credentials (`CredsOwner.USER`), which is off by default.

**This is a price comparator. It does not claim to know what you can get.**

### Price volatility is not availability either

The page shows how many times each price moved in the window, plus a scale-free
coefficient of variation. That is a real fact from AWS's published history and a
fair hint that a zone is contended — `m5.large` in us-west-2 moved 74 times in
seven days. It is **not** a fulfillment probability and is never presented as one.

---

## Design decisions worth arguing about

**Spot price history is a change-log, not a sample series.** AWS emits a row
precisely *when a price changes*, and retains ~89 days (measured: 89 days,
~2,000 rows per instance-type/region, 2–3 API calls, under a second). Two
consequences:

* charts are full-depth on a cold start, because deep history is one call away
  rather than something a poller must slowly accumulate; and
* those rows **are** storage segments — quote N's timestamp opens a segment and
  quote N+1's closes it — so the backfill is exact rather than a reconstruction.
  Gate 1 proves it: 466 adjacent segment pairs, every one meeting exactly.

That is why there are two write paths. `write(now=…)` is told "this is the state
now" and infers boundaries; `backfill(segments)` is handed intervals already known.
Routing history through `write` would stamp 90 days of dated quotes with the wall
clock and collapse them into one segment.

**The database is a rebuildable cache, not a system of record.** Every row is
re-derivable from one API call, so a schema change drops and rebuilds rather than
migrating — a half-migrated cache is worse than an empty one, because it serves
rows the new code misreads.

**Region and zone are separate fields.** A region comparator cannot key on a field
that secretly holds an AZ. Rolling up is a read-time decision, and the roll-up
always names the zone that produced the number.

**`instance_type` is the spine, not GPU SKU.** Only 69 of us-east-1's 1,354
instance types carry a GPU. Within one provider `m5.large` is already canonical —
it is the same 2 vCPU / 8 GiB machine in every region — so prices are directly
comparable with no normalization. Cross-provider SKU mapping (`gpu.py`) is
retained as *enrichment* for the GPU rows; it was load-bearing only when comparing
Vast against AWS.

**The watchlist is bounded, on purpose.** 17 regions × 1,354 types × ~2,000 history
rows is ~46M rows and ~57k API calls — neither pollable on a schedule nor
publishable as a static page. The default watchlist is 40 types across GPU,
general-purpose, compute, memory, burstable and storage families.

**Regions are discovered, not hardcoded.** `describe_regions` returns the 17 this
account has enabled; the other 17 are opt-in and would raise `AuthFailure` on every
call. A comparator that lists regions it cannot price is worse than one that admits
its scope — and any region that *does* fail is named on the page, because an absent
region is indistinguishable from a region with no capacity.

**Storage is segments, not points.** A row says "this exact (price, availability)
held from `first_seen` to `last_seen`", so the table grows with *change*, not with
time. Change detection hashes integer-quantized values — comparing JSON floats with
`=` would open a new row on every poll and silently destroy dedup.

**Absence is never rendered as a value.** An unobserved bucket is `None`: not zero,
not the previous price carried forward. Interpolating across a gap asserts an
observation we never made.

---

## The dashboard

A read-only page over the store: one row per (instance type, region), the cheapest
zone named, the intra-region spread, hardware spec, volatility, and a 7-day
sparkline. Filtering, search and column sort run **client-side** over rows already
in the document — so "pick what you want to see" works on a static host with no
server, no framework and no CDN.

**A page load never fetches from AWS.** It renders what the poller and backfill
already stored, so API quota is tied to the schedule rather than to traffic, and
the table and charts are two views of one set of stored facts instead of two
fetches that can disagree. Enforced by a test that wires in a provider which raises
if called.

```
GET /                           the page
GET /api/market                 current rows as JSON
GET /api/history/{instance_type} bucketed price series (?region=&days=&buckets=)
```

The page charts 7 days while the store holds 30. That is deliberate: 646 rows × 30
days is over a million segments to load and bucket per render. Deeper history stays
available per instance type through `?days=N`, which filters to one series and is
cheap.

### The hosted page is a snapshot, and says so

GitHub Pages is a static host: it cannot run the poller, the store, or FastAPI.
What is published there is a **snapshot** — a scheduled job backfills history, runs
a real poll, renders the page, and deploys it every 6 hours.

```bash
uv run python scripts/snapshot.py --out site --backfill
```

The renderer drives the real app over ASGI rather than re-rendering, so the static
files are literally the responses the live app gives. There is no second rendering
path to drift, and the first thing to drift would be a caveat.

Snapshot mode is explicit, not inferred: the page drops its auto-refresh and carries
a banner naming when the prices were read. **A stale page that looks live is the
same unearned claim as an availability we cannot observe**, so tests assert both
that a snapshot says it is one and that the live page does not.

**There is no database cache in CI**, and that is a deliberate deletion. An earlier
design kept the SQLite file in the Actions cache so sparklines could span runs —
necessary when a poller is the only source of history, because a poller cannot
retroactively learn yesterday's price. AWS is not that. A full 30-day rebuild across
17 regions measures **~43 seconds** and produces 172k segments, so each run
reconstructs everything from the API and a 62 MB cache round-trip buys nothing.

The workflow needs `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` as repository
secrets, scoped to a dedicated IAM user with only `ec2:DescribeSpotPriceHistory`,
`ec2:DescribeInstanceTypes` and `ec2:DescribeRegions`. Without them the page still
deploys and states that AWS is not configured rather than rendering an empty table.
Note that GitHub disables scheduled workflows after 60 days without repository
activity.

---

## The alert engine

Present, proven, and **not yet wired to the pipeline** — `run_tick` fetches and
persists; nothing calls `evaluate()`, and `RuleState` is not persisted.

The correctness problem it solves: a price sitting at its floor jitters across the
threshold on every poll, and a naive threshold check mails you every time. Every
rule reduces to one scalar — the floor price among offerings that qualify — so one
state machine covers both price and availability flapping. A rule that fires at
`price ≤ T` re-arms only once price clears `T × (1 + margin)`, after N consecutive
confirmations. Between them is a dead zone where nothing is emitted.

On a 207-tick series straddling the threshold 200 times:

```
spotfloor (hysteresis):   2 alerts
naive threshold check : 112 alerts
```

`step()` is pure — no I/O, no clock, no database — which is what makes the gate
provable. A zero-deadband rule is rejected at construction, because it would flap
forever.

**No LLM touches ingestion, normalization or alert evaluation.** That constraint is
architectural, so it is also a test (`test_no_llm_in_the_critical_path`).

---

## Layout

```
src/spotfloor/
  models.py            InstanceOffering, PriceKind, Availability, series identity
  gpu.py               GPU SKU vocabulary (enrichment for the GPU rows)
  query.py             read model: region_table(), volatility(), floor_series() -- pure
  providers/
    base.py            Provider protocol
    aws.py             region fan-out, history-as-segments, availability = unknown
    vast.py            live inventory (kept, no longer wired -- see below)
  storage/
    base.py            TimeSeriesStore protocol (the DuckDB seam)
    sqlite.py          segment storage, dedup, backfill, schema guard
  ingest/
    pipeline.py        one tick: fetch -> persist
    poller.py          scheduled polling
  alerts/
    rules.py           AlertRule, RuleState
    evaluator.py       pure step(); hysteresis
  web/
    app.py             FastAPI routes; read-only
    sparkline.py       inline SVG; gaps stay gaps
    templates/         the page, with client-side filtering
```

Business logic depends only on the storage *protocol*, never on SQL — so a
DuckDB/Parquet backend can replace SQLite for range queries without the pipeline,
evaluator or API noticing.

The Vast provider and its tests are **kept but unwired**. It is the only provider
that can answer "can I actually get this", and it documents the asymmetry that
makes AWS's `unknown` honest rather than lazy. Re-wiring it is a one-line change in
`build_providers`.

---

## Running it

```bash
uv sync

uv run python scripts/serve.py --backfill   # dashboard, full-depth charts
uv run python scripts/serve.py              # faster start, charts fill in per poll

uv run pytest                     # full suite (includes live gates)
uv run pytest -m "not live"       # offline only

uv run python scripts/gate0.py    # Vast availability rule + live normalized offerings
uv run python scripts/gate1.py    # region fan-out, dedup, backfill-as-segments, AWS honesty
uv run python scripts/gate2.py    # hysteresis vs. a naive evaluator
```

Environment: `SPOTFLOOR_DB`, `SPOTFLOOR_REGIONS` (unset = every enabled region),
`SPOTFLOOR_INSTANCE_TYPES`, `SPOTFLOOR_POLL_INTERVAL_S`, `SPOTFLOOR_HISTORY_DAYS`,
`SPOTFLOOR_BACKFILL_DAYS`, `SPOTFLOOR_BUCKETS`, `SPOTFLOOR_PORT`.

AWS credentials need `ec2:DescribeSpotPriceHistory`, `ec2:DescribeInstanceTypes`
and `ec2:DescribeRegions`. All three are free, read-only calls.

## Not built

- **Arbitrary instance selection** across all 1,354 types needs a live backend;
  Pages is static, so the watchlist is operator-configured and the browser filters
  what was fetched.
- **On-demand prices** — a different API. Only spot (`Linux/UNIX`) is shown.
- Alert delivery (email/Slack), auth, per-user rules, billing. The provider,
  storage and alert interfaces are in place for the rest.
- `floor_series` buckets in Python over segments fetched for the whole window. Fine
  at this row count; a range aggregate pushed into SQL is the move when the window
  gets long.
