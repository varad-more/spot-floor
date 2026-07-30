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

## Phase H — UI: multi-select, resizable table, multi-series chart ✅

- [x] Multi-select with autocomplete for instance types and regions (chips +
      filtered dropdown), replacing the single free-text search box
- [x] Table scroller owns both axes (`max-height: 68vh; overflow: auto`) so the
      sticky header survives vertical scrolling
- [x] Resizable columns — pointer-capture grips; `table-layout: fixed` is what makes
      the drag behave instead of reflowing every other column
- [x] **Multi-series line chart**: tick rows to plot, or ⇄ on a row to compare that
      instance across every region — the question the tool exists to answer
- [x] Legend with per-series toggles, crosshair + tooltip, gaps drawn as breaks
- [x] Series data embedded in the page (shared time axis, 6-sig-fig prices) so the
      chart works on the static export, which has no server to fetch from
- [x] README restructured: practical guide only; rationale moved to `docs/DESIGN.md`
- [x] `public/` committed as the Pages sample + footer crediting Varad More

**Chart colours were computed, not chosen.** Ran the categorical palette through the
validator in both modes rather than eyeballing: light (surface `#ffffff`) passes the
lightness band, chroma floor, CVD adjacent ΔE 9.1 and normal-vision ΔE 19.6, with a
contrast WARN on three slots — which obligates relief, satisfied by text legend labels
in ink plus the table view. Dark (surface `#1d1f23`) passes all six including contrast.
Eight slots, assigned in fixed order and never cycled: the plot control disables at 8
rather than inventing a 9th hue.

**A tick-algorithm bug found by testing the pure helpers in Node.** `niceTicks` took
the first nice step wide enough, so a 6.6–20.3 range asking for 5 gridlines rendered
only 3 — the nice-number set skips 2.5 → 5. Now picks the step whose tick *count* is
closest to the target; six representative ranges all yield 4–6 gridlines.

**A test that conflated linking with loading.** `test_the_page_loads_nothing_from_a_
third_party` matched any `href="https://"`, so the footer's repo link failed it. A
strict CSP constrains what a page *fetches*, not what it links to, so the test now
checks subresource elements specifically (`<script src>`, `<link href>`, `@import`,
`url(http…)`) and separately asserts every external URL sits on an `<a>`.

## Phase G — local-first: on-demand scanning + setup ✅

Decided 2026-07-29: **no credentials in GitHub.** Users clone and run against their
own account, so the hosted deploy is deleted rather than secured.

- [x] `POST /api/refresh` — the only route that contacts AWS; every GET stays
      storage-only and a test still enforces it
- [x] **Scan now** button scanning exactly the rows currently filtered, so "only
      these instances" is one click
- [x] Concurrent scans refused with 409 rather than doubled (quota is per region)
- [x] `scripts/scan.py --types --regions --backfill --show` for cron/terminal
- [x] `scripts/check_setup.py` preflight: creds, validity, each IAM action, live price
- [x] `docs/iam-policy.json` — the three read-only actions
- [x] Deleted `pages.yml`, added `ci.yml` (offline tests + entry-point smoke test)
- [x] Catalog fetched with a server-side `Filters` entry: 3.52s → 1.85s, and an
      unknown watchlist entry is unmatched rather than fatal
- [x] Quieted botocore's per-client "Found credentials" INFO spam (~40 lines/run)

**Corrected a false premise rather than building around it.** The request was to
scope scans "so it doesn't increase bills". EC2 describe calls are **free** — AWS does
not bill per request, so a 17-region scan costs $0.00. What scoping actually saves is
wall-clock time and per-region rate quota. Built it anyway (2.6s scoped vs 6.7s full),
with the real reason documented so nobody optimizes against an imaginary cost.

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

## Phase I — UI/UX pass: say what things mean, and make clicks land ✅

Triggered by three reports: *"what is GPU/HR & Availability?"*, *"the chart tooltip
looks too huge and weird"*, and *"there are too many minute errors in use"*. Then a
second batch: a modal for the 7-day chart, and per-instance scanning.

- [x] **`Avail.` column deleted.** 646 identical `unknown` pills taught the reader
      nothing and cost 88px. The claim now sits once in the header, always visible,
      linking to the long version — which is where it used to be *collapsed*, and that
      is precisely why the meaning was unknown.
- [x] Every derived column carries a `?` with a native `title`: `$/hr`, `$ per GPU`
      (renamed from `$/GPU·hr`), `Cheapest zone`, `AZ spread`, `Price moves`, `Spec`
- [x] **Tooltip rebuilt** — was a `<table>` at .8rem with `min-width: 11rem` and a
      20-char ISO timestamp. Now flex rows at .72rem with `30 Jul, 14:00 UTC`.
      Measured in Chrome: **221×79px for 3 series.**
- [x] `pointermove`/`pointerleave` instead of mouse events, so a finger scrubs the chart
- [x] Whole row clickable to plot (a 13px checkbox was the primary action's hit target)
- [x] Light/dark toggle; theme read in `<head>` so there is no wrong-theme flash, and a
      `sf-theme` event so the chart *redraws* — series colours are resolved to hex at
      draw time, so swapping CSS variables alone would leave the old palette
- [x] Keyboard: sortable headers focusable + Enter/Space, `aria-sort`, arrow keys and
      Backspace in the multi-selects
- [x] Empty states that explain: no rows match, every series hidden, nothing plotted
- [x] **Zoom modal** — native `<dialog>` + `showModal()`, so Escape, backdrop dismissal,
      focus containment and the top layer are platform behaviour. `host()`/`legendHost()`/
      `chartHeight()` switch destination so it is *one* renderer at two sizes, never a
      second implementation that could drift. `⤢ Enlarge` keeps the current selection;
      a sparkline click replaces it with that row.
- [x] **Per-row `⟳` rescan** — one type, one region, **2.3s** measured live against
      6.7s for the full watchlist. Needed no backend change: `POST /api/refresh` already
      narrowed via `dataclasses.replace()` on the frozen config.
- [x] **The scan button states its scope before the click** — `Scan 40×17`, recomputed
      from the visible rows on every filter change, and disabled with
      "Nothing is shown to scan" when the filters exclude everything.

**Four real bugs, three of which only a browser could find.**

1. *`$ per GPU` sorted 544 dashes ahead of every real value.* `-1` is the
   "not applicable" sentinel and the comparator treated it as a small number. Guarded
   with `(av < 0) !== (bv < 0)`; a test asserts the guard's source is still there.
2. *Clicking a column header did nothing.* The listener was on the inner `.cell` span,
   so the cell's padding was dead — while `th.sortable { cursor: pointer }` promised
   otherwise. Found because a scripted `th.click()` left `aria-sort="none"`.
3. *And finishing a resize drag re-sorted the column.* Same root cause from the other
   side: the grip lives inside that span, and pointerdown+pointerup on it fire a click.
   The listener moved to the `th` with the grip excluded — one fix for both.
4. *"Enlarge" produced a chart 268px **narrower** than the one it enlarged.*
   `width: min(1080px, 94vw)` against a page column of 1400px. Measured 1043px in the
   modal vs 1311px inline; now `min(1600px, 97vw)`.

**Verified in a real browser, without the extension.** The Chrome extension would not
connect and chromedriver was three majors behind the installed Chrome, so
`scratchpad/ui/drive.py` drives headless Chrome over its own DevTools protocol —
`websockets` was already in the venv, so nothing was installed and nothing downloaded.
**34 checks against the live 646-row page**, covering the modal, the tooltip's actual
pixel size, the sort order, the theme redraw, and both scan buttons with `fetch` stubbed
so no AWS quota is spent proving what the handler sends.

**Two testing traps worth remembering.** `dialog.close()` fires its `close` event on a
*queued task*, so reading the chart back in the same expression sees the pre-close state.
And `Page.reload` returns *before* navigation starts: polling for "readyState complete"
matches the old page and passes instantly, then the reload lands mid-test and silently
resets state. That one cost an hour chasing a phantom bug in `paintScanScope`, and is why
the reload helper stamps the page and waits for the stamp to disappear.

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
