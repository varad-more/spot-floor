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

## Two async traps that fake up phantom bugs

- `dialog.close()` fires its `close` event on a **queued task**. Reading state that the
  handler changes, in the same synchronous block, sees the pre-close world.
- CDP `Page.reload` returns **before** navigation begins. Polling for
  "readyState complete" matches the *old* document and passes instantly; the reload then
  lands mid-test and resets state under you. Cost an hour chasing a bug in
  `paintScanScope` that did not exist. Stamp the page, wait for the stamp to vanish.

**Rule:** when a check fails in a full run but passes in isolation, suspect the harness
before the code.
