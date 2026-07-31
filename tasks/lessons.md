# Lessons

Patterns worth not re-learning. One entry per correction, with the rule it produces.

## A confusing column is a UI bug, not a question to answer

*"What is GPU/HR & Availability?"* looked like a docs question. It wasn't. `Avail.` was
646 identical `unknown` pills and its explanation lived inside a **collapsed**
`<details>` — the reader could not see it, which is exactly why the meaning was unknown.
Answering in chat would have left the page just as unreadable.

**Rule:** when someone asks what a UI element means, fix the UI. Reply *and* ship.

## Interactive behaviour is unverified until something clicks it

Three bugs shipped through a full test suite because every test asserted on rendered
HTML: a sort that ignored clicks on most of the header, a resize drag that re-sorted the
column, and an "enlarge" that produced a *narrower* chart. All three were found in
minutes by a script that clicked things and read the geometry back.

**Rule:** for front-end work, drive a real browser before claiming it works. If the
extension won't connect, headless Chrome over CDP needs no new dependency — `websockets`
is usually already present. Rendering HTML and `node --check` prove it *parses*, which is
not the same as working.

## Never claim a measurement you did not take

With no browser available it was tempting to state the tooltip's new size from the CSS.
Arithmetic from a stylesheet is not a measurement. Once Chrome was driving, the real
number was 221×79px — say *that*, or say "computed from the CSS, not measured".

**Rule:** label derived numbers as derived. A measured number names its instrument.

## Fix the shared function, not the reported path

The header sort had two symptoms — dead padding and a spurious sort after a resize drag.
They read as separate bugs and were one: the listener was on the inner span that also
contains the grip. Moving it to the `th` with a `.closest('.grip')` guard fixed both in
three lines. Patching each symptom would have been more code and left the other half.

**Rule:** a bug report names a symptom. Find the one place all the symptoms route
through before editing.

## Correct a false premise out loud, then build the real thing

The scan-scoping request came with "consumes time and resources plus billing at times."
EC2 describe calls are free — no bill moves, at any scope. Saying so mattered, because
the wrong reason would have justified the wrong design. The feature was still worth
building for the real reasons: wall-clock time and per-region rate quota.

**Rule:** name the false premise in one sentence, then deliver the full request anyway
under the corrected reasoning. Don't build against an imaginary cost, and don't refuse
over one either.

## A layout fix verified at one viewport is verified at one viewport

`97vw` for the modal was checked at 1400px, passed, and shipped. It was wrong: the
inline chart it has to beat is `100vw - 74px` of *fixed* chrome, so a percentage width
crosses it at exactly one viewport (1227px) and loses above. The "fix" made the enlarged
chart 5px narrower at 1400px — the most common desktop size — and the harness only caught
it on a later unrelated run.

Two follow-ons from the same bug:
- When a rule is "A must always exceed B", derive it from the units. Mixing `%` against
  `px` guarantees a crossover; the only question is which side of it you tested on.
- `dialog` has a **UA stylesheet** `max-width: calc(100% - 6px - 2em)`. It, not your
  width, is often what binds. Measure the element, don't read your own CSS back.

**Rule:** sweep the dimension. Six viewports in a loop is cheaper than one wrong pixel
count in a commit message.

## A negative assertion matches your own explanation of it

`assert "97vw" not in body` failed the moment the CSS comment explained why `97vw` was
wrong. Grep-based tripwires see comments too.

**Rule:** assert the thing that must be present, not the string that must be absent.

## A sentinel is a claim about the value space, and value spaces grow

`-1` meant "not applicable" for `$ per GPU` and `Price moves`, and the sort comparator
swept negatives to the bottom. Correct — until `Saves` arrived, where a negative is
*real data*: spot above the on-demand list price is a market state, not a missing
value. The sentinel silently reclassified every contended row as "no data".

**Rule:** pick a sentinel outside the value space, not at the edge of it. `NaN` from a
blank attribute cannot collide with a number; `-1` can, as soon as one column admits
negatives. And when you replace a guard, assert the old one is **absent** — otherwise
nothing stops it coming back.

## A new dependency in a fake is a hole in the test suite's hermeticity

Adding a pricing client to `AwsProvider` made 40 tests build a *real* boto3 client,
because the test helper faked `client_factory` and nothing else. The suite still
passed most assertions — it was calling AWS and getting right answers. The tell was
runtime: **22.3s → 5.2s** once the fake was supplied. A second instance of the same
hole (`/api/catalog` in the web tests) made a result depend on whether the machine
running the tests happened to have credentials.

**Rule:** when a class gains a new outbound dependency, the test helper that
constructs it must gain a matching fake in the same commit. Watch suite *runtime* as
a hermeticity signal — offline tests that got slower are usually offline tests that
stopped being offline.

## Ask what a new field breaks in the aggregates that already exist

On-demand prices were a clean addition to the model and quietly wrong twice
downstream: history is grouped by `(type, region)` with no price kind, so on-demand
segments would have been counted as spot price *changes*; and `notes` already promised
a failed region was "absent from the table", which an on-demand-only row would have
falsified. Neither is visible in the diff that adds the field.

**Rule:** after adding a variant to a type, grep every place the old type is grouped,
counted, or filtered. The bug is never in the new code — it is in the aggregate that
was correct until the value space widened.

## Two async traps that fake up phantom bugs

- `dialog.close()` fires its `close` event on a **queued task**. Reading state that the
  handler changes, in the same synchronous block, sees the pre-close world.
- CDP `Page.reload` returns **before** navigation begins. Polling for
  "readyState complete" matches the *old* document and passes instantly; the reload then
  lands mid-test and resets state under you. Cost an hour chasing a bug in
  `paintScanScope` that did not exist. Stamp the page, wait for the stamp to vanish.

**Rule:** when a check fails in a full run but passes in isolation, suspect the harness
before the code.

## A cost estimate that justifies a limit deserves the same scrutiny as the limit

The watchlist was 40 types because a comment said an unbounded scan was "~46M rows
and ~57k API calls". That number assumed **one call per (type, region)**.
`DescribeSpotPriceHistory` takes no instance-type filter at all — it paginates the
whole region either way. Measured unfiltered: **9.2s, 17 paginated calls, 15,078
rows.** The estimate was wrong by three orders of magnitude, it had been sitting in
a docstring being cited as settled fact, and it was the sole justification for the
constraint that made `g5.2xlarge` missing.

The real constraint was elsewhere entirely: rendering 15,078 rows server-side is a
36 MB document. That one is genuine — but it wanted a different fix, and the wrong
number pointed at the wrong fix for months.

**Rule:** when a limit is justified by a number, re-measure the number before
accepting the limit — especially a number written by you. An estimate that was never
checked is a guess wearing a unit.

## Order before you truncate

The grouped menu ranked accelerated families first and then capped the list at 120.
The cap ran on the *alphabetical* list, so the 120 slots filled with `a1`, `c1`,
`c3`… and no GPU family ever reached the menu. The ordering code was correct, ran,
and was invisible.

**Rule:** when a pipeline both sorts and truncates, the sort must come first. A cap
applied before the ordering silently discards exactly the items the ordering existed
to promote — and it fails *quietly*, because the output looks like a plausible list.

## An indicator that describes an order nothing established

The header opened painting "price ↑" while the server ordered rows by
`(instance_type, price)`. The first few rows of the first type ascend, so it looked
right in any screenshot and in the first screenful of any manual check. Nothing had
ever sorted the data; the arrow was decoration asserted as fact.

**Rule:** initial UI state is a claim, and claims get established, not assumed. If
the page says "sorted by X", sort by X on load — don't rely on the data arriving
that way.

## Three harness bugs per real bug is the normal ratio

The browser sweep found 5 failures on the first pass. Two were real (the sort
indicator, the truncate-before-order bug). Three were the harness: a bare `.focus()`
that fires no event headless, a row count hardcoded while a live poller kept adding
rows underneath it, and a selector demanding a `polyline` from a series with one
observation — which correctly draws a **circle**, by a rule this repo wrote down.

**Rule:** on a red check, reproduce it in isolation before editing the product. And
never assert a fixed count against a system that is still ingesting — derive it from
the page. (Third time this file has recorded a stale hardcoded count.)
