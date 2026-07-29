# spotfloor — Phases 0–2

Scope agreed at session start: **Phases 0–2 only**, Vast + AWS only, no external
credentials. Phases 3–6 depend on accounts (email/Slack/Stripe/hosting) that would
have to be stubbed rather than proven, and the spec's gates are hard.

## Phase 0 — Provider abstraction + one real source ✅

- [x] `GpuOffering` normalized schema + `Provider` protocol
- [x] Canonical cross-provider GPU SKU vocabulary (`gpu.py`)
- [x] Vast.ai provider with a documented availability rule
- [x] **GATE 0**: live fetch normalizes to the expected SKU, count and a sane price band

## Phase 1 — Ingestion + time-series storage ✅

- [x] `TimeSeriesStore` protocol + SQLite implementation (segment storage)
- [x] Dedup: unchanged state extends a segment; a real change opens a new one
- [x] APScheduler poller (`max_instances=1`, `coalesce=True`)
- [x] AWS provider (spot price history; availability `unknown` under app creds)
- [x] **GATE 1**: 3 live cycles, no errors; rows grow on change not on time;
      time-ordered series query; AWS asserted `unknown`

## Phase 2 — Alert engine ✅

- [x] `AlertRule` / `RuleState`; pure `step()` evaluator
- [x] Hysteresis: deadband + N-consecutive confirmations
- [x] Availability flapping handled by the *same* mechanism as price
- [x] **GATE 2**: 200 threshold-straddling ticks produce exactly one trigger;
      availability flap produces one; events strictly alternate over a 2,000-tick walk

## Phase 4 (partial) — read-only web dashboard ✅

Not the full Phase 4 (no auth, no per-user rules, no Stripe). Just the view.

- [x] `query.py`: pure read model — `market_table()`, `floor_series()`
- [x] Inline SVG sparklines; gaps render as gaps
- [x] FastAPI app: `/`, `/api/market`, `/api/history/{gpu_model}`, `/healthz`
- [x] Multi-region AWS support (`SPOTFLOOR_AWS_REGIONS`) — real cross-region compare
- [x] Graceful degradation to Vast-only when AWS credentials are absent, *stated on
      the page* rather than rendering an empty AWS section
- [x] Verified live: server running against Vast + AWS (us-east-1, us-west-2)

## Hosting — GitHub Pages snapshot ✅

- [x] `scripts/snapshot.py` renders the static site by driving the real app over
      ASGI (no second rendering path to drift)
- [x] Snapshot mode: no auto-refresh, relative API paths, explicit "this is a
      snapshot" banner — tested in both directions so the modes cannot converge
- [x] `TimeSeriesStore.prune(before)` + retention tied to the chart window
- [x] Hourly GitHub Actions workflow; DB kept in the Actions cache, not published
- [x] Live at https://varadmore.me/spot-floor/

---

## Review

**Result: 102 tests pass, all three gates green against live provider APIs, and the
dashboard verified against live Vast + AWS (us-east-1, us-west-2) — 23 rows across
11 GPU models.**

Two decisions were changed by evidence rather than by preference, and both were bugs
I had already written:

**1. A single Vast sort order cannot see the spot floor.** I originally queried Vast
once, sorted by `dph_total`, and read the spot floor off that result. Measured live on
RTX 4090, that reported a floor of `$0.1200/GPU/hr` when the true floor was `$0.1067`
— an 11% overstatement. The endpoint returns a server-side *slice* whose membership
depends on the sort key: 15 machines with cheaper bids were absent from the
price-sorted slice entirely, and *neither* query hit the result cap, so this is not
truncation. Fix: query once per sort key and union by `machine_id`. In a product named
spotfloor, this was the one bias that could not ship.

**2. I was discarding ~10% of real spot quotes.** I had a guard dropping any listing
where `min_bid > dph_total`, on the assumption that a bid floor above the on-demand
rate proved a units mixup. It fired on ~10 machines *every* cycle — far too systematic
to be corruption. Testing the units directly (if `min_bid` were per-GPU, then
`dph_total / min_bid` would scale with node size — it is flat: 1.09 at 1 GPU, 1.05 at
8) showed the units were fine and the guard was wrong. Those quotes are real: the box
is contended, or the host set a high bid floor to discourage interruptible use. They
are now kept. The lesson is that the guard was sanitizing data to protect an
assumption instead of testing the assumption.

**On the AWS honesty constraint.** This was verifiable rather than merely assertable.
`get-spot-placement-scores` works fine with app credentials and returns `Score: 1` for
`p5.48xlarge` in every AZ — but AWS computes that against *the calling account's*
quota and history, so it is a fact about this repo's account, not about a user's odds.
The code therefore does not call the API at all under app credentials, and a test
(`assert_not_called`) enforces it.

**Two bugs the dashboard work exposed, both in code that already "passed".**

**3. The scheduled poller had never run — once.** `Poller.start()` called
`add_job(..., next_run_time=None)`. That reads as "use the trigger's default"; it
means "add this job **paused**". So every ingestion test passed while the actual
scheduler wired up a job that never fired. Nothing caught it because every test
drove `run_tick()` directly — the scheduler was the one part of ingestion with no
coverage, and it was the part that was broken. It now ticks immediately and then on
the interval (a 300s deferral would leave a fresh process with a blank dashboard),
and `tests/test_poller.py` asserts it fires, keeps firing, and stops when stopped.

**4. `WebConfig.from_env()` returned slot descriptors instead of defaults.** It read
its fallbacks off the class — `os.getenv("X", cls.history_hours)` — but `WebConfig`
is a `slots=True` dataclass, where class-level attribute access yields the slot
*descriptor*, not the default value. The app booted fine and served `/healthz`, then
500'd on the first real page with `unsupported type for timedelta hours component:
member_descriptor`. Every test passed, because every test constructed a `WebConfig`
by hand and only `scripts/serve.py` used the env path. Found by running the server
against live providers, which is the argument for doing that before calling it done.
`from_env` now only passes keys for variables that are actually set, so the field
defaults stay the single source of truth.

**5. The first published page dropped its own caveat.** `build_providers` correctly
reported "AWS is not configured", `snapshot.py` assigned it to `app.state.notes`,
and lifespan startup — which runs *afterwards* — reset the list. The deployed page
showed no AWS rows and no explanation, which reads as "AWS has no capacity" rather
than "we never queried AWS". That is the precise misreading the note exists to
prevent, and the note was the thing that vanished. Notes are now a `create_app`
parameter, set before startup instead of clobbered by it. Found only by fetching
the deployed page and asking why AWS was missing — no unit test exercised the
constructor-then-lifespan ordering.

All three were the same class of miss: the seam between components was tested, and
the wiring that assembles them was not. Each was caught by running the thing rather
than by testing its parts.

**A test that read the ambient environment.** The Pages workflow sets
`SPOTFLOOR_BUCKETS` job-wide, and `test_environment_overrides_are_parsed` asserted
that an untouched variable falls through to its default. Green locally, red on its
first CI run. A `clean_env` fixture now clears every `SPOTFLOOR_*` variable, and the
fix was verified by running the suite under CI's exact variable set rather than a
clean one.

**On concurrency.** The dashboard reads while the poller writes, so
`SqliteTimeSeriesStore` now opens with `check_same_thread=False` (safe:
`sqlite3.threadsafety == 3`, and WAL lets readers run alongside the writer) and
serializes `write()` behind a lock. The lock is not decoration: `write()` decides
insert-vs-extend by reading the open segment and then writing based on what it saw,
and under `isolation_level=None` those are separate autocommitted statements.

**On the dashboard's honesty.** A UI is where these constraints are easiest to lose,
because the lie is the framework default. A table renderer's natural output for AWS
availability is an empty cell, which reads as "none available" — so `unknown` is
rendered as a word in an outlined pill, with the reason stated above the table, and
a test asserts it. Likewise the chart: gaps stay gaps, because interpolating across
a missing bucket asserts an observation we never made.

**Deviation from the spec worth flagging:** the spec names SQLModel. The time-series
store uses stdlib `sqlite3` instead, because the dedup is a precise `UPDATE`-then-
`INSERT` with rowcount branching and an ORM would obscure exactly the SQL that has to
be correct — and portable SQL is what makes the DuckDB swap real rather than notional.
SQLModel still fits the relational CRUD entities (users, rules) in Phase 4.

**TODO(scope)** — deliberately not built:
- `max_price_usd_hr` is per-node only; a per-GPU threshold basis may be wanted.
- `require_available` is a bool meaning "confirmed obtainable" (AVAILABLE *or*
  CONSTRAINED). Finer control (strictly AVAILABLE) would need a `min_availability`
  field.
- Vast result-set truncation is logged but still not surfaced per-response. The page
  says "cheapest observed" unconditionally, which is always true but coarser than it
  could be — it cannot yet say *which* rows were drawn from a capped slice.
- Rule state is not yet persisted to the store (it lives in memory across a tick);
  Phase 3 needs it durable for idempotent delivery.
- The dashboard is read-only and unauthenticated: it shows the market, not an
  account. Alert rules exist in the engine but have no UI, because rules are
  per-user and there are no users until Phase 4 proper.
- The poller runs in-process with the web app. That is right for one node and wrong
  the moment there are two — two replicas would double-poll the same providers.
  Splitting the poller into its own process is a deploy concern (Phase 6).
- `floor_series` buckets in Python over segments fetched for the whole window. Fine
  at this row count; a range aggregate pushed into SQL (or DuckDB) is the move when
  the window gets long.
