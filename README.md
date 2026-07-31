# spotfloor

**Compare AWS EC2 spot prices across every region — and see which availability zone
each price actually came from.**

Spot prices differ by region, and they differ *within* a region by more than most
people expect. Measured live: `g6.12xlarge` in `ca-central-1` ranges from **$1.13 to
$5.11/hr** depending on the zone — a 4.5× spread inside one region. spotfloor shows
you the cheapest zone by name, and how much the regional roll-up hid.

Runs entirely on your machine against your own AWS account. The API calls it makes
are free.

### [→ See a sample page](https://varadmore.me/spot-floor/)

That page is **real data — every EC2 instance type, priced across every region your
account can reach — captured and frozen.** It is a static snapshot, not a live feed:
nothing refreshes it, and spot prices move continuously, so read every number as
"what was true then".

Sizing the instance itself rather than pricing it? [EC2 Instance
Advisor](https://varadmore.me/ec2-instance-advisor/) weighs vCPU, memory, GPU and
network against your priorities; this page picks up where that one leaves off.

**For actual work, clone this repo and run it with your own AWS credentials.** No
credentials are stored in this repository and CI cannot refresh that page, which is
deliberate — the sample exists to show you the interface, not to be a data source.

---

## What it shows

| | |
|---|---|
| **One row per (instance type, region)** | with the cheapest zone named, because you launch into a zone |
| **On-demand price, and what spot saves** | measured against the zone you'd actually launch into, not a regional average |
| **AZ spread** | how much price variation the regional number hid |
| **7-day price chart** | multi-series — plot one instance across every region and compare |
| **Price moves** | how often the price changed, a contention hint |
| **Every instance type EC2 offers** | ~1,350 of them, discovered from the API — GPU, compute, memory, burstable, storage |
| **Grouped filters** | instance types by family (accelerated first, each labelled with its GPU), regions by geography |
| **Availability** | always `unknown`, and [here's why](#the-one-thing-it-cannot-tell-you) |

Measured live: `p4d.24xlarge` in `us-east-1` at **$7.94/hr spot against $21.96
on-demand — 64% off**. A negative saving is possible and is shown as one: spot above
list price is a real market state under contention, not a data error.

**The scope is the whole catalogue, not a curated list.** A hand-picked watchlist is
wrong the moment you look for something nobody thought to pick — which is exactly how
`g5.2xlarge` came to be missing from a page that listed `g5.xlarge` and `g5.12xlarge`.
At full scope that is ~15,000 rows, so the table renders in the browser from an
embedded dataset and paints a page at a time; filtering and sorting still run over
every row, and the bar under the table says how many of them you are looking at.

Multi-select filters with autocomplete, sortable and resizable columns, a light/dark
toggle, and any sparkline (or **⤢ Enlarge**) opens the chart full size.

**Scan…** opens a picker: choose any of EC2's ~1,354 instance types and any of your
enabled regions, see the scope and a rough duration before you commit, and fetch types
that have never been in the table. It opens prefilled with whatever the filters
currently show. Each row also carries a **⟳** to refetch just that one type in that one
region.

---

## Quick start

```bash
# 1. Prerequisites: Python 3.12+ and uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Clone and install
git clone https://github.com/varad-more/spot-floor.git
cd spot-floor
uv sync

# 3. Give AWS read-only permissions (see below), then verify
uv run python scripts/check_setup.py

# 4. Run it
uv run python scripts/serve.py --backfill        # → http://127.0.0.1:8000
```

`--backfill` loads 30 days of real history first (~35s) so the charts are full-depth
immediately. Without it, charts fill in one poll at a time.

### AWS permissions

Create an IAM user with exactly these four read-only actions
([`docs/iam-policy.json`](docs/iam-policy.json)):

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "ec2:DescribeSpotPriceHistory",
      "ec2:DescribeInstanceTypes",
      "ec2:DescribeRegions",
      "pricing:GetProducts"
    ],
    "Resource": "*"
  }]
}
```

All four are **free** — AWS bills for neither EC2 describe calls nor the Price List
Query API, and none of them can launch or modify anything. Don't reuse an admin key; a
dedicated user with only these four can do nothing else if it leaks.

`pricing:GetProducts` is the only optional one. Without it everything still works and
the on-demand and savings columns say so, rather than reading as "spot saves you
nothing".

Then point boto3 at your credentials however you normally would:

```bash
aws configure                       # writes ~/.aws/credentials
aws sso login --profile my-profile  # then: export AWS_PROFILE=my-profile
```

Run `aws configure` yourself — **never paste keys into files in this repo.**

`check_setup.py` verifies each piece separately (credentials resolve → credentials
are valid → each IAM action → a real live price) and names the exact fix for whatever
fails. It never prints secret material.

---

## Refreshing data

The server polls every 5 minutes on its own. To scan on demand:

**From the page** — **Scan…** opens a picker. Pick instance types and regions
independently of the filters (so you can price something that isn't in the table yet),
watch the estimate update — *"3 types × 2 regions — roughly 3s"* — then scan. For a
single price, click **⟳** on that row: one type, one region, ~2s.

**From the terminal:**

```bash
uv run python scripts/scan.py                               # everything
uv run python scripts/scan.py --types m5.large,c5.large     # only these types
uv run python scripts/scan.py --regions us-east-1,us-west-2 # only these regions
uv run python scripts/scan.py --types p5.48xlarge --show    # scan and print
uv run python scripts/scan.py --backfill --days 60          # deep history
```

Narrowing saves **time and rate quota, not money** — describe calls are free, but API
throttles are per region.

| Scan | Time |
|---|---|
| 1 type × 1 region (one **⟳**) | 2.3s |
| 2 types × 2 regions | 2.6s |
| 40 types × 17 regions (2,003 quotes) | 6.7s |
| **every type × 17 regions (46,417 quotes)** | **9.2s** |
| 30-day backfill, 40 types (172k segments) | ~35s |
| 7-day backfill, every type (~1.7M segments) | ~4min |
| on-demand list prices, every type (24,383 pairs) | 54s, once per process |

An unfiltered sweep costs about what a 40-type one does, because
`DescribeSpotPriceHistory` takes no instance-type filter and paginates the whole
region either way — 17 paginated calls, not one per type. This repo's own earlier
estimate of "~57k API calls" assumed the opposite and was wrong by three orders of
magnitude; the watchlist was never bounded by API cost, only by how many rows a page
could render.

---

## The one thing it cannot tell you

**Every availability cell says `unknown`. That is the honest answer, permanently.**

AWS does not publish spot availability. The nearest thing, Spot Placement Score, is
computed against *the calling account's* quota and usage history — so a score fetched
with your credentials describes your account, not whether capacity exists. Measured
live, `p5.48xlarge` scored 1/10 in every zone for the author's account.

So spotfloor doesn't call that API and reports `unknown` rather than inventing a
number. **It compares prices; it does not claim to know what you can get.**

"Price moves" is not availability either — it counts real price changes from AWS's
published history, which hints at contention. It is not a fulfillment probability.

---

## Configuration

All optional, all environment variables.

| Variable | Default | Meaning |
|---|---|---|
| `SPOTFLOOR_DB` | `spotfloor.db` | SQLite path. Pure cache — safe to delete. |
| `SPOTFLOOR_REGIONS` | *all enabled* | Comma-separated. Unset discovers your regions. |
| `SPOTFLOOR_INSTANCE_TYPES` | *every type EC2 offers* | Comma-separated. Set it to narrow the scope for a faster local run. |
| `SPOTFLOOR_HISTORY_DAYS` | `7` | What the page charts. |
| `SPOTFLOOR_BACKFILL_DAYS` | `30` | Backfill depth. AWS retains ~89. |
| `SPOTFLOOR_POLL_INTERVAL_S` | `300` | Background poll interval. |
| `SPOTFLOOR_PORT` | `8000` | |

```bash
SPOTFLOOR_INSTANCE_TYPES=p5.48xlarge,p4d.24xlarge \
SPOTFLOOR_REGIONS=us-east-1,us-west-2 \
uv run python scripts/serve.py --backfill
```

---

## Commands

```bash
uv run python scripts/check_setup.py                     # preflight diagnostics
uv run python scripts/serve.py --backfill                # dashboard
uv run python scripts/scan.py --help                     # one-shot scan
uv run python scripts/snapshot.py --out site --backfill   # static export

uv run pytest -m "not live"    # 212 tests, no AWS needed
uv run pytest                  # + live correctness gates
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `no AWS credentials resolved` | `aws configure`. An empty `AWS_ACCESS_KEY_ID=` counts as unset. |
| `UnauthorizedOperation` | Key is valid but IAM policy is missing — attach [`docs/iam-policy.json`](docs/iam-policy.json). |
| A region is missing | It'll be named in a note on the page. Opt-in regions you haven't enabled raise `AuthFailure`. |
| Charts are single dots | Started without `--backfill`. Run `scripts/scan.py --backfill`. |
| `address already in use` | `SPOTFLOOR_PORT=8787` |
| Everything says `unknown` | Correct and permanent — see [above](#the-one-thing-it-cannot-tell-you). |

---

## Project layout

```
src/spotfloor/
  models.py       InstanceOffering — region/zone split, optional GPU fields
  query.py        read model: region_table(), volatility() — pure functions
  providers/aws.py  region fan-out, history-as-segments, availability = unknown
  storage/        TimeSeriesStore protocol + SQLite segment storage
  ingest/         one poll tick, and the scheduler
  alerts/         hysteresis alert engine (proven, not yet wired)
  web/            FastAPI routes, chart, template
scripts/          check_setup, serve, scan, snapshot, gate0–2
docs/DESIGN.md    why it's built this way, with the measurements
```

**[docs/DESIGN.md](docs/DESIGN.md)** covers the design decisions: why AWS spot history
is a change-log rather than a sample series, why the database is a rebuildable cache,
why regions are discovered instead of hardcoded, and what was measured to decide each.

## Not built

- Spot `Linux/UNIX` only. On-demand is priced for the matching SKU (shared tenancy,
  no bundled software); Windows, SUSE and dedicated tenancy are not.
- **On-demand price history.** AWS publishes none, so that series only accumulates
  forward from your first poll — a backfilled on-demand chart would be invented.
- **Regions AWS quotes in a currency other than USD** (the China regions) get no
  on-demand price. Converting would mean inventing an exchange rate.
- No hosted deployment, by choice — publishing would mean putting AWS credentials in
  GitHub. CI runs offline tests only.
- Alert delivery, auth, per-user rules. The engine exists; the wiring doesn't.

## License

[MIT](LICENSE).

---

<div align="center">

Built by **Varad More** · [github.com/varad-more](https://github.com/varad-more)

</div>
