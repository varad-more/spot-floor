"""The dashboard, driven through its real routes against a real store.

The load-bearing tests in this file are the ones about what the page must *say*:

* ``test_availability_is_rendered_as_an_explicit_unknown`` -- a UI is where the
  honesty constraint is easiest to break, because "leave the cell blank" is the
  default behaviour of every table renderer and it silently reads as "none
  available".
* ``test_the_page_names_the_zone_behind_every_regional_price`` -- a bare regional
  minimum is a number you cannot act on, because you launch into a zone.
* ``test_volatility_is_never_labelled_as_availability`` -- price movement is a real
  fact and a fair contention proxy, and relabelling it as fulfillment would be
  exactly the fabrication this project refuses.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from spotfloor.models import Availability, InstanceOffering, PriceKind
from spotfloor.storage.sqlite import SqliteTimeSeriesStore
from spotfloor.web.app import WebConfig, create_app


def offering(
    *,
    provider: str = "aws",
    instance_type: str = "m5.large",
    region: str = "us-east-1",
    zone: str | None = "us-east-1a",
    price: float = 0.05,
    kind: PriceKind = PriceKind.SPOT,
    availability: Availability = Availability.UNKNOWN,
    gpu_count: int = 0,
    gpu_model: str | None = None,
    vcpus: int | None = 2,
    memory_gib: float | None = 8.0,
    observed_at: datetime | None = None,
) -> InstanceOffering:
    return InstanceOffering(
        provider=provider,
        instance_type=instance_type,
        region=region,
        zone=zone,
        price_usd_hr=price,
        price_kind=kind,
        availability=availability,
        gpu_count=gpu_count,
        gpu_model=gpu_model,
        vcpus=vcpus,
        memory_gib=memory_gib,
        observed_at=observed_at or datetime.now(UTC),
    )


@pytest.fixture
def store(tmp_path):
    s = SqliteTimeSeriesStore(str(tmp_path / "web.db"))
    yield s
    s.close()


@pytest.fixture
def seeded(store):
    """Two regions, two zones inside one: the comparison the page exists for."""
    now = datetime.now(UTC)
    store.write(
        [
            # us-east-1 has a cheap zone and a dear one -> a real intra-region spread.
            offering(region="us-east-1", zone="us-east-1a", price=0.088),
            offering(region="us-east-1", zone="us-east-1d", price=0.051),
            # us-west-2 is a separate market entirely.
            offering(region="us-west-2", zone="us-west-2b", price=0.043),
            # A GPU instance, so the $/GPU column has something in it.
            offering(
                instance_type="p5.48xlarge",
                region="us-east-1",
                zone="us-east-1b",
                price=20.0,
                gpu_count=8,
                gpu_model="H100_SXM_80GB",
                vcpus=192,
                memory_gib=2048.0,
            ),
        ],
        now=now - timedelta(minutes=1),
    )
    return store


@pytest.fixture
def client(seeded):
    app = create_app(store=seeded, config=WebConfig(history_days=7), poll=False)
    with TestClient(app) as c:
        yield c


def test_the_page_renders(client) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "spotfloor" in response.text
    assert "m5.large" in response.text


def test_availability_is_rendered_as_an_explicit_unknown(client) -> None:
    """Never a blank cell. `unknown` must be visible on the page as a word."""
    body = client.get("/").text
    assert "unknown" in body
    # And the reason is stated, not left for the reader to infer.
    assert "Spot Placement Score" in body


def test_the_page_names_the_zone_behind_every_regional_price(client) -> None:
    """You launch into a zone, so a bare regional minimum is unactionable."""
    body = client.get("/").text
    assert "us-east-1d" in body, "the cheapest zone is not named on the page"
    assert "Cheapest zone" in body


def test_the_page_states_the_spread_the_rollup_hid(client) -> None:
    body = client.get("/").text
    assert "AZ spread" in body


def test_volatility_is_never_labelled_as_availability(client) -> None:
    """Price movement is a contention proxy, not a fulfillment signal."""
    body = client.get("/").text
    assert "Price moves" in body
    assert "not availability" in body


def test_regions_are_not_merged_on_the_page(client) -> None:
    body = client.get("/").text
    assert "us-east-1" in body
    assert "us-west-2" in body


def test_market_api_reports_the_cheapest_zone_and_the_spread(client) -> None:
    payload = client.get("/api/market").json()
    rows = {(r["instance_type"], r["region"]): r for r in payload["rows"]}

    east = rows[("m5.large", "us-east-1")]
    assert east["cheapest_usd_hr"] == pytest.approx(0.051)
    assert east["cheapest_zone"] == "us-east-1d"
    assert east["dearest_zone"] == "us-east-1a"
    assert east["zone_count"] == 2
    assert east["spread_pct"] == pytest.approx(72.55, abs=0.01)

    # A different region is a different row, never averaged in.
    west = rows[("m5.large", "us-west-2")]
    assert west["cheapest_usd_hr"] == pytest.approx(0.043)
    assert west["zone_count"] == 1


def test_market_api_makes_no_availability_claim(client) -> None:
    payload = client.get("/api/market").json()
    for row in payload["rows"]:
        assert row["availability"] == "unknown"
        assert row["availability_known"] is False


def test_market_api_carries_the_hardware_spec(client) -> None:
    payload = client.get("/api/market").json()
    p5 = next(r for r in payload["rows"] if r["instance_type"] == "p5.48xlarge")

    assert p5["gpu_count"] == 8
    assert p5["gpu_model"] == "H100_SXM_80GB"
    assert p5["cheapest_per_gpu_hr"] == pytest.approx(2.5)
    assert p5["vcpus"] == 192


def test_a_cpu_row_has_no_per_gpu_price(client) -> None:
    payload = client.get("/api/market").json()
    # Keyed on (type, region): m5.large appears once per region, and rows sort by
    # price, so picking "the first m5.large" would silently depend on which region
    # happens to be cheapest.
    m5 = next(
        r
        for r in payload["rows"]
        if (r["instance_type"], r["region"]) == ("m5.large", "us-east-1")
    )

    assert m5["gpu_count"] == 0
    assert m5["cheapest_per_gpu_hr"] is None
    assert m5["cheapest_per_vcpu_hr"] == pytest.approx(0.051 / 2)


def test_history_api_returns_a_bucketed_series(client) -> None:
    payload = client.get("/api/history/m5.large?days=1&buckets=12").json()
    assert payload["instance_type"] == "m5.large"
    assert len(payload["points"]) == 12
    observed = [p for p in payload["points"] if p["floor_usd_hr"] is not None]
    assert observed, "the seeded observation should appear in some bucket"


def test_history_api_can_scope_to_one_region(client) -> None:
    payload = client.get("/api/history/m5.large?region=us-east-1&buckets=8").json()
    floors = [p["floor_usd_hr"] for p in payload["points"] if p["floor_usd_hr"]]
    # us-west-2's cheaper $0.043 must not leak into a us-east-1-scoped series.
    assert floors and min(floors) == pytest.approx(0.051)


def test_history_for_an_unobserved_type_is_404_not_an_empty_chart(client) -> None:
    """Silence is not a flat line at zero."""
    assert client.get("/api/history/x99.nonexistent").status_code == 404


def test_an_empty_store_renders_a_page_instead_of_crashing(store) -> None:
    app = create_app(store=store, config=WebConfig(), poll=False)
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "No observations yet" in response.text
        assert client.get("/api/market").json()["rows"] == []


def test_healthz(client) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


# --- client-side controls ----------------------------------------------------
#
# "Let the user select what they want tracked" has to work on a static host, so
# filtering happens in the browser over rows already in the document.


def test_the_page_ships_filter_controls(client) -> None:
    body = client.get("/").text
    assert 'id="q"' in body
    assert 'id="family"' in body
    assert 'id="region"' in body
    assert 'id="gpuonly"' in body


def test_rows_carry_the_data_attributes_the_filters_read(client) -> None:
    body = client.get("/").text
    for attribute in ("data-type", "data-family", "data-region", "data-gpu", "data-price"):
        assert attribute in body


# --- snapshot mode -----------------------------------------------------------
#
# A static host cannot poll, so what it serves is a point-in-time snapshot. The
# failure mode to guard against is a page that *looks* live -- an auto-refresh
# meta tag and a timestamp imply data that keeps arriving. That is the same
# species of unearned claim as reporting an availability we cannot observe.


@pytest.fixture
def snapshot_client(seeded):
    app = create_app(store=seeded, config=WebConfig(), poll=False, snapshot=True)
    with TestClient(app) as c:
        yield c


def test_a_snapshot_says_it_is_a_snapshot(snapshot_client) -> None:
    body = snapshot_client.get("/").text
    assert "This is a snapshot, not a live view" in body
    assert "static snapshot" in body


def test_a_snapshot_does_not_pretend_to_refresh(snapshot_client) -> None:
    """The meta refresh would silently reload a page nothing is updating."""
    assert 'http-equiv="refresh"' not in snapshot_client.get("/").text


def test_the_live_page_does_refresh_and_makes_no_snapshot_claim(client) -> None:
    """The inverse, so the two modes cannot quietly converge."""
    body = client.get("/").text
    assert 'http-equiv="refresh"' in body
    assert "This is a snapshot" not in body


def test_a_snapshot_links_relative_api_paths(snapshot_client) -> None:
    """Absolute /api/... 404s under a project-scoped Pages URL."""
    body = snapshot_client.get("/").text
    assert "api/market.json" in body
    assert "<code>/api/market</code>" not in body


def test_caller_supplied_notes_survive_startup_and_reach_the_page(seeded) -> None:
    """A missing region must never be silently missing.

    The first published snapshot had no AWS rows and no explanation: the script
    built the providers, got back "AWS is not configured", assigned it to
    app.state.notes -- and lifespan startup, which runs afterwards, reset the
    list. The page looked like a market with no capacity rather than a market we
    did not query.
    """
    note = "eu-west-1 could not be priced (AuthFailure)"
    app = create_app(
        store=seeded, config=WebConfig(), poll=False, snapshot=True, notes=[note]
    )
    with TestClient(app) as client:
        assert note in client.get("/").text
        assert note in client.get("/api/market").json()["notes"]


def test_the_page_loads_nothing_from_a_third_party(snapshot_client) -> None:
    """Self-contained by construction: no CDN, no fonts, no charting library."""
    import re

    body = snapshot_client.get("/").text
    assert not re.search(r'(?:src|href)="https?://', body)


# --- config ------------------------------------------------------------------
#
# `from_env` was the one path the tests did not touch, because every test builds
# a WebConfig directly -- and it was broken. It read its defaults off the class
# (`os.getenv("X", cls.history_days)`), but WebConfig is a slots dataclass, so
# class-level access yields the slot *descriptor*, not the default. The app
# booted, served /healthz, and 500'd on the first real page.


SPOTFLOOR_ENV_VARS = (
    "SPOTFLOOR_DB",
    "SPOTFLOOR_REGIONS",
    "SPOTFLOOR_INSTANCE_TYPES",
    "SPOTFLOOR_POLL_INTERVAL_S",
    "SPOTFLOOR_HISTORY_DAYS",
    "SPOTFLOOR_BACKFILL_DAYS",
    "SPOTFLOOR_BUCKETS",
)


@pytest.fixture
def clean_env(monkeypatch):
    """Clear every SPOTFLOOR_* variable so these tests read one environment.

    Without this they inherit the developer's shell or the CI job's env and pass
    or fail on where they run. That is not hypothetical: the Pages workflow sets
    SPOTFLOOR_BUCKETS job-wide, and the override test below -- which asserts an
    untouched variable falls through to its default -- went green locally and red
    in CI on exactly that.
    """
    for name in SPOTFLOOR_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_an_unset_environment_yields_the_declared_defaults(clean_env) -> None:
    assert WebConfig.from_env() == WebConfig()


def test_defaults_are_usable_values_not_descriptors(clean_env) -> None:
    """The specific failure: a default that cannot do arithmetic."""
    config = WebConfig.from_env()

    assert isinstance(config.history_days, int)
    assert timedelta(days=config.history_days) == timedelta(days=7)


def test_environment_overrides_are_parsed(clean_env) -> None:
    clean_env.setenv("SPOTFLOOR_DB", "/tmp/x.db")
    clean_env.setenv("SPOTFLOOR_REGIONS", "us-east-1, us-west-2 ,")
    clean_env.setenv("SPOTFLOOR_HISTORY_DAYS", "14")

    config = WebConfig.from_env()

    assert config.db_path == "/tmp/x.db"
    assert config.regions == ("us-east-1", "us-west-2")
    assert config.history_days == 14
    # Untouched variables still fall through to the declared default.
    assert config.buckets == WebConfig().buckets


def test_regions_default_to_none_meaning_discover_every_enabled_one(clean_env) -> None:
    """`None` is not "no regions" -- it is "ask the account which ones it has"."""
    assert WebConfig.from_env().regions is None


def test_a_config_from_env_actually_renders_a_page(store, clean_env) -> None:
    """End-to-end guard: boot the app the way scripts/serve.py does.

    The bug was invisible to every test that passed a hand-built WebConfig, so
    this one insists the env path reaches a rendered page.
    """
    app = create_app(store=store, config=WebConfig.from_env(), poll=False)
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/market").status_code == 200


def test_a_page_load_does_not_fetch_from_providers(seeded) -> None:
    """Rendering reads storage only -- traffic must not drive API quota."""

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
