"""GATE 2 evidence: hysteresis vs. a naive evaluator on the same price series.

Run: uv run python scripts/gate2.py
"""

from __future__ import annotations

import random

from ec2_spot_prices.alerts.evaluator import step
from ec2_spot_prices.alerts.rules import MISSING, AlertRule, EventKind, Metric, RuleState

THRESHOLD = 12.0


def naive_triggers(series: list[Metric]) -> int:
    """What a threshold check without hysteresis does: fire on every tick below T."""
    return sum(1 for m in series if not isinstance(m, type(MISSING)) and m <= THRESHOLD)


def main() -> None:
    rule = AlertRule(
        id=1,
        user_id="demo",
        gpu_model="H100_SXM_80GB",
        gpu_count_min=8,
        max_price_usd_hr=THRESHOLD,
        rearm_margin_pct=0.05,  # re-arm only above $12.60
        rearm_confirmations=2,
    )

    rng = random.Random(7)
    series: list[Metric] = (
        [15.0, 14.0, 13.0]
        + [11.90]
        + [round(rng.uniform(11.90, 12.10), 4) for _ in range(200)]  # straddles the threshold
        + [14.0, 14.0]
        + [11.0]
    )

    print("=" * 78)
    print("GATE 2 -- alert hysteresis")
    print("=" * 78)
    print(f"rule: 8x H100_SXM_80GB, alert below ${THRESHOLD:.2f}/hr/node")
    print(f"re-arm bound: ${rule.rearm_bound:.2f} (threshold + 5%), re-arm needs 2 consecutive")
    print(f"series: {len(series)} ticks, 200 of which jitter across the threshold\n")

    state = RuleState(rule_id=rule.id, rule_version=rule.version)
    fired: list[tuple[int, EventKind, float | None]] = []
    for tick, metric in enumerate(series):
        result = step(rule, state, metric)
        state = result.state
        fired.extend((tick, e.kind, e.metric) for e in result.events)

    print("events emitted by ec2_spot_prices:")
    for tick, kind, metric in fired:
        price = f"${metric:.2f}" if metric is not None else "n/a"
        print(f"  tick {tick:>3}  {str(kind):<10} at {price}")

    naive = naive_triggers(series)
    print(f"\n  ec2_spot_prices (hysteresis): {sum(1 for _, k, _ in fired if k is EventKind.TRIGGERED)} alerts")
    print(f"  naive threshold check : {naive} alerts")
    print(
        f"\n  The naive evaluator mails the user {naive} times for what is really\n"
        f"  ONE event -- a price sitting at its floor and jittering. That is the\n"
        f"  difference between an alerting product and a spam cannon."
    )

    triggers = sum(1 for _, k, _ in fired if k is EventKind.TRIGGERED)
    assert triggers == 2, triggers  # the first crossing, and one genuinely new one
    assert not [t for t, _, _ in fired if 4 <= t < 204], "straddling ticks emitted alerts"
    print("\nGATE 2: PASS -- exactly one trigger per genuine crossing")


if __name__ == "__main__":
    main()
