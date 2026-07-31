# EC2 Spot Prices

Compare AWS EC2 spot prices across every region, and see which availability zone
each price actually came from.

Prices vary inside a region more than most people expect. `g6.12xlarge` in
`ca-central-1` ranges from $1.13 to $5.11/hr depending on the zone — a 4.5× spread
in one region. EC2 Spot Prices names the cheapest zone and shows what the regional
roll-up hid.

Runs on your machine against your own AWS account. Every API call it makes is free.

### [→ See a sample page](https://ec2-spot-prices.varadmore.me/)

Real data: every EC2 instance type, across every region that account could reach,
frozen at capture time. Nothing refreshes it. No credentials live in this repo and
CI cannot regenerate that page, so it shows you the interface — it is not a data
source.

Sizing an instance rather than pricing one? [EC2 Instance
Advisor](https://varadmore.me/ec2-instance-advisor/) weighs vCPU, memory, GPU and
network against your priorities.

---

## Quick start

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # needs Python 3.12+ and uv

git clone https://github.com/varad-more/ec2-spot-prices.git
cd ec2-spot-prices
uv sync

uv run python scripts/check_setup.py              # verify AWS access
uv run python scripts/serve.py --backfill         # → http://127.0.0.1:8000
```

`--backfill` loads 30 days of history first (~35s) so the charts start full-depth.

### AWS permissions

Four read-only actions ([`docs/iam-policy.json`](docs/iam-policy.json)):

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

All four are free, and none of them can launch or modify anything. Use a dedicated
user rather than an admin key. `pricing:GetProducts` is the only optional one —
without it the on-demand and savings columns say so instead of reading as "spot
saves you nothing".

Run `aws configure` yourself. **Never paste keys into files in this repo.**

---

## What it shows

| | |
|---|---|
| One row per (instance type, region) | with the cheapest zone named, because you launch into a zone |
| On-demand price, and what spot saves | measured against the zone you'd actually launch into |
| AZ spread | how much variation the regional number hid |
| 7-day price chart | multi-series — plot one instance across every region |
| Price moves | how often the price changed, a contention hint |
| Every instance type EC2 offers | ~1,350, discovered from the API |
| Availability | always `unknown`, and [here's why](#availability-is-always-unknown) |

`p4d.24xlarge` in `us-east-1`: $7.94/hr spot against $21.96 on-demand, 64% off. A
negative saving is possible and is shown as one, though it is float noise in
practice — the highest ratio measured here is 1.000045×, so spot is capped at the
list price.

The scope is the whole catalogue — about 15,000 rows — so the table renders in the
browser from an embedded dataset and paints a page at a time. Filtering and sorting
still run over every row.

**Scan…** opens a picker: any of EC2's ~1,354 instance types, any enabled region,
with a scope and duration estimate before you commit. **⟳** on a row refetches just
that one type in that one region.

---

## The spot floor

AWS will not sell you a spot instance for less than a tenth of its on-demand price.
Bid theory says a spot price should fall until demand meets supply. It doesn't. It
falls to 10% of list and stops dead — however idle the capacity, however empty the
region. That wall is what this tool is named after.

AWS doesn't publish the rule, so this repo measures it. Across 15,277 (instance
type, region) pairs on 2026-07-30:

| | |
|---|---|
| Lowest spot price seen, as a fraction of on-demand | 0.0995× |
| Rows below 10% of list | 0 |
| Rows pinned exactly to the floor | 771 (5.0%) |
| Highest ratio seen | 1.000045× — spot is capped at the list price too |

The page computes these from the rows it is actually rendering (`floor_stats` in
`web/app.py`), never from a constant. If AWS changes the rule, the numbers move and
the prose stays true.

A row at the floor is at its structural minimum: waiting will not make it cheaper,
and the only way to pay less is to launch the same hardware somewhere else. The
**At floor** toggle isolates those rows.

---

## Availability is always `unknown`

AWS does not publish spot availability. The nearest thing, Spot Placement Score, is
computed against *the calling account's* quota and usage history — so a score
fetched with your credentials describes your account, not whether capacity exists.
Measured live, `p5.48xlarge` scored 1/10 in every zone for the author's account.

So EC2 Spot Prices doesn't call that API. It reports `unknown` instead of inventing a
number. It compares prices; it does not claim to know what you can get.

"Price moves" is not availability either. It counts real price changes from AWS's
published history, which hints at contention and stops there.

---

## Refreshing data

The server polls every 5 minutes on its own. To scan on demand:

```bash
uv run python scripts/scan.py                               # everything
uv run python scripts/scan.py --types m5.large,c5.large
uv run python scripts/scan.py --regions us-east-1,us-west-2
uv run python scripts/scan.py --backfill --days 60          # deep history
```

Narrowing saves time and rate quota, not money: describe calls are free, but API
throttles are per region.

| Scan | Time |
|---|---|
| 1 type × 1 region (one **⟳**) | 2.3s |
| every type × 17 regions (46,417 quotes) | 9.2s |
| 30-day backfill, 40 types | ~35s |
| 7-day backfill, every type (~1.7M segments) | ~4min |
| on-demand list prices (24,383 pairs) | 54s, once per process |

An unfiltered sweep costs about what a 40-type one does, because
`DescribeSpotPriceHistory` takes no instance-type filter and paginates the whole
region either way — 17 paginated calls, not one per type.

---

## Configuration

All optional, all environment variables.

| Variable | Default | Meaning |
|---|---|---|
| `EC2_SPOT_PRICES_DB` | `ec2-spot-prices.db` | SQLite path. Pure cache — safe to delete. |
| `EC2_SPOT_PRICES_REGIONS` | *all enabled* | Comma-separated. Unset discovers your regions. |
| `EC2_SPOT_PRICES_INSTANCE_TYPES` | *every type* | Comma-separated. Narrow it for a faster local run. |
| `EC2_SPOT_PRICES_HISTORY_DAYS` | `7` | What the page charts. |
| `EC2_SPOT_PRICES_BACKFILL_DAYS` | `30` | Backfill depth. AWS retains ~89 days. |
| `EC2_SPOT_PRICES_POLL_INTERVAL_S` | `300` | Background poll interval. |
| `EC2_SPOT_PRICES_PORT` | `8000` | |

## Commands

```bash
uv run python scripts/check_setup.py                      # preflight diagnostics
uv run python scripts/serve.py --backfill                 # dashboard
uv run python scripts/scan.py --help                      # one-shot scan
uv run python scripts/snapshot.py --out site --backfill   # static export

uv run pytest -m "not live"    # 226 tests, no AWS needed
uv run pytest                  # + live correctness gates
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `no AWS credentials resolved` | `aws configure`. An empty `AWS_ACCESS_KEY_ID=` counts as unset. |
| `UnauthorizedOperation` | Key is valid but the IAM policy is missing — attach [`docs/iam-policy.json`](docs/iam-policy.json). |
| A region is missing | It'll be named in a note on the page. Opt-in regions you haven't enabled raise `AuthFailure`. |
| Charts are single dots | Started without `--backfill`. Run `scripts/scan.py --backfill`. |
| `address already in use` | `EC2_SPOT_PRICES_PORT=8787` |

## Layout

```
src/ec2_spot_prices/
  models.py         InstanceOffering — region/zone split, optional GPU fields
  query.py          read model: region_table(), volatility() — pure functions
  providers/aws.py  region fan-out, history-as-segments, availability = unknown
  storage/          TimeSeriesStore protocol + SQLite segment storage
  ingest/           one poll tick, and the scheduler
  alerts/           hysteresis alert engine (proven, not yet wired)
  web/              FastAPI routes, chart, template
scripts/            check_setup, serve, scan, snapshot, gate0–2
```

[docs/DESIGN.md](docs/DESIGN.md) covers why AWS spot history is a change-log and not
a sample series, why the database is a rebuildable cache, why regions are discovered
instead of hardcoded, and what was measured to decide each.

## Not built

- Spot `Linux/UNIX` only. On-demand is priced for the matching SKU (shared tenancy,
  no bundled software); Windows, SUSE and dedicated tenancy are not.
- **On-demand price history.** AWS publishes none, so that series only accumulates
  forward from your first poll.
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
