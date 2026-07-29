# spotfloor — AWS region-wise spot instance tracker

Scope agreed 2026-07-29: **pivot from cross-cloud GPU tracker to AWS-only,
region-wise, all-instance-families spot price comparator.** Region rows with the
cheapest AZ named. Operator-configured watchlist, in-browser filtering.

## Measured before designing (live, read-only, this account)

| Fact | Value | Consequence |
|---|---|---|
| Regions enabled | 17 (17 more not opted in) | opt-in regions throw `AuthFailure`; report, never silently skip |
| Instance types (us-east-1) | 1,354 — only 69 with GPUs | GPU-only covered 5% of the catalog |
| Spot history retention | **89 days**, real | charts are full-depth on the *first* run |
| Per (type, region) | ~2,000 rows, 2–3 calls, <1s | cheap per unit |
| Unbounded (17 × 1,354) | ~46M rows, ~57k calls | non-viable; watchlist must stay bounded |

## Phase A — generalize the model ✅

- [x] `GpuOffering` → `InstanceOffering`: `gpu_model`/`gpu_count` nullable,
      `vcpus`/`memory_gib` added, `price_per_gpu_hr` returns None off-GPU
- [x] Split `region` (us-east-1) from `zone` (us-east-1a) — a region comparator
      cannot key on a field that holds an AZ
- [x] AWS-only makes `instance_type` canonical, so `gpu.py` demotes from
      grouping spine to optional enrichment
- [x] `price_usd_hr` becomes the primary axis (directly comparable across regions)

## Phase B — storage: dated segments ✅

- [x] `OfferingSegment` + `TimeSeriesStore.backfill()` — a write path for quotes
      that carry their *own* timestamps, distinct from `write(now=)`
- [x] Schema version check that rebuilds on mismatch (the DB is a cache now, not
      a system of record — AWS can re-serve 90 days on demand)
- [x] Unique index on `(series_key, first_seen)` so re-backfill is idempotent

## Phase C — provider: region fan-out + deep history ✅

- [x] Enumerate enabled regions via `describe_regions`
- [x] ~~Per-region catalog~~ — **plan was wrong.** Instance *specs* are global
      (`m5.large` is 2 vCPU everywhere); only which types are *offered* varies, and
      that needs no call because an unoffered type returns no price history. One
      catalog fetch, not 17.
- [x] Batch `describe_spot_price_history` over the whole watchlist per call
- [x] Threaded region fan-out (limits are per-region, so parallel is safe)
- [x] `AuthFailure` / opt-in regions surface as notes on the page

## Phase D — region-wise read model ✅

- [x] `region_table()`: one row per `(instance_type, region)`, cheapest AZ named
- [x] Intra-region spread (cheapest vs dearest AZ) — the reason not to roll up blind
- [x] Volatility: price-change count + coefficient of variation over the window

## Phase E — dashboard ✅

- [x] Region comparison table, client-side search / family filter / sort
- [x] Self-contained (no CDN) — CSP on the Artifact/Pages host forbids it

## Phase F — hosting, revised by measurement ✅

- [x] **Deleted the Actions DB cache.** It existed because a poller cannot
      retroactively learn yesterday's price. AWS can: a full 30-day rebuild across
      17 regions measures ~43s and yields 172k segments, so every run reconstructs
      from the API and a 62 MB cache round-trip buys nothing. One whole subsystem
      removed, plus its cache-key management and its "sparklines fill in over the
      first few runs" caveat.
- [x] Cron relaxed hourly → 6-hourly: the chart window comes from AWS's own history,
      so the page is full-depth on every run and frequent polling adds nothing.
- [x] `--backfill` on both `serve.py` and `snapshot.py`

---

## Review

**Result: 153 offline tests pass; gates 0, 1 and 2 pass against live APIs. Verified
end to end — 646 rows across 17 regions and 40 instance types, rendered in 43s.**

**The measurement that justified the whole design.** The open question was whether
rolling a region's zones up to one price loses anything worth showing. Measured live
and then re-verified against the raw AWS API rather than trusted from my own code:
`g6.12xlarge` in ca-central-1 ranges **$1.1336 (1b) to $5.1093 (1d) — a 350.7%
spread inside one region.** `p5.48xlarge` in ap-south-1 spans $6.60 to $20.29. A
regional average would be actively misleading and a bare minimum unactionable, so
every row names its zone and states the spread it hid. The cross-region finding is
just as large: the same 8×H100 box is $6.60/hr in ap-south-1 and $20.76/hr in
us-east-1.

**Two places the plan was wrong, both corrected by evidence.**

1. *Per-region instance catalogs.* I planned 17 catalog fetches because "regions
   differ". They differ in which types they *offer*, not in what a type *is* —
   `m5.large` is 2 vCPU / 8 GiB everywhere. One fetch, and an unoffered type needs
   no call because it simply returns no price history.

2. *The Actions cache.* Carried over from the previous design without re-examining
   its premise. Its whole purpose was preserving history a poller cannot acquire
   retroactively, and AWS hands over 89 days on request. Deleting it removed 62 MB
   of round-trip per run and a caveat about sparklines being thin on early runs.

**The architectural finding.** AWS spot price history is a *change-log*, not a
sample series: a row is emitted precisely when a price changes. That is exactly the
shape of this repo's segment storage, so quote N opens a segment and quote N+1
closes it — boundaries given, not inferred. Gate 1 asserts it on live data: 466
adjacent segment pairs, every one meeting exactly, no gap and no overlap. It also
means the store is now a *rebuildable cache*, which is what licensed the
drop-and-rebuild schema guard instead of migrations.

Consequence: two write paths. `write(now=…)` infers boundaries from "the state now";
`backfill(segments)` takes intervals already known. Routing history through `write`
would stamp 90 days of dated quotes with the wall clock and collapse them into one
segment. `backfill` is idempotent via a unique index on `(series_key, first_seen)`,
which matters because a lost cache means re-backfilling — and a duplicated segment
would show up as a phantom price *move* in the volatility column.

**Two test bugs of mine, both found by running.**

*A wrong boundary assertion.* I asserted three empty buckets after a segment ended;
only two are. The overlap test is `last_seen >= start`, so a segment ending exactly
on a bucket edge counts for that bucket. Production was right and the test was
wrong — tightening the code to `>` would silently drop real observations whenever a
price change landed on a boundary, which with hourly buckets and hourly-ish AWS
quotes is not a rare alignment. Fixed the test and added an explicit boundary test
documenting the rule.

*An order-dependent assertion.* `next(r for r in rows if r["instance_type"] ==
"m5.large")` picked whichever region happened to be cheapest, so the expected value
was right for us-east-1 and the test read us-west-2. Now keyed on
`(instance_type, region)`.

*A fake that lied.* `get_paginator` had `side_effect` returning a fresh MagicMock
per call, so a test inspecting `get_paginator(op).paginate.call_args` examined a
different object than the code had used and got `None`. Memoized per operation.

**Page weight.** 646 rows with inline sparklines is 1,449 KiB raw / **163 KiB
gzipped**. Sparkline buckets went 84 → 56 after noting that 84 points inside a 168px
sparkline is sub-pixel: it renders identically and costs ~12 bytes of coordinate text
per point per row.

**On what was lost.** Dropping Vast removes the only provider that could answer "can
I get it". That is a real reduction in what the product can claim, not a cleanup, and
it is why the page now leads with "this is a price comparator". The Vast provider and
its tests are kept unwired precisely because they document the asymmetry that makes
AWS's `unknown` honest rather than lazy.

---

## Honesty constraints carried over (non-negotiable)

- **Every AWS row is `availability: unknown`, forever.** Dropping Vast removes the
  only provider that could answer "can I get it", so this tool is strictly a
  *price* comparator and the page must say so. Placement Score stays uncalled.
- **Volatility is not availability.** Price-change frequency is a real fact derived
  from published history and a fair contention *proxy*. It must never be labelled
  as a fulfillment signal.
- Absence stays absence: unobserved buckets are `None`, never zero or carried-forward.
- Regions are provider-native; no cross-provider region mapping is invented.

## TODO(scope) — deliberately not built

- True arbitrary selection across all 1,354 types needs a live backend; Pages is
  static, so the watchlist is operator-configured and the browser filters it.
- Vast provider and its tests are kept (they document the availability thesis) but
  are no longer wired into the default app.
- Alert engine remains unwired to the pipeline (was already true pre-pivot).
