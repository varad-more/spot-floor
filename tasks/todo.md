# EC2 Spot Prices — AWS region-wise spot instance tracker

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
   modal vs 1311px inline. Fixed twice — see below; the landing value is
   `min(1600px, calc(100vw - 1.5rem))`.

**Two more the first pass missed, found by re-running and by widening the net.**

5. *The width fix was only half a fix.* `min(1600px, 97vw)` beat the inline chart below
   a 1227px viewport and lost above it — 5px **narrower** at 1400px, the size it was
   verified at. The inline chart is `100vw - 74px` of *fixed* chrome, so a percentage
   width crosses it at exactly one viewport; and below the 1600px cap what actually
   binds is the UA stylesheet's `dialog { max-width: calc(100% - 6px - 2em) }`, not the
   author rule. Now swept at 1280/1400/1512/1680/1920/2560: never narrower, 1.88×–2.21×
   chart area. Real horizontal gain only exists above ~1450px; below that the win is
   vertical, 300px → 560px.
6. *`⤢ Enlarge` with nothing plotted opened a modal over the table its own empty state
   pointed at* — "click any row below", covering the rows. The guard went into
   `syncBoxes()`, the single function all five selection paths route through, so
   `clear`, the filter path, and init are covered by one line instead of three.

**Verified in a real browser, without the extension.** The Chrome extension would not
connect and chromedriver was three majors behind the installed Chrome, so
`scratchpad/ui/drive.py` drives headless Chrome over its own DevTools protocol —
`websockets` was already in the venv, so nothing was installed and nothing downloaded.
**34 checks against the live 646-row page**, covering the modal, the tooltip's actual
pixel size, the sort order, the theme redraw, and both scan buttons with `fetch` stubbed
so no AWS quota is spent proving what the handler sends. Three more harnesses followed:
`drive_snapshot.py` (9 checks against the static `public/` export via `file://`),
`edges.py` (9 edge cases — empty states, a *failing* rescan restoring its own button,
a 390px phone), and `widths.py` (the six-viewport sweep that caught bug 5). All green;
each of the last two found a bug the happy-path run could not.

**Two testing traps worth remembering.** `dialog.close()` fires its `close` event on a
*queued task*, so reading the chart back in the same expression sees the pre-close state.
And `Page.reload` returns *before* navigation starts: polling for "readyState complete"
matches the old page and passes instantly, then the reload lands mid-test and silently
resets state. That one cost an hour chasing a phantom bug in `paintScanScope`, and is why
the reload helper stamps the page and waits for the stamp to disappear.

## Phase J — licensing and repo metadata ✅

- [x] **MIT `LICENSE`.** The README had claimed MIT with no file behind it; that claim
      was removed rather than pick a license unasked, and the question sat open until
      it was answered. Verified in the built wheel: `License-Expression: MIT` and the
      text lands in `dist-info/licenses/`. GitHub reads `licenseInfo` off the **default
      branch**, so it stays null until this is pushed.
- [x] **`pyproject`** — PEP 639 `license` + `license-files`, keywords, five classifiers.
- [x] **Repo description and 14 topics** set via `gh repo edit`. The description leads
      with the per-zone spread, since that is the thing nothing else shows.
- [x] Stale run instruction: 178 → **180** offline tests (184 with the live gates).

---

## Phase K — on-demand prices, a real scan picker, and a setup prompt ✅

Four requests, and one of them contained a false premise worth correcting out loud
before building: *"scrape that from open source"*. On-demand prices need no scraping.
The **AWS Price List Query API** is first-party, free, and already reachable with the
credentials this tool has — demonstrated by measurement, not asserted: one call
returns **33 USD regions in 0.76s**. Scraping would have been slower, more fragile,
and a violation of this repo's own "official APIs only" rule.

### On-demand price, and the saving

- [x] **`AwsProvider.on_demand_prices()`.** Omitting the `regionCode` filter is what
      makes it affordable: one paginated call covers *every* region for one instance
      type, so a 40-type watchlist is **40 calls, not 680**. Four `TERM_MATCH` filters
      (Linux / Shared / preInstalledSw NA / capacitystatus Used) pin the SKU to the
      same product the spot side prices — without all four, one type returns a dozen
      SKUs and "the on-demand price" becomes whichever sorted first.
- [x] **Stored as its own `PriceKind.ON_DEMAND` series**, not a column bolted onto the
      spot row. `series_key` already treats price kind as identity, so folding them
      together would render one price apparently thrashing by 10x.
- [x] **Shown as a column, not a second row.** The question is "how much does spot save
      me", and a table twice as tall answers it worse. An on-demand price with no spot
      counterpart still gets its own row — dropping it would hide the only price we
      have.
- [x] **`savings_pct` is measured against `cheapest_usd_hr`**, the zone you would
      actually launch into. Against a zone-average it would have read 50% where the
      truth was 75% — a saving on a price available in no zone.
- [x] Three things that must stay `None` rather than become numbers: **no
      `pricing:GetProducts`** (degrades to a note, spot table untouched); **China
      regions**, which quote CNY and would need an invented exchange rate; and **no
      on-demand history**, because AWS publishes none and a backfilled list price
      would be fiction.

### Three bugs this feature exposed, none of them in the feature

1. *The offline suite had started calling AWS.* `provider()` in `test_aws.py` faked
   the EC2 client but not the new pricing one, so `_default_pricing_factory` built a
   real boto3 client and 40 tests went to the network. Caught by a wrong assertion,
   not by a timeout. **Runtime fell 22.3s → 5.2s once faked** — the suite had been
   quietly non-hermetic. `priced_client` in `test_web.py` had the same hole via
   `/api/catalog`, where it made the result depend on whether the machine running the
   tests happened to have AWS configured.
2. *A failed region would have gained an on-demand row.* `notes` promises "eu-west-1
   could not be priced, so it is absent from the table", but the Price List API is a
   global catalog and quotes regions this account cannot call. On-demand is now
   filtered to the regions that actually answered, so the note stays true.
3. *On-demand segments would have inflated `price moves`.* The read model groups
   history by `(type, region)` with no price kind in the key, so an on-demand segment
   folded in there would be counted as a spot price change that never happened —
   fabricated data in a column the page explicitly presents as real. History is now
   read spot-only; `latest` still returns both, because that is the comparison column.

### The sort sentinel had to stop being a negative number

`-1` meant "not applicable" for `$ per GPU` and `Price moves`, and the comparator swept
negatives to the bottom. That is wrong the moment `Saves` exists: **a saving is
genuinely negative when spot sits above the on-demand list price**, which is a real
market state under contention and real data that must sort with the rest of it. The
sentinel is now a blank attribute (`NaN`); the old guard is asserted *absent* so it
cannot be reintroduced.

### The scan picker

- [x] **"Scan now" scanned whatever the filters left visible.** That made the filters
      do two unrelated jobs and made the one thing you actually want — *price a type I
      have never priced* — impossible to ask for: you cannot filter a table down to a
      row that is not in it.
- [x] **`POST /api/catalog`**, memoized on app state. A POST because it asks AWS, and
      this project's guarantee is that *every GET reads storage only*. Hardcoding a
      1,354-entry list to keep it a GET would be the same mistake as hardcoding the
      region list — wrong from the next launch announcement onward.
- [x] Live estimate before the click, and a **loading state**: the picker is usable
      while the catalog is in flight, and without saying so it silently answers "No
      match" for a perfectly valid type. Found by driving it, not by reading it.
- [x] **Opens prefilled from the filters.** "Empty means everything" is the right rule
      for the server and the wrong default for a dialog: off a 1,354-type catalog it
      turns an unmodified Scan click into a ~3-minute sweep nobody asked for.

### Setup prompt, and the primer

- [x] A modal when no provider could be assembled — **two signals, not one**: boto3
      resolved no key, *or* the provider blew up (a profile that does not exist). Both
      produce the same empty table and want the same advice; matching only the first
      left the second staring at nothing.
- [x] It ships **outside** the chart script, because with no credentials there are no
      rows and that script does not ship — which is precisely the case it has to run
      in. Dismissal is remembered, so the 5-minute meta refresh does not re-nag.
- [x] A three-line primer under the lede: what a row is, what to compare, what to click.

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

---

## Phase L — the whole catalog, every region, GPU-first framing

Requested: *"make sure you have added all instances and all regions … g5.2xlarge
is not present"*, plus subtle branding toward **choosing the right GPU**.

### The Phase-0 cost estimate was wrong, and it was load-bearing

The table at the top of this file says an unbounded scan is "~57k calls,
non-viable". That number assumed **one call per (type, region)**. It is not:
`describe_spot_price_history` takes *no* instance-type filter at all, and
paginates the whole region. Re-measured live, 2026-07-30:

| Measured | Bounded (40 types) | **Unbounded (all types)** |
|---|---|---|
| Current-price sweep, 17 regions | 6.7s | **9.2s** |
| API calls | 17 paginated | **17 paginated** (same) |
| Table rows (type × region) | 650 | **15,078** |
| Distinct types priced | 40 | **1,339** |
| On-demand list prices | 40 calls, ~4s | **1 sweep, 53.7s, 24,383 pairs** |
| 7-day history backfill | 172k segments | **~1.66M segments, ~2 min** |

So the watchlist was never bounded by API cost. It was bounded by **rendering** —
and that is the only thing that actually has to change.

- [x] **L1 — `instance_types=None` means every type.** Drop `InstanceTypes` from
      the history call, paginate the catalog unfiltered.
- [x] **L2 — bulk on-demand sweep.** One paginated `get_products` with no
      `instanceType` filter returns all 24,383 (type, region) pairs. Keep the
      per-type path for small watchlists; it wins under ~100 types.
- [x] **L3 — the page stops server-rendering every row.** 15,078 rows × 2.4 KiB
      is a 36 MB document and ~1M DOM nodes. Rows become a compact embedded
      dataset; the DOM holds only what is on screen.
- [x] **L4 — sparklines drawn client-side** from an RLE series, not 15,078 inline
      SVGs (627 KiB at only 650 rows today).
- [x] **L5 — regions: say what is unreachable.** 17 enabled, 17 not opted in.
      Opting in is an account change only the owner should make — so the page
      names them rather than quietly showing 17 of 34.
- [x] **L6 — GPU-first framing**, subtly: title, lede, a GPU default view, and a
      cross-link to the sibling EC2 Instance Advisor.
- [x] **L7 — republish the snapshot** and update README/docs counts.
- [x] **L8 — grouped option menus** (requested mid-build). A flat list of 1,339
      types is unusable: group by **instance family**, labelled with the GPU where
      there is one (`g5 · NVIDIA A10G`), accelerated families first. Group regions
      by **geography** (US / Europe / Asia Pacific / …). Applies to both the filter
      bar and the scan picker — they share one `multiselect()`.

### Verified

- 212 offline tests + 4 live gates pass.
- 27/27 checks in a real browser (headless Chrome over CDP): client rendering, the
  render cap and its notice, sparkline geometry, sort in both directions including
  blanks-last, grouped menus with accelerated families leading, filtering to
  `g5.2xlarge` across all 14 regions that price it, chart lines and dots, show
  more / show all, and a clean console.
- Live: `g5.2xlarge` prices in every region it is offered in — us-east-1 at
  $1.0603 spot against $1.2120 on-demand.

### Two bugs the browser caught that the test suite could not

1. The header painted "price ↑" on load while rows arrived ordered by
   `(instance_type, price)`. Fixed by sorting on init rather than asserting it.
2. The grouped menu capped at 120 options *before* ranking, so alphabetical
   `a1`/`c1` filled every slot and no GPU family ever appeared.

### Left undone, deliberately

- **17 regions are still unreachable** — they are opt-in and this account has not
  enabled them. Enabling one is an account-level change for the owner to make, so
  the page names them in a note instead.
- **The repo has not been renamed.** `ec2-spot-advisor` was recommended; renaming
  moves the published URL and is a GitHub setting, not a code change.
