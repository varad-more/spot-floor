# spotfloor

Cross-cloud GPU spot price **and availability** tracker with threshold alerting.
Tells you when a target GPU node is cheap *and actually obtainable*.

Price is the easy part — anyone can chart it. The hard part, and the reason this
exists, is **fulfillment**: can I get an 8×H100 node right now, and will I keep it?

Status: **Phases 0–2 complete** (provider normalization, time-series ingestion,
hysteresis alert engine), plus a read-only web dashboard. Gates 0, 1 and 2 pass
against live provider APIs.

```bash
uv run python scripts/serve.py     # http://127.0.0.1:8000
```

---

## The availability signal is not equally honest across providers

This is the first thing to know about this product, and it is stated here rather
than buried:

| Provider | Price | Availability | Why |
|---|---|---|---|
| **Vast.ai** | live | **live, real** | Publishes real per-machine inventory. `rentable` is a fact. |
| **AWS** | live | **`unknown`** | AWS does not expose spot availability. See below. |

**AWS cannot tell us whether you can get capacity.** The closest thing is the Spot
Placement Score API — and it does not return a market fact. AWS computes the score
*against the calling account's* quotas, limits and usage history. Measured live from
this repo's account, `p5.48xlarge` scored **1/10 in every availability zone**. That
number describes our account, not your odds.

So a placement score fetched with the application's credentials is not merely
imprecise — it is **about the wrong account**. Publishing it as a market signal would
be fabrication. spotfloor therefore reports `availability: unknown` for AWS and *does
not even call the placement-score API* under app credentials. That is enforced by a
test (`test_app_creds_never_call_the_placement_score_api`), not by a comment.

The only honest way to get a real AWS availability signal is to compute it with
**your own** credentials. That path exists (`CredsOwner.USER`) and is off by default.

Consequence, by design: **an AWS offering can never satisfy an availability-gated
alert rule.** `unknown` is not `obtainable`. That is correct behaviour, not a gap.

AWS is the pricing case. Vast is the availability showcase.

---

## Design decisions worth arguing about

**GPU models are normalized. Regions are not.** A chip is a chip: Vast's `"H100 SXM"`
and AWS's `"H100"` on a `p5` are the same silicon, so both resolve to the canonical
SKU `H100_SXM_80GB` and their prices are genuinely comparable. A datacenter is *not*
fungible: Vast's `"Japan, JP"` is not AWS's `us-east-1a`, and there is no honest
mapping between them. Region stays provider-native and is never compared across
providers. Inventing a cross-provider region taxonomy would be fake precision.

VRAM disambiguates real SKU splits — an `A100` ships as both 40GB and 80GB at
materially different prices — and is bucketed rather than trusted verbatim, because
AWS reports a 24GB L4 as `22888 MiB`.

**Price is stored per node, compared per GPU.** `price_usd_hr` is the whole-node rate,
because that is what a provider actually bills. `price_per_gpu_hr` is derived.

**A single Vast query cannot see the spot floor.** The search endpoint returns a
server-side *slice* whose membership depends on the sort order — not merely a
truncation. Measured live on RTX 4090: sorting by `dph_total` reported a spot floor of
`$0.1200/GPU/hr` when the true floor was `$0.1067`, an **11% overstatement**, because
15 machines with cheaper bids were absent from the price-sorted slice entirely. Both
slices were under the result cap. spotfloor therefore queries once per sort key and
unions by `machine_id`. In a product named spotfloor, this is the one bias that cannot
ship.

**Vast's `min_bid` is quoted per node** — verified, not assumed. If it were per-GPU,
`dph_total / min_bid` would scale with node size; measured across live listings it is
flat (RTX 4090: 1.09 at 1 GPU, 1.05 at 8). Getting this backwards would misprice every
multi-GPU spot quote by up to 8×.

**Storage is segments, not points.** A row says "this exact (price, availability) held
from `first_seen` to `last_seen`", so the table grows with *change*, not with time, and
"when did it change" is a row you read rather than a diff you reconstruct. Change
detection hashes integer-quantized values — comparing JSON floats with `=` would open a
new row on every poll and silently destroy dedup.

**Absence is never rendered as a value.** A series that goes stale is *not observed*,
which is a different claim from "unavailable". No tombstones, nothing fabricated.

---

## The alert engine

The correctness problem: a price sitting at its floor jitters across the threshold on
every poll. A naive threshold check mails you every time.

Every rule reduces to one scalar — **the floor price among offerings that actually
qualify** (matching the filters, and obtainable if the rule demands it). That single
reduction lets *one* state machine handle both failure modes: when a machine flips
`available → constrained` it drops out of the qualifying set, so availability flapping
*is* a metric change, and the deadband that stops price spam stops availability spam
too.

A rule that fires at `price ≤ T` does not re-arm at `price > T`. It re-arms only once
price clears `T × (1 + margin)`, and only after N consecutive confirmations. Between
`T` and that bound is a dead zone where **nothing is emitted**.

On a 207-tick series that straddles the threshold 200 times:

```
spotfloor (hysteresis):   2 alerts
naive threshold check : 112 alerts
```

A rule parked *exactly* on its threshold fires **once, ever** — firing is inclusive
(`≤ T`), re-arming requires strictly more than `T`. A zero-deadband rule is rejected at
construction, because it would flap forever.

`step()` is a pure function — no I/O, no clock, no database — which is what makes the
gate provable: the oscillation test drives 200 ticks through the real state machine
with nothing mocked.

**No LLM touches ingestion, normalization or alert evaluation.** That constraint is
architectural, so it is also a test (`test_no_llm_in_the_critical_path`).

---

## The dashboard

A read-only page over the store: current prices per GPU model and provider, with
a sparkline of the floor over the last N hours.

**A page load never fetches from a provider.** It renders what the poller already
stored, so provider rate limits are tied to the poll interval rather than to
traffic, and the table and the chart are two views of one set of stored facts
instead of two fetches that can disagree. That is enforced by a test that wires in
a provider which raises if called.

The three ways a GPU dashboard normally lies, and what this one does instead:

| Temptation | What this page does |
|---|---|
| Leave AWS's availability cell blank | Renders an explicit `unknown` in an outlined pill, with the reason stated above the table. A blank cell reads as "none available", which is a claim we have not earned. |
| Sort AWS and Vast rows together by region | Rows group by **GPU model**; region stays provider-native and is labelled as such. AWS regions *are* compared against each other — that is a real market comparison. |
| Interpolate a chart across missing data | A bucket with no observation is a **break in the line**, never zero and never the previous price carried forward. |

Two price columns, because they answer different questions: *cheapest observed* is
what a price tracker shows, and *cheapest obtainable* is what you could actually
rent. For AWS the second is always `unknown` — which is the entire thesis rendered
as a table cell.

Charts are server-rendered inline SVG: no CDN, no charting library, and the
drawing is a pure function you can assert on.

```
GET /                        the page
GET /api/market              current rows as JSON
GET /api/history/{gpu_model} bucketed floor series (?provider=&hours=&buckets=)
```

Set `SPOTFLOOR_AWS_REGIONS=us-east-1,us-west-2,eu-west-1` to compare AWS against
itself across regions. Vast needs no credentials; without AWS credentials the app
still runs and says so on the page rather than showing an empty AWS section.

---

## Layout

```
src/spotfloor/
  models.py            GpuOffering, PriceKind, Availability, series identity
  gpu.py               canonical cross-provider GPU SKU vocabulary
  query.py             read model: market_table(), floor_series() -- pure
  providers/
    base.py            Provider protocol
    vast.py            live inventory; the documented availability rule
    aws.py             spot pricing; availability = unknown, by construction
  storage/
    base.py            TimeSeriesStore protocol (the DuckDB seam)
    sqlite.py          segment storage + dedup
  ingest/
    pipeline.py        one tick: fetch -> persist
    poller.py          scheduled polling
  alerts/
    rules.py           AlertRule, RuleState
    evaluator.py       pure step(); hysteresis
  web/
    app.py             FastAPI routes; read-only
    sparkline.py       inline SVG; gaps stay gaps
    templates/         the page
```

Business logic depends only on the storage *protocol*, never on SQL — so a
DuckDB/Parquet backend can replace SQLite for range queries without the pipeline,
evaluator or API noticing.

---

## Running it

```bash
uv sync

uv run python scripts/serve.py    # the dashboard on http://127.0.0.1:8000

uv run pytest                     # full suite (includes the live provider gates)
uv run pytest -m "not live"       # offline only

uv run python scripts/gate0.py    # availability rule + live normalized offerings
uv run python scripts/gate1.py    # 3 live poll cycles, dedup, AWS honesty
uv run python scripts/gate2.py    # hysteresis vs. a naive evaluator
```

The AWS gates need credentials with `ec2:DescribeSpotPriceHistory` and
`ec2:DescribeInstanceTypes`. Vast needs none — its search API is public.

`scripts/serve.py` reads `SPOTFLOOR_DB`, `SPOTFLOOR_AWS_REGIONS`,
`SPOTFLOOR_AWS_INSTANCE_TYPES`, `SPOTFLOOR_POLL_INTERVAL_S`,
`SPOTFLOOR_HISTORY_HOURS` and `SPOTFLOOR_PORT`.

## Not built yet

Phases 3–6: notification delivery (email/Slack), auth and per-user rules, Stripe
tiers, deploy. The dashboard is read-only and unauthenticated — it shows the
market, not an account. The provider, storage and alert interfaces are in place
for the rest.
