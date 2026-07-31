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

import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from spotfloor.models import Availability, InstanceOffering, PriceKind
from spotfloor.storage.sqlite import SqliteTimeSeriesStore
from spotfloor.web.app import NO_CREDENTIALS_NOTE, WebConfig, create_app

# --- reading the page's data -------------------------------------------------
# The table is rendered by the browser from an embedded payload, because the full
# EC2 catalogue is 15,078 rows and server-rendering that is a ~36 MB document. So
# assertions about *what the page knows* read the payload; assertions about what it
# *does* with that knowledge read the rendering code. Both still run over the real
# route -- neither is a second implementation of the page.


def table_data(body: str) -> dict:
    """The embedded row payload the client renders from."""
    match = re.search(
        r'<script id="tabledata" type="application/json">(.*?)</script>', body, re.S
    )
    assert match, "no table data embedded"
    return json.loads(match.group(1))


def payload_row(payload: dict, instance_type: str, region: str) -> dict:
    """One row, with its interned indices resolved back to names."""
    for raw in payload["rows"]:
        spec = payload["specs"][raw[0]]
        if spec[0] == instance_type and payload["regions"][raw[1]] == region:
            zone = lambda i: None if i < 0 else payload["zones"][i]  # noqa: E731
            return {
                "instance_type": spec[0],
                "family": spec[1],
                "vcpus": spec[2],
                "memory_gib": spec[3],
                "gpu_model": spec[4],
                "gpu_count": spec[5],
                "region": region,
                "price": raw[2],
                "zone": zone(raw[3]),
                "zone_count": raw[4],
                "dearest_zone": zone(raw[5]),
                "dearest_price": raw[6],
                "on_demand": raw[7],
                "moves": raw[8],
                "cv": raw[9],
                "series": decode_rle(raw[10]),
                "price_kind": raw[11],
            }
    raise AssertionError(f"no row for {instance_type} in {region}")


def decode_rle(encoded: str) -> list[float | None]:
    """Inverse of `app._rle`. `null` must survive the round trip as `None`."""
    out: list[float | None] = []
    for token in encoded.split(","):
        if not token:
            continue  # only reachable for an entirely empty series
        head, sep, count = token.partition(":")
        if not sep:
            out.append(float(head))
            continue
        out.extend([None if head == "" else float(head)] * int(count))
    return out


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
def priced(seeded):
    """The seeded store, plus the on-demand list price for one of its two regions.

    Only one region on purpose: the other is what proves a missing on-demand price
    renders as "not observed" rather than as a zero or as "saves nothing".
    """
    seeded.write(
        [
            # No zone -- AWS charges one on-demand rate for the whole region.
            offering(region="us-east-1", zone=None, price=0.096, kind=PriceKind.ON_DEMAND),
            offering(
                instance_type="p5.48xlarge",
                region="us-east-1",
                zone=None,
                price=98.32,
                kind=PriceKind.ON_DEMAND,
                gpu_count=8,
                gpu_model="H100_SXM_80GB",
                vcpus=192,
                memory_gib=2048.0,
            ),
        ],
        now=datetime.now(UTC) - timedelta(minutes=1),
    )
    return seeded


@pytest.fixture
def client(seeded):
    app = create_app(store=seeded, config=WebConfig(history_days=7), poll=False)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def priced_client(priced):
    app = create_app(
        store=priced,
        config=WebConfig(history_days=7),
        poll=False,
        # Explicit, not incidental: `/api/catalog` builds providers, and the default
        # factory reaches for real boto3 credentials. Leaving it unset makes the
        # offline suite pass or fail depending on whether the machine running it
        # happens to have AWS configured.
        provider_factory=lambda config: ([], ["AWS is not configured in this test."]),
    )
    with TestClient(app) as c:
        yield c


def test_the_page_renders(client) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "spotfloor" in response.text
    assert "m5.large" in response.text


def test_availability_is_rendered_as_an_explicit_unknown(client) -> None:
    """`unknown` must be visible on the page as a word, never an empty space.

    It no longer needs to be a table column: the value is constant on every row
    forever, so 646 identical cells taught the reader nothing and cost a column of
    width. The constraint is that the claim is *stated*, not that it is repeated.
    """
    body = client.get("/").text
    assert "availability: unknown" in body
    # And the reason is stated, not left for the reader to infer.
    assert "Spot Placement Score" in body


def test_the_availability_claim_is_not_hidden_behind_a_disclosure(client) -> None:
    """A collapsed <details> is a claim the reader never sees.

    Dropping the per-row column is only honest if the statement is always visible,
    so it has to sit ahead of the first disclosure widget on the page.
    """
    body = client.get("/").text
    assert body.index("availability: unknown") < body.index("<details class="), (
        "the availability caveat sits inside a collapsed block"
    )


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


def test_rows_carry_the_fields_the_filters_and_sorts_read(client) -> None:
    """Every column the header can sort by must exist on the row objects.

    These used to be `data-` attributes on server-rendered `<tr>`s. They are now
    fields on the embedded rows, but the contract is the same one and breaking it
    fails the same way: a sort key with no backing value silently sorts nothing.
    """
    body = client.get("/").text
    payload = table_data(body)
    row = payload_row(payload, "m5.large", "us-east-1")

    assert row["price"] > 0
    assert row["zone"].startswith("us-east-1"), "the price must name the zone it came from"
    assert row["zone_count"] >= 1
    assert row["family"] == "m5"

    # The sort keys the header advertises, and where each one comes from. `spread`,
    # `saves` and `pergpu` are derived client-side from fields above, so they are
    # asserted through the deriving code rather than the payload.
    for key in ("type", "region", "price", "ondemand", "saves", "spread",
                "pergpu", "moves"):
        assert f'data-key="{key}"' in body, f"header offers no {key} column"
    assert "spread: price > 0 ? (dearest / price - 1) * 100 : 0" in body
    assert "pergpu: gpuCount ? price / gpuCount : null" in body


# --- the chart ---------------------------------------------------------------


def test_the_page_embeds_series_data_for_the_chart(client) -> None:
    """Embedded rather than fetched, so the chart works on a static export too."""
    payload = table_data(client.get("/").text)

    assert payload["buckets"] > 0
    assert "step_s" in payload and "start" in payload
    # Every row carries its own history, on the same time grid, so any pair the
    # chart can plot is already present before the user picks one.
    for region in ("us-east-1", "us-west-2"):
        assert len(payload_row(payload, "m5.large", region)["series"]) == payload["buckets"]


def test_unobserved_buckets_are_null_in_the_chart_payload(client) -> None:
    """`null` means not observed. The chart must break the line, not bridge it.

    Sending 0 or a carried-forward price would make the chart assert an observation
    that was never made -- the same class of claim as a fabricated availability.
    The run-length encoding is the new place this could go wrong: a gap has to
    survive being compressed alongside the numbers around it.
    """
    series = payload_row(table_data(client.get("/").text), "m5.large", "us-east-1")["series"]

    # The fixture writes one observation, so most of the 7-day window is unobserved.
    assert None in series, "expected gaps to be present as null"
    assert 0 not in series, "a gap was encoded as zero"


def test_the_rle_round_trip_preserves_gaps_and_values() -> None:
    """The encoding is only safe if it is exactly reversible, gaps included."""
    from spotfloor.web.app import _rle

    original = [None, 0.5, 0.5, 0.5, None, None, 0.25, None]
    assert decode_rle(_rle(original)) == original
    # The compression that makes the full catalogue affordable: eight buckets of one
    # price is one token, not eight.
    assert _rle([0.5] * 8) == "0.5:8"
    assert _rle([None] * 4) == ":4"
    # A run of one observed value drops its count -- most runs are length one, and
    # that suffix was 95 bytes per row of pure overhead.
    assert _rle([0.5]) == "0.5"
    assert _rle([0.5, 0.25]) == "0.5,0.25"

    # ...but a gap keeps its count even at length one, so these two stay distinct.
    assert _rle([None]) == ":1"
    assert _rle([]) == ""
    assert decode_rle(":1") == [None]
    assert decode_rle("") == []

    # Every shape a real series takes, round-tripped.
    for series in (
        [None], [0.5], [], [None, None], [0.5, None, 0.5],
        [0.5, 0.5, None, 0.25, 0.25, 0.25], [None, 0.1, None],
    ):
        assert decode_rle(_rle(series)) == series, series


def test_the_client_draws_gaps_as_gaps_too(client) -> None:
    """The sparkline moved from Python to JavaScript; its one rule moved with it.

    `sparkline_svg` is still tested directly in test_sparkline.py, but the page no
    longer calls it -- so the claim "a gap is drawn as a gap" needs an assertion
    against the code that actually renders now.
    """
    body = client.get("/").text
    assert "function sparkline(values)" in body
    # A null starts a new run rather than contributing a point to the current one.
    assert "if (v === null) { flush(); } else { run.push([i, v]); }" in body
    # A run of one is a dot: a polyline with a single point renders nothing at all,
    # which would make intermittent data look like absent data.
    assert "if (run.length === 1)" in body


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


# --- columns that have to explain themselves ---------------------------------


def test_every_derived_column_says_what_it_means(client) -> None:
    """A derived number nobody can name is a number nobody can use.

    `$ per GPU` and `Price moves` are computed, not reported by AWS, so the header
    carries the definition rather than expecting the reader to infer it.
    """
    body = client.get("/").text
    assert "$ per GPU" in body
    assert "divided by the number of GPUs" in body
    assert "NOT an availability or fulfillment signal" in body
    # Every ambiguous header ships a help marker, not just the two worst.
    assert body.count('class="q"') >= 6


def test_the_whole_header_cell_sorts_but_the_resize_grip_does_not(client) -> None:
    """Another source-level tripwire for a bug that shipped.

    The click listener sat on the inner ``.cell`` span, which left the header cell's
    padding dead even though ``th.sortable`` shows a pointer cursor -- and because the
    resize grip lives *inside* that span, finishing a column drag also re-sorted the
    column you were only trying to widen.
    """
    body = client.get("/").text
    assert "th.addEventListener('click'" in body, "the header listener is back on a child"
    assert "event.target.closest('.grip')" in body, "a resize drag will sort the column"


def test_not_applicable_never_sorts_as_a_small_number(client) -> None:
    """A source-level tripwire for a bug that shipped once.

    `$ per GPU`, `Price moves`, `On-demand` and `Saves` all have a "not applicable"
    state -- no GPU, no history, no on-demand price observed. Sorting that as a
    number puts every dash ahead of every real value, which is what made `$ per GPU`
    look broken when clicked.

    The sentinel is a *blank* attribute (NaN), not -1. It had to stop being a
    negative number when `Saves` arrived: a saving is genuinely negative when spot
    sits above the on-demand list price, and that row is real data that must sort
    with the rest of it rather than being swept in with the blanks.
    """
    body = client.get("/").text
    assert "var aNA = isNaN(av), bNA = isNaN(bv);" in body, "the sentinel guard is gone"
    assert "if (aNA !== bNA) { return aNA ? 1 : -1; }" in body, "blanks no longer sort last"
    # A negative sentinel would silently re-break `Saves`. Nothing may reintroduce it.
    assert "(av < 0) !== (bv < 0)" not in body


# --- on-demand, and the saving it makes visible ------------------------------


def test_the_page_shows_the_on_demand_price_and_what_spot_saves(priced_client) -> None:
    """The comparison a spot table exists to support, made explicit.

    A spot price on its own is a number without a scale: $0.051/hr is only
    meaningful next to the $0.096 you would otherwise pay.
    """
    body = priced_client.get("/").text
    assert ">On-demand<" in body
    assert ">Saves<" in body

    east = payload_row(table_data(body), "m5.large", "us-east-1")
    assert east["on_demand"] == pytest.approx(0.096)
    # 0.051 in the cheapest zone against 0.096 on-demand is 47% off. The percentage
    # is derived in the browser, so the assertion covers the input and the formula
    # that turns it into the rendered cell.
    assert east["price"] == pytest.approx(0.051)
    assert "saves: (onDemand && onDemand > 0) ? (1 - price / onDemand) * 100 : null" in body
    assert (1 - east["price"] / east["on_demand"]) * 100 == pytest.approx(46.88, abs=0.01)


def test_on_demand_is_one_row_per_region_not_a_second_row(priced_client) -> None:
    """Stored as its own series; shown as a column. The table must not double."""
    rows = priced_client.get("/api/market").json()["rows"]

    assert [r["price_kind"] for r in rows] == ["spot"] * len(rows)
    east = next(r for r in rows if r["instance_type"] == "m5.large" and r["region"] == "us-east-1")
    assert east["on_demand_usd_hr"] == pytest.approx(0.096)
    assert east["savings_pct"] == pytest.approx(46.88, abs=0.01)


def test_a_missing_on_demand_price_says_so_instead_of_reading_as_zero(priced_client) -> None:
    """us-west-2 was never priced on-demand. The cell must not imply 0% saved.

    Same failure mode as a blank availability cell: the default rendering of "we do
    not know" is a value, and the value it looks like is the wrong claim.
    """
    rows = priced_client.get("/api/market").json()["rows"]
    west = next(r for r in rows if r["region"] == "us-west-2")

    assert west["on_demand_usd_hr"] is None
    assert west["savings_pct"] is None

    body = priced_client.get("/").text
    # Absent in the payload as null -- not as 0, which is a price.
    assert payload_row(table_data(body), "m5.large", "us-west-2")["on_demand"] is None
    # And rendered as a dash that says why, rather than as an empty cell.
    assert "It is not $0." in body
    assert "This is not ‘0% saved’." in body


def test_on_demand_history_never_inflates_the_price_moves_column(priced_client) -> None:
    """The read model groups history by (type, region), with no price kind in the key.

    So an on-demand segment folded into that grouping would be counted as a spot
    price change that never happened -- a fabricated fact in a column the page
    explicitly presents as "real changes AWS published".
    """
    rows = priced_client.get("/api/market").json()["rows"]
    east = next(r for r in rows if r["instance_type"] == "m5.large" and r["region"] == "us-east-1")

    # Two zones, one segment each, one poll: no price has changed yet.
    assert east["price_changes"] == 1


# --- the scan picker ---------------------------------------------------------


def test_the_scan_button_opens_a_picker_rather_than_scanning_the_filters(client) -> None:
    """You cannot filter a table down to a row that is not in it.

    "Scan now" scanned whatever the filters left visible, which made the filters do
    two unrelated jobs and made the one thing you actually want -- price a type you
    have never priced -- impossible to ask for.
    """
    body = client.get("/").text
    assert 'id="picker"' in body
    assert 'id="pick-type"' in body and 'id="pick-region"' in body
    # The filters are offered as a starting point, not imposed as the scope.
    assert 'id="pickvisible"' in body
    assert "Use current filters" in body


def test_the_picker_states_the_cost_of_the_scan_before_it_runs(client) -> None:
    """"40x17" is only useful if you know whether that is a second or a minute."""
    body = client.get("/").text
    assert "function estimateSeconds" in body
    assert "function paintEstimate" in body
    assert 'id="pickest"' in body


def test_the_catalog_offers_types_that_have_never_been_priced(priced_client) -> None:
    """The whole point of the picker: reach a type that is not in the table yet.

    With no AWS provider configured it degrades to what the store and the watchlist
    already know -- a shorter list, never an empty menu, and it says which it is.
    """
    body = priced_client.post("/api/catalog").json()

    assert "m5.large" in body["instance_types"]
    # The configured watchlist, not merely what has been observed.
    assert set(body["watchlist"]) <= set(body["instance_types"])
    assert body["complete"] is False
    assert body["note"], "a partial list must say why it is partial"


def test_the_catalog_is_a_post_because_it_asks_aws(client) -> None:
    """Every GET reads storage only. That guarantee is what keeps API quota tied to
    the poll schedule rather than to page traffic, so the one route that has to ask
    AWS which instance types exist cannot be a GET."""
    assert client.get("/api/catalog").status_code == 405


# --- the missing-credentials prompt ------------------------------------------


def test_missing_credentials_prompt_the_reader_instead_of_showing_an_empty_table(
    store,
) -> None:
    """An empty table reads as "there is nothing to show", not "you are not set up".

    This is the most common first-run failure with this tool, and the page it
    produces is indistinguishable from a working page with no capacity.
    """
    app = create_app(store=store, poll=False, notes=[NO_CREDENTIALS_NOTE])
    with TestClient(app) as c:
        body = c.get("/").text

    assert 'id="setup"' in body
    assert "dialog.showModal()" in body
    # It points at the real setup path rather than restating the error.
    assert "docs/iam-policy.json" in body
    assert "aws configure" in body
    assert "scripts/check_setup.py" in body
    # And it repeats the one rule that matters most.
    assert "Never paste keys into files in" in body


def test_a_configured_page_does_not_nag_about_credentials(client) -> None:
    """The inverse, so the prompt cannot start firing on a working setup."""
    body = client.get("/").text
    assert 'id="setup"' not in body


def test_filtering_to_nothing_explains_itself(client) -> None:
    """A sticky header over blank space reads as a broken page, not a narrow filter."""
    body = client.get("/").text
    assert 'id="norows"' in body
    assert "No rows match these filters" in body


def test_the_page_ships_a_theme_toggle_that_redraws_the_chart(client) -> None:
    """The dark rules were keyed off [data-theme] and nothing ever set it.

    Series colours are resolved to hex when the SVG is built, so a CSS-only swap
    would leave the lines in the other theme's palette -- hence the redraw hook.
    """
    body = client.get("/").text
    assert 'id="theme"' in body
    assert "sf-theme" in body
    assert "document.addEventListener('sf-theme', drawChart)" in body


def test_the_chart_works_in_snapshot_mode_too(snapshot_client) -> None:
    """Charting touches only embedded data, so a static export keeps it."""
    body = snapshot_client.get("/").text
    assert 'id="tabledata"' in body
    assert 'id="plot"' in body
    assert table_data(body)["rows"], "a static export shipped no rows to chart"


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
    assert snapshot_client.post("/api/catalog", json={}).status_code == 404
    body = snapshot_client.get("/").text
    assert 'id="scan"' not in body
    assert 'id="picker"' not in body


def test_the_live_page_does_offer_a_scan_button(client) -> None:
    """The inverse, so the two modes cannot quietly converge."""
    body = client.get("/").text
    assert 'id="scan"' in body
    assert 'id="picker"' in body


def test_every_row_can_be_rescanned_on_its_own(client) -> None:
    """The narrowest possible question: one type, one region.

    A full sweep of the watchlist is 17 regions and ~7s; a single row is one region
    and ~2s. Watching one price move should not cost the other sixteen regions' rate
    quota, so each row carries its own refetch control.
    """
    body = client.get("/").text
    # Both controls are emitted by the row renderer, in the same cell, so "every row
    # that can compare can also rescan" is a property of one function now rather
    # than a count that has to match across thousands of rendered rows.
    assert 'class="cmp"' in body and 'class="rescan"' in body
    assert body.count('class="rescan"') == body.count('class="cmp"') == 1
    # The handler must send a one-element scope, not the visible filter.
    assert "runScan([row.type], [row.region]" in body


def test_the_scan_button_states_its_scope_before_the_click(client) -> None:
    """"Scan now" never said *what* it would scan, so "does this hit all 17 regions?"
    could only be answered by clicking. The label is now derived from the visible
    rows and repainted whenever the filters change."""
    body = client.get("/").text
    assert "function paintScanScope" in body
    assert "document.addEventListener('sf-filter', paintScanScope)" in body
    assert "new CustomEvent('sf-filter')" in body, "nothing tells the label to repaint"


def test_a_snapshot_ships_no_per_row_rescan(snapshot_client) -> None:
    """Same rule as the toolbar button: no server, so no control that pretends.

    Asserted against *markup and code*, not the bare word: a comment explaining why
    the control is absent contains the word too, and a tripwire that fires on its own
    rationale is a tripwire that gets deleted. (Same lesson as the `97vw` assertion.)
    """
    body = snapshot_client.get("/").text
    assert 'class="rescan"' not in body, "a static export emitted a refetch button"
    assert "function runScan" not in body, "a static export shipped the scan handler"
    assert "api/refresh" not in body, "a static export can still POST a scan"


def test_the_chart_can_be_enlarged_without_replacing_the_selection(client) -> None:
    """Sparkline click zooms *that* row; the header control zooms what is plotted.

    Without it the only route into the modal was a sparkline, which resets the
    selection -- so a carefully built 6-series comparison could not be enlarged at all.
    """
    body = client.get("/").text
    assert 'id="enlarge"' in body
    assert "openZoom(null)" in body


def test_the_modal_width_is_not_a_percentage(client) -> None:
    """A vw width cannot beat the inline chart's fixed 74px of chrome.

    `97vw` looked like a fix and was one only below a 1227px viewport -- at 1400px the
    "enlarged" chart came out 5px narrower. Measured across six viewports after the
    change: never narrower.
    """
    body = client.get("/").text
    assert "width: min(1600px, calc(100vw - 1.5rem))" in body


def test_enlarging_an_empty_chart_is_not_offered(client) -> None:
    """It opened a modal over the table its own empty-state prompt pointed at.

    The guard lives in `syncBoxes`, which every selection change routes through --
    a per-caller check would have missed 'clear', the filter path, or init.
    """
    body = client.get("/").text
    assert "enlarge.disabled = !plotted.length" in body
    assert body.index("function syncBoxes") < body.index("enlarge.disabled"), (
        "the guard moved out of the function every selection change calls"
    )


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
