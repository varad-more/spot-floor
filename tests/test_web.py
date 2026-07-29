"""The dashboard, driven through its real routes against a real store.

The load-bearing test in this file is
``test_aws_availability_is_rendered_as_an_explicit_unknown``: a UI is where the
honesty constraint is easiest to break, because "leave the cell blank" is the
default behaviour of every table renderer and it silently reads as "none
available".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from spotfloor.models import Availability, GpuOffering, PriceKind
from spotfloor.storage.sqlite import SqliteTimeSeriesStore
from spotfloor.web.app import WebConfig, create_app


def offering(
    *,
    provider: str = "vast",
    external_id: str | None = "m1",
    gpu_model: str = "H100_SXM_80GB",
    gpu_count: int = 8,
    region: str = "Japan, JP",
    price: float = 16.0,
    kind: PriceKind = PriceKind.ON_DEMAND,
    availability: Availability = Availability.AVAILABLE,
    observed_at: datetime | None = None,
) -> GpuOffering:
    return GpuOffering(
        provider=provider,
        external_id=external_id,
        instance_type=f"{gpu_count}x{gpu_model}",
        gpu_model=gpu_model,
        gpu_count=gpu_count,
        region=region,
        price_usd_hr=price,
        price_kind=kind,
        availability=availability,
        observed_at=observed_at or datetime.now(UTC),
    )


@pytest.fixture
def store(tmp_path):
    s = SqliteTimeSeriesStore(str(tmp_path / "web.db"))
    yield s
    s.close()


@pytest.fixture
def seeded(store):
    """Two providers, same silicon: the cross-provider comparison the page exists for."""
    now = datetime.now(UTC)
    store.write(
        [
            offering(provider="vast", external_id="m1", price=16.0,
                     availability=Availability.AVAILABLE),
            offering(provider="vast", external_id="m2", price=12.0,
                     kind=PriceKind.SPOT, availability=Availability.CONSTRAINED),
            offering(provider="aws", external_id=None, region="us-east-1a",
                     price=24.0, kind=PriceKind.SPOT,
                     availability=Availability.UNKNOWN),
            offering(provider="aws", external_id=None, region="us-west-2b",
                     price=20.0, kind=PriceKind.SPOT,
                     availability=Availability.UNKNOWN),
        ],
        now=now - timedelta(minutes=1),
    )
    return store


@pytest.fixture
def client(seeded):
    app = create_app(store=seeded, config=WebConfig(history_hours=6), poll=False)
    with TestClient(app) as c:
        yield c


def test_the_page_renders(client) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "spotfloor" in response.text
    assert "H100_SXM_80GB" in response.text


def test_aws_availability_is_rendered_as_an_explicit_unknown(client) -> None:
    """Never a blank cell. `unknown` must be visible on the page as a word."""
    body = client.get("/").text
    assert "unknown" in body
    # And the reason is stated, not left for the reader to infer.
    assert "Spot Placement Score" in body


def test_the_page_never_claims_to_have_seen_the_whole_market(client) -> None:
    body = client.get("/").text
    assert "Cheapest observed" in body


def test_regions_are_labelled_provider_native(client) -> None:
    body = client.get("/").text
    assert "provider-native" in body
    # Both providers' own region strings survive verbatim; neither is translated.
    assert "Japan, JP" in body
    assert "us-east-1a" in body


def test_market_api_separates_price_from_obtainable_price(client) -> None:
    payload = client.get("/api/market").json()
    rows = {(r["provider"], r["price_kind"]): r for r in payload["rows"]}

    vast = rows[("vast", "on_demand")]
    assert vast["cheapest_per_gpu_hr"] == pytest.approx(2.0)
    assert vast["cheapest_obtainable_per_gpu_hr"] == pytest.approx(2.0)
    assert vast["availability_known"] is True

    aws = rows[("aws", "spot")]
    # AWS has real prices...
    assert aws["cheapest_per_gpu_hr"] == pytest.approx(2.5)
    # ...and makes no claim whatsoever about getting them.
    assert aws["cheapest_obtainable_per_gpu_hr"] is None
    assert aws["availability_known"] is False
    assert aws["obtainable_nodes"] == 0
    assert aws["node_count"] == 2


def test_an_aws_row_never_merges_two_regions_into_one_price(client) -> None:
    """us-east-1a and us-west-2b are different markets; the row reports the cheaper."""
    payload = client.get("/api/market").json()
    aws = next(r for r in payload["rows"] if r["provider"] == "aws")
    assert aws["cheapest_region"] == "us-west-2b"
    assert aws["node_count"] == 2


def test_history_api_returns_a_bucketed_series(client) -> None:
    payload = client.get("/api/history/H100_SXM_80GB?hours=6&buckets=12").json()
    assert payload["gpu_model"] == "H100_SXM_80GB"
    assert len(payload["points"]) == 12
    observed = [p for p in payload["points"] if p["floor_per_gpu_hr"] is not None]
    assert observed, "the seeded observation should appear in some bucket"


def test_history_api_can_scope_to_one_provider(client) -> None:
    payload = client.get("/api/history/H100_SXM_80GB?provider=aws&buckets=8").json()
    floors = [p["floor_per_gpu_hr"] for p in payload["points"] if p["floor_per_gpu_hr"]]
    # Vast's cheaper $1.50/GPU spot node must not leak into an AWS-scoped series.
    assert floors and min(floors) == pytest.approx(2.5)


def test_history_for_an_unobserved_model_is_404_not_an_empty_chart(client) -> None:
    """Silence is not a flat line at zero."""
    assert client.get("/api/history/B200_SXM_192GB").status_code == 404


def test_an_empty_store_renders_a_page_instead_of_crashing(store) -> None:
    app = create_app(store=store, config=WebConfig(), poll=False)
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "No observations yet" in response.text
        assert client.get("/api/market").json()["rows"] == []


def test_healthz(client) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


# --- config ------------------------------------------------------------------
#
# `from_env` was the one path the tests did not touch, because every test builds
# a WebConfig directly -- and it was broken. It read its defaults off the class
# (`os.getenv("X", cls.history_hours)`), but WebConfig is a slots dataclass, so
# class-level access yields the slot *descriptor*, not the default. The app
# booted, served /healthz, and 500'd on the first real page with
# "unsupported type for timedelta hours component: member_descriptor".


def test_an_unset_environment_yields_the_declared_defaults(monkeypatch) -> None:
    for name in (
        "SPOTFLOOR_DB",
        "SPOTFLOOR_AWS_REGIONS",
        "SPOTFLOOR_AWS_INSTANCE_TYPES",
        "SPOTFLOOR_POLL_INTERVAL_S",
        "SPOTFLOOR_HISTORY_HOURS",
        "SPOTFLOOR_BUCKETS",
    ):
        monkeypatch.delenv(name, raising=False)

    assert WebConfig.from_env() == WebConfig()


def test_defaults_are_usable_values_not_descriptors(monkeypatch) -> None:
    """The specific failure: a default that cannot do arithmetic."""
    monkeypatch.delenv("SPOTFLOOR_HISTORY_HOURS", raising=False)
    config = WebConfig.from_env()

    assert isinstance(config.history_hours, int)
    assert timedelta(hours=config.history_hours) == timedelta(hours=6)


def test_environment_overrides_are_parsed(monkeypatch) -> None:
    monkeypatch.setenv("SPOTFLOOR_DB", "/tmp/x.db")
    monkeypatch.setenv("SPOTFLOOR_AWS_REGIONS", "us-east-1, us-west-2 ,")
    monkeypatch.setenv("SPOTFLOOR_HISTORY_HOURS", "12")

    config = WebConfig.from_env()

    assert config.db_path == "/tmp/x.db"
    assert config.aws_regions == ("us-east-1", "us-west-2")
    assert config.history_hours == 12
    # Untouched variables still fall through to the declared default.
    assert config.buckets == WebConfig().buckets


def test_a_config_from_env_actually_renders_a_page(store, monkeypatch) -> None:
    """End-to-end guard: boot the app the way scripts/serve.py does.

    The bug was invisible to every test that passed a hand-built WebConfig, so
    this one insists the env path reaches a rendered page.
    """
    monkeypatch.delenv("SPOTFLOOR_HISTORY_HOURS", raising=False)
    app = create_app(store=store, config=WebConfig.from_env(), poll=False)
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/market").status_code == 200


def test_a_page_load_does_not_fetch_from_providers(seeded) -> None:
    """Rendering reads storage only -- traffic must not drive provider rate limits."""

    class ExplodingProvider:
        name = "boom"

        def fetch(self):
            raise AssertionError("a page load must never hit a provider")

    app = create_app(
        store=seeded,
        providers=[ExplodingProvider()],
        config=WebConfig(),
        poll=False,
    )
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/market").status_code == 200
