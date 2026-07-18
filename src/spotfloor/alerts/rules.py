"""Alert rules and their persisted state.

The rule reduces to one scalar: **the floor price among offerings that actually
qualify** -- matching the filters, and obtainable if the rule demands it. That
single reduction is what lets one hysteresis state machine cover both failure modes
the spec calls out. A machine flipping ``available -> constrained`` drops out of the
qualifying set, so availability flapping *is* a metric change, and the deadband that
stops price spam stops availability spam too. There is no second mechanism to keep
in sync.

When nothing qualifies, the metric is ``MISSING`` -- not zero, not the last known
price. Absence of an obtainable offer cannot prove a price crossed a threshold, so a
MISSING observation can never fire a rule.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from pydantic import BaseModel, Field, model_validator

from spotfloor.models import Availability, obtainability_rank


class Missing:
    """Sentinel: nothing qualified this cycle. Distinct from 'the price is zero'."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "MISSING"


MISSING: Final = Missing()
Metric = float | Missing


class RuleStatus(StrEnum):
    ARMED = "armed"
    TRIGGERED = "triggered"


class EventKind(StrEnum):
    TRIGGERED = "triggered"
    RESOLVED = "resolved"


class AlertRule(BaseModel):
    """A user's standing question: "tell me when this GPU is cheap and obtainable"."""

    id: int
    user_id: str
    gpu_model: str
    gpu_count_min: int = Field(default=1, ge=1)
    region: str | None = None  # None = any region
    max_price_usd_hr: float | None = Field(default=None, gt=0)  # per NODE, None = any price
    require_available: bool = True
    providers: frozenset[str] = frozenset()  # empty = all providers
    enabled: bool = True

    # Bumped on any edit. Editing a rule resets its state, so that lowering a
    # threshold on an already-triggered rule does not leave it wedged forever.
    version: int = 1

    # --- hysteresis ---
    rearm_margin_pct: float = Field(default=0.05, ge=0)
    trigger_confirmations: int = Field(default=1, ge=1)
    rearm_confirmations: int = Field(default=2, ge=1)
    missing_rearm_confirmations: int = Field(default=3, ge=1)

    @model_validator(mode="after")
    def _reject_zero_deadband(self) -> AlertRule:
        """A zero deadband is a flapping bug, not a configuration choice.

        With no margin, the fire condition (``m <= T``) and the re-arm condition
        (``m >= T``) are both true at exactly ``m == T``, so the rule fires, re-arms
        and fires again forever. Rejecting it at construction is far cheaper than
        debugging the resulting alert storm in production.
        """
        if self.max_price_usd_hr is not None and self.rearm_margin_pct <= 0:
            raise ValueError(
                "rearm_margin_pct must be > 0 for a price rule, or it will flap at the threshold"
            )
        return self

    @property
    def rearm_bound(self) -> float | None:
        """Price must climb back above this before the rule can fire again.

        ``None`` for a pure availability rule (no price threshold): such a rule can
        only re-arm by the supply actually going away.
        """
        if self.max_price_usd_hr is None:
            return None
        return self.max_price_usd_hr * (1 + self.rearm_margin_pct)

    def qualifies(self, provider: str, gpu_model: str, gpu_count: int, region: str,
                  availability: Availability) -> bool:
        """Whether an offering counts toward this rule's metric at all.

        ``require_available`` means **confirmed obtainable** -- AVAILABLE or
        CONSTRAINED. CONSTRAINED is included because it means "you can get it, you
        may not keep it", which is still obtainable; excluding it would make every
        interruptible spot listing invisible to alerts, and spot is the point.

        It excludes UNKNOWN, and that has a deliberate consequence: **an AWS offering
        can never satisfy an availability-gated rule**, because AWS cannot tell us
        whether you could get the capacity. That is correct behaviour, not a gap.
        """
        if gpu_model != self.gpu_model or gpu_count < self.gpu_count_min:
            return False
        if self.region is not None and region != self.region:
            return False
        if self.providers and provider not in self.providers:
            return False
        if self.require_available:
            return obtainability_rank(availability) <= obtainability_rank(
                Availability.CONSTRAINED
            )
        return True


class RuleState(BaseModel):
    """Persisted hysteresis state. One per rule."""

    rule_id: int
    rule_version: int = 1
    status: RuleStatus = RuleStatus.ARMED
    consecutive_fire: int = 0
    consecutive_rearm: int = 0
    consecutive_missing: int = 0
    trigger_count: int = 0
    last_metric: float | None = None


class AlertEvent(BaseModel):
    """Something worth telling the user about. Phase 2 logs these; Phase 3 delivers them."""

    rule_id: int
    kind: EventKind
    metric: float | None
    reason: str
