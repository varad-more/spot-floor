"""End-to-end: provider -> store -> evaluator.

The composition gate. Dedup and hysteresis are each correct in isolation; this
proves they are correct *together* -- specifically that re-running an identical tick
produces no new alert. A store that re-inserted unchanged rows, or an evaluator that
re-fired a triggered rule, would both show up right here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from spotfloor.alerts.evaluator import evaluate
from spotfloor.alerts.rules import AlertRule, EventKind, RuleState, RuleStatus
from spotfloor.ingest.pipeline import run_tick
from spotfloor.models import Availability, GpuOffering, PriceKind
from spotfloor.storage.base import OfferingFilter, TimeRange
from spotfloor.storage.sqlite import SqliteTimeSeriesStore

T0 = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)


class FakeProvider:
    """Replays a scripted sequence of prices, one per tick."""

    name = "vast"

    def __init__(self, prices: list[float]) -> None:
        self._prices = list(prices)

    def fetch(self) -> list[GpuOffering]:
        price = self._prices.pop(0)
        return [
            GpuOffering(
                provider="vast",
                external_id="m1",
                instance_type="8xH100_SXM_80GB",
                gpu_model="H100_SXM_80GB",
                gpu_count=8,
                region="Japan, JP",
                price_usd_hr=price,
                price_kind=PriceKind.ON_DEMAND,
                availability=Availability.AVAILABLE,
                availability_score=0.99,
                observed_at=T0,
            )
        ]


@pytest.fixture
def store(tmp_path):
    s = SqliteTimeSeriesStore(str(tmp_path / "e2e.db"))
    yield s
    s.close()


def test_a_price_drop_fires_exactly_one_alert_through_the_whole_pipeline(store) -> None:
    """The full loop, and the thing that must not happen twice."""
    rule = AlertRule(
        id=1,
        user_id="u1",
        gpu_model="H100_SXM_80GB",
        gpu_count_min=8,
        max_price_usd_hr=12.0,
    )
    state = RuleState(rule_id=rule.id, rule_version=rule.version)
    provider = FakeProvider([16.0, 16.0, 11.0, 11.0, 11.0])

    events = []
    for tick in range(5):
        now = T0 + timedelta(minutes=5 * tick)
        run_tick([provider], store, now=now)
        result = evaluate(rule, state, store.latest(OfferingFilter(), now=now))
        state = result.state
        events.extend(result.events)

    # Two ticks at 16.00, then three at 11.00. That is ONE crossing, so ONE alert --
    # even though three separate polls observed a qualifying price.
    assert [e.kind for e in events] == [EventKind.TRIGGERED]
    assert state.status is RuleStatus.TRIGGERED
    assert events[0].metric == 11.0

    # And dedup held: 5 polls of 2 distinct states = 2 segments, not 5 rows.
    history = store.history(OfferingFilter(), TimeRange(T0, T0 + timedelta(hours=1)))
    assert len(history) == 2


def test_no_llm_in_the_critical_path() -> None:
    """The 'deterministic core' constraint is architectural, so make it a test."""
    import pathlib
    import re

    src = pathlib.Path(__file__).parent.parent / "src"
    banned = re.compile(r"\b(anthropic|openai|langchain|litellm)\b", re.IGNORECASE)

    offenders = [
        path.relative_to(src)
        for path in src.rglob("*.py")
        if banned.search(path.read_text())
    ]
    assert not offenders, f"LLM dependency leaked into ingestion/alerting: {offenders}"
