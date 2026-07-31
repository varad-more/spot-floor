# spotfloor — design notes

Why the thing is built the way it is. The [README](../README.md) covers how to run it; this is the reasoning, and the measurements behind it.

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

A 4.5× price difference inside one region. A regional average would mislead you
outright. A bare regional minimum wouldn't — but you can't act on it without
knowing which zone produced it. So every row names its zone and states the spread
it hid.

The cross-region spread is just as real: `p5.48xlarge` (8×H100) is $6.60/hr in
ap-south-1 and $20.76/hr in us-east-1 — the same hardware, 3× the price.

---

## What this tool does not know

**AWS does not publish spot availability.** Every availability cell reads
`unknown` — the word, spelled out, because a blank cell reads as "none available"
and that is a claim we have not earned.

The nearest thing AWS offers is the Spot Placement Score API, and it does not
return a market fact: AWS computes the score *against the calling account's* quotas
and usage history. Measured live from this repo's account, `p5.48xlarge` scored
1/10 in every availability zone. That number describes our account. A score
fetched with the app's credentials is **about the wrong account entirely**, and
publishing it as a market signal would be fabrication.

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
seven days. It is not a fulfillment probability, and nothing on the page says
otherwise.

---

## Design decisions worth arguing about

**Spot price history is a change-log, not a sample series.** AWS emits a row
precisely *when a price changes*, and retains ~89 days (measured: 89 days,
~2,000 rows per instance-type/region, 2–3 API calls, under a second). Two
consequences:

* charts are full-depth on a cold start, because deep history is one call away —
  no poller has to accumulate it slowly;
* those rows **are** storage segments — quote N's timestamp opens a segment and
  quote N+1's closes it — so the backfill is exact. Gate 1 proves it: 466
  adjacent segment pairs, every one meeting exactly.

That is why there are two write paths. `write(now=…)` is told "this is the state
now" and infers boundaries; `backfill(segments)` is handed intervals already known.
Routing history through `write` would stamp 90 days of dated quotes with the wall
clock and collapse them into one segment.

**The database is a cache, and every row in it is re-derivable from one API
call.** So a schema change drops and rebuilds. Migrating would risk leaving it
half-converted, which is worse than empty — it serves rows the new code misreads.

**Region and zone are separate fields.** A region comparator cannot key on a field
that secretly holds an AZ. Rolling up is a read-time decision, and the roll-up
always names the zone that produced the number.

**`instance_type` is the spine.** Only 69 of us-east-1's 1,354
instance types carry a GPU. Within one provider `m5.large` is already canonical —
it is the same 2 vCPU / 8 GiB machine in every region — so prices are directly
comparable with no normalization. Cross-provider SKU mapping (`gpu.py`) is
retained as *enrichment* for the GPU rows; it was load-bearing only when comparing
Vast against AWS.

**The scope is every instance type — and the estimate that said otherwise was
wrong.** This section used to argue the watchlist had to be bounded because an
unbounded scan was "~46M rows and ~57k API calls". The call count assumed one
request per (type, region). `DescribeSpotPriceHistory` takes no instance-type filter
at all: it paginates the whole region either way, so an unfiltered sweep is the same
17 paginated calls as a 40-type one. Re-measured live, 2026-07-30:

| | 40 types | every type |
|---|---|---|
| Sweep, 17 regions | 6.7s | **9.2s** |
| API calls | 17 paginated | **17 paginated** |
| Rows (type × region) | 650 | **15,078** |
| On-demand list prices | 40 calls | **1 sweep, 54s, 24,383 pairs** |

The genuine constraint was rendering, not ingestion: 15,078 rows of server-rendered
HTML is a ~36 MB document. That is solved where it lives — the browser renders the
table from a compact embedded dataset — and the watchlist is now unbounded by
default. `DEFAULT_INSTANCE_TYPES` survives as a ready-made short list for a fast
local run.

The cost of getting this wrong was concrete: a page offering `g5.xlarge` and
`g5.12xlarge` but not `g5.2xlarge`, for a reason that had stopped being true.

**Regions are discovered at startup.** `describe_regions` returns the 17 this
account has enabled; the other 17 are opt-in and would raise `AuthFailure` on every
call. A comparator that lists regions it cannot price is worse than one that admits
its scope — and any region that *does* fail is named on the page, because an absent
region is indistinguishable from a region with no capacity.

**Storage is intervals.** A row says "this exact (price, availability) held from
`first_seen` to `last_seen`", so the table grows only when something changes.
Change detection hashes integer-quantized values — comparing JSON floats with `=`
would open a new row on every poll and silently destroy dedup.

**Absence is never rendered as a value.** An unobserved bucket is `None`: not zero,
not the previous price carried forward. Interpolating across a gap asserts an
observation we never made.

---

## The dashboard

A read-only page over the store: one row per (instance type, region), the cheapest
zone named, the intra-region spread, hardware spec, volatility, and a 7-day
sparkline. Filtering, search and column sort run client-side over rows already in
the document — so "pick what you want to see" works on a static host with no
server, no framework and no CDN.

**A page load never fetches from AWS.** It renders what the poller and backfill
already stored, so API quota is tied to the schedule rather than to traffic. The
table and charts become two views of one set of stored facts, instead of two
fetches that can disagree. Enforced by a test that wires in a provider which
raises if called.

```
GET /                           the page
GET /api/market                 current rows as JSON
GET /api/history/{instance_type} bucketed price series (?region=&days=&buckets=)
```

The page charts 7 days while the store holds 30. That is deliberate: 646 rows × 30
days is over a million segments to load and bucket per render. Deeper history stays
available per instance type through `?days=N`, which filters to one series and is
cheap.

### Exporting a static snapshot (optional)

**Nothing about this project is hosted, and there are no credentials in this
repository.** You clone it and run it against your own account. If you want a
shareable static export anyway:

```bash
uv run python scripts/snapshot.py --out site --backfill
```

That writes a self-contained directory — `index.html` plus JSON — you can serve from
anywhere. Filtering and sorting still work in the exported page, because they only
touch rows already in the document. The **Scan now** button is deliberately absent:
there is no server behind a static file, and a control that silently does nothing is
worse than no control.

The renderer drives the real app over ASGI rather than re-rendering, so the static
files are literally the responses the live app gives. There is no second rendering
path to drift, and the first thing to drift would be a caveat.

Snapshot mode is explicit: the page drops its auto-refresh and carries a banner
naming when the prices were read. **A stale page that looks live is the same
unearned claim as an availability we cannot observe**, so tests assert both that a
snapshot says it is one and that the live page does not.

If you want this on a schedule, that is your own CI with your own secrets — this repo
deliberately ships no deploy workflow, because publishing would require putting AWS
credentials into GitHub. `.github/workflows/ci.yml` runs the offline tests only.

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



## The spot floor, and why it is measured rather than stated

The project is named after a market-structure claim: **AWS does not price spot below
10% of on-demand.** A claim like that is exactly the kind this repo refuses to make
on authority, so `floor_stats` computes it on every render from the rows being
rendered, and the page prints those figures beside the prose.

Measured over 15,277 (type, region) pairs, 2026-07-30: minimum ratio **0.099494**,
**zero** rows below the band, **771 (5.0%)** pinned to it, maximum **1.000045**. The
0.10 histogram bucket holds 836 rows against 130-250 in each neighbour — a spike, not
a tail. `sa-east-1` runs 20% of its rows at the floor.

**The tolerance is sized from the data.** Prices are quantized to
micro-dollars, so an exactly-floored row divides to a hair either side of 0.1. The
first attempt (±0.0005) reported the real minimum, 0.099494, as having *broken
through* the floor — undercutting the section's own claim. ±0.001 absorbs the
quantization and still stops well short of the next histogram bucket at 0.11.

Two consequences elsewhere:

* the **at-floor badge** is the most actionable fact on a row — "this price cannot
  fall further where it is" — so it is a label, never a colour alone;
* the page's older line about spot going *above* list is now known to be nearly
  vacuous: the maximum observed ratio is 1.000045, so the negative-savings path is
  float noise rather than a market state. The handling stays (it costs nothing and
  the cap is undocumented), but the page no longer implies it is common.

## Desktop-only, on purpose

This is a 12-column table of 15,000 rows beside a multi-series time chart. There is
no honest phone layout for it: every responsive strategy for a table this wide ends
in hiding columns, and the columns are the point — the zone a price came from, the
spread the regional number hid. So the layout spends the width it is given (1760px)
rather than reserving room for a viewport it will never be used in.
