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


def test_the_page_ships_multiselect_filter_controls(client) -> None:
    """Multi-select with autocomplete, not a single free-text box."""
    body = client.get("/").text
    assert 'id="ms-type"' in body
    assert 'id="ms-region"' in body
    assert 'id="gpuonly"' in body
    assert 'id="clear"' in body


def test_the_multiselects_are_populated_with_every_observed_value(client) -> None:
    """The autocomplete options come from the data, not a hardcoded list."""
    body = client.get("/").text
    # Rendered as JSON arrays into the script, so the exact values must appear.
    assert '"m5.large"' in body and '"p5.48xlarge"' in body
    assert '"us-east-1"' in body and '"us-west-2"' in body


def test_rows_carry_the_data_attributes_the_filters_read(client) -> None:
    body = client.get("/").text
    for attribute in ("data-key", "data-type", "data-family", "data-region",
                      "data-gpu", "data-price"):
        assert attribute in body


# --- the chart ---------------------------------------------------------------


def test_the_page_embeds_series_data_for_the_chart(client) -> None:
    """Embedded rather than fetched, so the chart works on a static export too."""
    import json
    import re

    body = client.get("/").text
    match = re.search(r'<script id="chartdata" type="application/json">(.*?)</script>', body, re.S)
    assert match, "no chart data embedded"

    payload = json.loads(match.group(1))
    assert payload["buckets"] > 0
    assert "step_s" in payload and "start" in payload
    # Keyed by (instance_type, region) -- the pair the chart plots.
    assert "m5.large|us-east-1" in payload["series"]
    assert "m5.large|us-west-2" in payload["series"]
    assert len(payload["series"]["m5.large|us-east-1"]) == payload["buckets"]


def test_unobserved_buckets_are_null_in_the_chart_payload(client) -> None:
    """`null` means not observed. The chart must break the line, not bridge it.

    Sending 0 or a carried-forward price would make the chart assert an observation
    that was never made -- the same class of claim as a fabricated availability.
    """
    import json
    import re

    body = client.get("/").text
    payload = json.loads(
        re.search(r'id="chartdata" type="application/json">(.*?)</script>', body, re.S).group(1)
    )
    series = payload["series"]["m5.large|us-east-1"]

    # The fixture writes one observation, so most of the 7-day window is unobserved.
    assert None in series, "expected gaps to be present as null"
    assert 0 not in series, "a gap was encoded as zero"


def test_the_chart_ships_a_legend_container_and_a_plot_target(client) -> None:
    body = client.get("/").text
    assert 'id="plot"' in body
    assert 'id="legend"' in body
    assert 'id="tip"' in body  # hover tooltip layer


def test_rows_offer_a_compare_across_regions_control(client) -> None:
    """The question the tool exists for, as one click per row."""
    body = client.get("/").text
    assert 'class="cmp"' in body
    assert "across all regions" in body


def test_the_chart_works_in_snapshot_mode_too(snapshot_client) -> None:
    """Charting touches only embedded data, so a static export keeps it."""
    body = snapshot_client.get("/").text
    assert 'id="chartdata"' in body
    assert 'id="plot"' in body


def test_the_page_credits_its_author(client) -> None:
    body = client.get("/").text
    assert "Varad More" in body
    assert "Data fetched" in body


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
    """Self-contained by construction: no CDN, no fonts, no charting library.

    Checks *subresource* loads specifically, not every external URL. A plain
    ``<a href="https://…">`` in the footer fetches nothing and is exactly what a
    published page should carry; a strict CSP constrains what the page requests, so
    that is what this asserts.
    """
    import re

    body = snapshot_client.get("/").text

    # Anything that would issue a network request for a subresource.
    assert not re.search(r"<script[^>]+\bsrc\s*=", body), "external script"
    assert not re.search(r"<link[^>]+\bhref\s*=\s*[\"']https?://", body), "external stylesheet"
    assert not re.search(r"<(?:img|iframe|source|video|audio)[^>]+\bsrc\s*=\s*[\"']https?://", body)
    assert "@import" not in body, "CSS @import can fetch a remote sheet"
    assert not re.search(r"url\(\s*[\"']?https?://", body), "remote url() in CSS"

    # And the only external URLs present are links a reader can click.
    for match in re.finditer(r'href="(https?://[^"]+)"', body):
        start = body.rfind("<", 0, match.start())
        assert body[start : start + 3] == "<a ", f"non-anchor external href: {match.group(1)}"


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


# --- on-demand scanning ------------------------------------------------------
#
# POST /api/refresh is the ONLY route that contacts AWS. Every GET reads storage,
# which is what keeps API quota tied to the schedule rather than to page traffic.
# The tests below pin both halves of that split.


class RecordingProvider:
    """A provider that records what it was asked for instead of calling AWS."""

    name = "aws"

    def __init__(self, config: WebConfig) -> None:
        self.config = config
        self.notes: list[str] = []
        self.calls = 0

    def fetch(self):
        self.calls += 1
        return [
            offering(
                instance_type=self.config.instance_types[0],
                region=(self.config.regions or ("us-east-1",))[0],
                zone=(self.config.regions or ("us-east-1",))[0] + "a",
                price=0.0333,
            )
        ]


@pytest.fixture
def scan_app(seeded):
    """An app whose provider factory records the scope it was handed."""
    built: list[RecordingProvider] = []

    def factory(config: WebConfig) -> tuple[list, list[str]]:
        provider = RecordingProvider(config)
        built.append(provider)
        return [provider], []

    app = create_app(
        store=seeded,
        config=WebConfig(instance_types=("m5.large", "c5.large"), regions=("us-east-1",)),
        poll=False,
        provider_factory=factory,
    )
    with TestClient(app) as client:
        yield client, built


def test_refresh_scans_and_reports_what_it_wrote(scan_app) -> None:
    client, built = scan_app
    response = client.post("/api/refresh", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["fetched"] == {"aws": 1}
    assert body["inserted"] + body["extended"] >= 1
    assert built and built[0].calls == 1


def test_refresh_narrows_the_scan_to_the_requested_scope(scan_app) -> None:
    """The point of scoping: 2 regions instead of 17 is faster and spends less quota."""
    client, built = scan_app
    response = client.post(
        "/api/refresh",
        json={"instance_types": ["p5.48xlarge"], "regions": ["eu-west-1", "eu-west-2"]},
    )

    assert response.status_code == 200
    scoped = built[-1].config
    assert scoped.instance_types == ("p5.48xlarge",)
    assert scoped.regions == ("eu-west-1", "eu-west-2")
    assert response.json()["regions"] == ["eu-west-1", "eu-west-2"]


def test_an_empty_scope_falls_back_to_the_configured_watchlist(scan_app) -> None:
    """"Scan everything" must not be spelled as "scan nothing"."""
    client, built = scan_app
    client.post("/api/refresh", json={"instance_types": [], "regions": []})

    scoped = built[-1].config
    assert scoped.instance_types == ("m5.large", "c5.large")
    assert scoped.regions == ("us-east-1",)


def test_a_second_concurrent_scan_is_refused_rather_than_doubled(seeded) -> None:
    """Two clicks must not double-poll: rate limits are per region, and the second
    scan would spend quota to learn exactly what the first is already learning."""
    import threading

    entered = threading.Event()
    release = threading.Event()

    class BlockingProvider:
        name = "aws"
        notes: list[str] = []

        def fetch(self):
            entered.set()
            release.wait(timeout=5)
            return []

    app = create_app(
        store=seeded,
        config=WebConfig(),
        poll=False,
        provider_factory=lambda config: ([BlockingProvider()], []),
    )

    with TestClient(app) as client:
        result: dict[str, int] = {}
        first = threading.Thread(
            target=lambda: result.__setitem__(
                "first", client.post("/api/refresh", json={}).status_code
            )
        )
        first.start()
        assert entered.wait(timeout=5), "the first scan never started"

        assert client.post("/api/refresh", json={}).status_code == 409

        release.set()
        first.join(timeout=5)
        assert result["first"] == 200

        # And the lock is released, so a later scan still works.
        assert client.post("/api/refresh", json={}).status_code == 200


def test_refresh_reports_an_unconfigured_provider_instead_of_pretending(seeded) -> None:
    app = create_app(
        store=seeded,
        config=WebConfig(),
        poll=False,
        provider_factory=lambda config: ([], ["AWS is not configured"]),
    )
    with TestClient(app) as client:
        response = client.post("/api/refresh", json={})

    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]


def test_a_snapshot_has_no_refresh_route_and_no_button(snapshot_client) -> None:
    """A static file has no server; a button that silently does nothing is worse
    than no button, so both the route and the control are absent.

    404 rather than 405: the route is never registered in snapshot mode, so the path
    genuinely does not exist -- which is the honest status for it.
    """
    assert snapshot_client.post("/api/refresh", json={}).status_code == 404
    body = snapshot_client.get("/").text
    assert 'id="scan"' not in body
    assert "Scan now" not in body


def test_the_live_page_does_offer_a_scan_button(client) -> None:
    """The inverse, so the two modes cannot quietly converge."""
    body = client.get("/").text
    assert 'id="scan"' in body
    assert "Scan now" in body


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
