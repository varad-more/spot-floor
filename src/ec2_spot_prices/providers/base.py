"""The contract every provider implements."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ec2_spot_prices.models import InstanceOffering


@runtime_checkable
class Provider(Protocol):
    """Fetches live offerings and normalizes them to :class:`InstanceOffering`.

    Implementations must never fabricate an availability signal. If a provider
    does not truthfully expose whether capacity is obtainable, it reports
    ``Availability.UNKNOWN``.
    """

    name: str

    def fetch(self) -> list[InstanceOffering]:
        """Return current offerings. Raises on transport failure; never returns partial lies."""
        ...
