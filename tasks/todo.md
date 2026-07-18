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

---

## Review

**Result: 65 tests pass, all three gates green against live provider APIs.**

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
- Vast result-set truncation is logged but not surfaced in an API response, so any UI
  must say "the cheapest we observed", never "the cheapest that exists".
- Rule state is not yet persisted to the store (it lives in memory across a tick);
  Phase 3 needs it durable for idempotent delivery.
