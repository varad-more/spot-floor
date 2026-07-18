"""The contract every provider implements."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from spotfloor.models import GpuOffering


@runtime_checkable
class Provider(Protocol):
    """Fetches live offerings and normalizes them to :class:`GpuOffering`.

    Implementations must never fabricate an availability signal. If a provider
    does not truthfully expose whether capacity is obtainable, it reports
    ``Availability.UNKNOWN``.
    """

    name: str

    def fetch(self) -> list[GpuOffering]:
        """Return current offerings. Raises on transport failure; never returns partial lies."""
        ...
