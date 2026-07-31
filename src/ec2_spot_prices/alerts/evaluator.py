"""Deterministic alert evaluation with hysteresis.

No LLM, no I/O, no clock reads in :func:`step` -- it is a pure function from
(rule, state, metric) to (state, events). That purity is what makes the hard gate
provable: the oscillation test drives 200 ticks through it with no database and no
scheduler, so a passing test is evidence about the logic rather than about timing.

**The deadband is the whole idea.** A rule that fires at ``price <= T`` does not
re-arm at ``price > T``; it re-arms only once price climbs back above
``T * (1 + margin)``. Between ``T`` and that bound is a dead zone where *nothing is
emitted*, so a price hovering on the threshold -- the normal case for a market at
its floor -- produces exactly one alert, not one per poll.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ec2_spot_prices.alerts.rules import (
    MISSING,
    AlertEvent,
    AlertRule,
    EventKind,
    Metric,
    Missing,
    RuleState,
    RuleStatus,
)
from ec2_spot_prices.storage.base import OfferingRecord

logger = logging.getLogger(__name__)

# Prices are compared at micro-dollar precision. Float noise below that is not a
# market move, and letting it decide a state transition would make alerts random.
_PRECISION = 6


@dataclass(frozen=True, slots=True)
class StepResult:
    state: RuleState
    events: list[AlertEvent]


def compute_metric(rule: AlertRule, records: list[OfferingRecord]) -> Metric:
    """The floor price among offerings that qualify, or MISSING if none do.

    "Floor among the qualifying" is the product in one line: the cheapest node you
    could actually get. An offering that is cheaper but unobtainable does not lower
    this number, because it is not a price you can pay.
    """
    prices = [
        r.offering.price_usd_hr
        for r in records
        if rule.qualifies(
            provider=r.offering.provider,
            gpu_model=r.offering.gpu_model,
            gpu_count=r.offering.gpu_count,
            region=r.offering.region,
            availability=r.offering.availability,
        )
    ]
    return min(prices) if prices else MISSING


def _fires(rule: AlertRule, metric: float) -> bool:
    """Inclusive at the threshold: 'below $X' includes exactly $X."""
    if rule.max_price_usd_hr is None:
        return True  # pure availability rule: qualifying supply existing is the event
    return round(metric, _PRECISION) <= round(rule.max_price_usd_hr, _PRECISION)


def _rearms(rule: AlertRule, metric: float) -> bool:
    """Strictly above the threshold plus the margin, so the dead zone is never both."""
    bound = rule.rearm_bound
    if bound is None:
        return False  # availability rule: only vanishing supply can re-arm it
    return round(metric, _PRECISION) >= round(bound, _PRECISION)


def step(rule: AlertRule, state: RuleState, metric: Metric) -> StepResult:
    """Advance one observation. Pure: same inputs, same outputs, always."""
    # A rule edit resets state; otherwise lowering a threshold on a triggered rule
    # would leave it wedged in TRIGGERED forever.
    if state.rule_version != rule.version:
        state = RuleState(rule_id=rule.id, rule_version=rule.version)

    state = state.model_copy(deep=True)
    events: list[AlertEvent] = []

    if isinstance(metric, Missing):
        return _step_missing(rule, state, events)

    state.last_metric = metric
    state.consecutive_missing = 0

    if state.status is RuleStatus.ARMED:
        if _fires(rule, metric):
            state.consecutive_fire += 1
            state.consecutive_rearm = 0
            if state.consecutive_fire >= rule.trigger_confirmations:
                state.status = RuleStatus.TRIGGERED
                state.trigger_count += 1
                state.consecutive_fire = 0
                events.append(
                    AlertEvent(
                        rule_id=rule.id,
                        kind=EventKind.TRIGGERED,
                        metric=metric,
                        reason="qualifying offer met the threshold",
                    )
                )
        else:
            state.consecutive_fire = 0
        return StepResult(state, events)

    # TRIGGERED: the only way out is a genuine re-arm. Note that a metric still below
    # the threshold emits NOTHING here -- that is precisely the anti-spam property.
    if _rearms(rule, metric):
        state.consecutive_rearm += 1
        if state.consecutive_rearm >= rule.rearm_confirmations:
            state.status = RuleStatus.ARMED
            state.consecutive_rearm = 0
            events.append(
                AlertEvent(
                    rule_id=rule.id,
                    kind=EventKind.RESOLVED,
                    metric=metric,
                    reason="price rose clear of the threshold",
                )
            )
    else:
        # Includes the dead zone AND values still firing. A partial re-arm that gets
        # interrupted must not become a re-fire.
        state.consecutive_rearm = 0
    return StepResult(state, events)


def _step_missing(rule: AlertRule, state: RuleState, events: list[AlertEvent]) -> StepResult:
    """Nothing qualified this cycle."""
    state.consecutive_missing += 1
    # Absence breaks any run of firing observations: it cannot corroborate them.
    state.consecutive_fire = 0
    state.consecutive_rearm = 0

    # An ARMED rule can NEVER fire on absence. "No obtainable offer exists" is not
    # evidence that a price fell below a threshold.
    if state.status is RuleStatus.ARMED:
        return StepResult(state, events)

    # A TRIGGERED rule resolves only after sustained absence. One flaky poll, or a
    # single provider 503, must not resolve the alert and re-arm it to fire again.
    if state.consecutive_missing >= rule.missing_rearm_confirmations:
        state.status = RuleStatus.ARMED
        state.consecutive_missing = 0
        events.append(
            AlertEvent(
                rule_id=rule.id,
                kind=EventKind.RESOLVED,
                metric=None,
                reason="qualifying supply disappeared",
            )
        )
    return StepResult(state, events)


def evaluate(
    rule: AlertRule, state: RuleState, records: list[OfferingRecord]
) -> StepResult:
    """Reduce this cycle's offerings to a metric, then advance the state machine."""
    if not rule.enabled:
        return StepResult(state, [])
    return step(rule, state, compute_metric(rule, records))


def log_intent(events: list[AlertEvent]) -> None:
    """Phase 2 stops at intent. Delivery is Phase 3."""
    for event in events:
        logger.info(
            "ALERT %s rule=%d metric=%s (%s)",
            event.kind,
            event.rule_id,
            event.metric,
            event.reason,
        )
