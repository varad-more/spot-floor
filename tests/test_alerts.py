"""GATE 2: hysteresis. Exactly one trigger per genuine crossing.

The oscillation test is the make-or-break: a series that straddles the threshold
must produce one alert, not one per tick. Everything else here defends that
property against the specific ways it tends to break.

step() is pure, so these drive the real state machine with no database, no
scheduler and no clock -- a pass is evidence about the logic itself.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ec2_spot_prices.alerts.evaluator import compute_metric, evaluate, step
from ec2_spot_prices.alerts.rules import (
    MISSING,
    AlertRule,
    EventKind,
    Metric,
    RuleState,
    RuleStatus,
)
from ec2_spot_prices.models import Availability, InstanceOffering, PriceKind
from ec2_spot_prices.storage.base import OfferingRecord

THRESHOLD = 12.0
NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)


def rule(**overrides) -> AlertRule:
    defaults = dict(
        id=1,
        user_id="u1",
        gpu_model="H100_SXM_80GB",
        gpu_count_min=8,
        max_price_usd_hr=THRESHOLD,
        rearm_margin_pct=0.05,  # re-arm bound = 12.60
        rearm_confirmations=2,
    )
    return AlertRule(**(defaults | overrides))


def drive(r: AlertRule, metrics: list[Metric]) -> list:
    """Run a series through the state machine, collecting every event emitted."""
    state = RuleState(rule_id=r.id, rule_version=r.version)
    events = []
    for metric in metrics:
        result = step(r, state, metric)
        state = result.state
        events.extend(result.events)
    return events


def record(
    price: float,
    availability: Availability = Availability.AVAILABLE,
    *,
    provider: str = "vast",
    gpu_count: int = 8,
) -> OfferingRecord:
    offering = InstanceOffering(
        provider=provider,
        instance_type=f"{gpu_count}xH100_SXM_80GB",
        gpu_model="H100_SXM_80GB",
        gpu_count=gpu_count,
        region="Japan, JP",
        price_usd_hr=price,
        price_kind=PriceKind.ON_DEMAND,
        availability=availability,
        observed_at=NOW,
    )
    return OfferingRecord(offering=offering, first_seen=NOW, last_seen=NOW)


# --- THE GATE ----------------------------------------------------------------


def test_oscillation_produces_exactly_one_trigger_per_genuine_crossing() -> None:
    """GATE 2. 200 ticks straddling the threshold must emit nothing after the first.

    This is the failure mode that makes alerting products untrustworthy: a price
    sitting at its floor jitters across the threshold on every poll, and a naive
    evaluator mails the user 200 times.
    """
    r = rule()
    rng = random.Random(7)

    approach = [15.0, 14.0, 13.0]  # above: silence
    crossing = [11.90]  # first genuine crossing: ONE alert
    # Straddle the threshold for 200 ticks without ever clearing the 12.60 re-arm bound.
    straddle = [round(rng.uniform(11.90, 12.10), 4) for _ in range(200)]
    recovery = [14.0, 14.0]  # clears the bound for 2 consecutive ticks: RESOLVED
    recross = [11.0]  # a genuinely new crossing: a second alert is correct

    state = RuleState(rule_id=r.id, rule_version=r.version)
    timeline: list[tuple[int, EventKind]] = []
    for tick, metric in enumerate(approach + crossing + straddle + recovery + recross):
        result = step(r, state, metric)
        state = result.state
        timeline.extend((tick, e.kind) for e in result.events)

    kinds = [k for _, k in timeline]
    assert kinds == [EventKind.TRIGGERED, EventKind.RESOLVED, EventKind.TRIGGERED]

    # The assertion that actually matters: the 200 straddling ticks were silent.
    straddle_ticks = range(4, 204)
    assert not [t for t, _ in timeline if t in straddle_ticks], (
        "the threshold-straddling ticks emitted alerts -- this is the spam bug"
    )

    # And triggers never repeat without an intervening resolve.
    for earlier, later in zip(kinds, kinds[1:]):
        assert earlier != later, "two consecutive events of the same kind"


def test_availability_flapping_does_not_spam() -> None:
    """GATE 2: rapid available<->constrained toggling emits at most one alert.

    Availability flapping is handled by the *same* deadband as price: when a machine
    stops qualifying, the metric goes MISSING, and a TRIGGERED rule needs sustained
    absence (not one blip) before it resolves and could fire again.
    """
    r = rule(missing_rearm_confirmations=3)

    # 100 ticks toggling between "an obtainable offer at $10" and "nothing obtainable".
    metrics: list[Metric] = []
    for i in range(100):
        metrics.append(10.0 if i % 2 == 0 else MISSING)

    events = drive(r, metrics)
    assert [e.kind for e in events] == [EventKind.TRIGGERED], (
        f"availability flapping emitted {len(events)} events"
    )


# --- the properties the gate depends on --------------------------------------


def test_a_zero_deadband_rule_is_rejected() -> None:
    """With no margin, fire and re-arm are both true at m == T and the rule flaps forever."""
    with pytest.raises(ValidationError):
        rule(rearm_margin_pct=0)


def test_parked_exactly_on_the_threshold_fires_once_and_never_again() -> None:
    """m == T forever. Fire is inclusive, so it fires; re-arm needs > T, so it never does."""
    events = drive(rule(), [THRESHOLD] * 50)
    assert len(events) == 1 and events[0].kind is EventKind.TRIGGERED


def test_falling_further_below_the_threshold_does_not_re_alert() -> None:
    """'It got even cheaper' is a digest, not an alert."""
    events = drive(rule(), [11.0, 10.0, 9.0, 4.0])
    assert len(events) == 1


def test_a_gap_far_below_the_threshold_still_fires_once() -> None:
    events = drive(rule(), [50.0, 4.0])
    assert [e.kind for e in events] == [EventKind.TRIGGERED]


def test_the_dead_zone_emits_nothing() -> None:
    """Between T (12.00) and the re-arm bound (12.60) nothing is emitted, in either state."""
    events = drive(rule(), [11.0, 12.1, 12.5, 12.59])
    assert [e.kind for e in events] == [EventKind.TRIGGERED]


def test_a_partial_re_arm_is_not_a_re_fire() -> None:
    """One tick above the bound (needs 2), then back below -> no resolve, and no new trigger."""
    events = drive(rule(rearm_confirmations=2), [11.0, 14.0, 11.0, 11.0])
    assert [e.kind for e in events] == [EventKind.TRIGGERED]


def test_missing_never_fires_an_armed_rule() -> None:
    """Absence of an obtainable offer cannot prove a price crossed a threshold."""
    assert drive(rule(), [MISSING] * 10) == []


def test_a_triggered_rule_survives_a_transient_disappearance() -> None:
    """One flaky poll must not resolve the alert (which would let it re-fire)."""
    r = rule(missing_rearm_confirmations=3)
    assert [e.kind for e in drive(r, [11.0, MISSING, MISSING, 11.0])] == [EventKind.TRIGGERED]
    # ... but sustained absence does resolve it.
    resolved = drive(r, [11.0, MISSING, MISSING, MISSING])
    assert [e.kind for e in resolved] == [EventKind.TRIGGERED, EventKind.RESOLVED]
    assert resolved[-1].reason == "qualifying supply disappeared"


def test_editing_a_rule_resets_its_state() -> None:
    """Without this, lowering a threshold on a triggered rule wedges it forever."""
    r = rule()
    state = step(r, RuleState(rule_id=r.id, rule_version=r.version), 11.0).state
    assert state.status is RuleStatus.TRIGGERED

    edited = rule(max_price_usd_hr=8.0, version=2)
    result = step(edited, state, 7.0)
    assert result.state.status is RuleStatus.TRIGGERED
    assert [e.kind for e in result.events] == [EventKind.TRIGGERED], "edited rule stayed wedged"


def test_trigger_confirmations_debounce_a_single_spike() -> None:
    r = rule(trigger_confirmations=3)
    assert drive(r, [11.0, 15.0, 11.0, 15.0]) == []  # never 3 in a row
    assert len(drive(r, [11.0, 11.0, 11.0])) == 1


def test_events_strictly_alternate_over_a_random_walk() -> None:
    """Property: a trigger is always followed by a resolve before the next trigger."""
    rng = random.Random(11)
    r = rule()
    state = RuleState(rule_id=r.id, rule_version=r.version)

    kinds: list[EventKind] = []
    for _ in range(2_000):
        metric = MISSING if rng.random() < 0.1 else round(rng.uniform(8.0, 16.0), 4)
        result = step(r, state, metric)
        state = result.state
        kinds.extend(e.kind for e in result.events)

    assert kinds, "the walk never fired; the test proves nothing"
    for earlier, later in zip(kinds, kinds[1:]):
        assert earlier != later
    triggers = kinds.count(EventKind.TRIGGERED)
    resolves = kinds.count(EventKind.RESOLVED)
    assert 0 <= triggers - resolves <= 1


# --- the metric --------------------------------------------------------------


def test_the_metric_is_the_floor_among_obtainable_offers() -> None:
    """A cheaper node you cannot get must not drag the metric down. The thesis, tested."""
    metric = compute_metric(
        rule(),
        [
            record(4.00, Availability.UNAVAILABLE),  # cheapest, but a mirage
            record(16.00, Availability.AVAILABLE),
            record(20.00, Availability.AVAILABLE),
        ],
    )
    assert metric == 16.00


def test_an_aws_offering_can_never_satisfy_an_availability_rule() -> None:
    """The honesty constraint, as a testable consequence.

    AWS availability is UNKNOWN, and UNKNOWN is not 'obtainable'. A rule that demands
    obtainability therefore cannot be satisfied by AWS -- correct, not a gap.
    """
    aws_only = [record(1.00, Availability.UNKNOWN, provider="aws")]

    assert compute_metric(rule(require_available=True), aws_only) is MISSING
    # Without the availability requirement, the same row is usable for pricing.
    assert compute_metric(rule(require_available=False), aws_only) == 1.00


def test_rules_only_match_their_own_filters() -> None:
    records = [
        record(1.00, gpu_count=4),  # too few GPUs
        record(2.00, provider="runpod"),  # wrong provider
        record(16.00),  # the only match
    ]
    assert compute_metric(rule(providers=frozenset({"vast"})), records) == 16.00


def test_a_disabled_rule_is_never_evaluated() -> None:
    r = rule(enabled=False)
    result = evaluate(r, RuleState(rule_id=r.id, rule_version=r.version), [record(1.0)])
    assert result.events == []
