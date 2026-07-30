"""Preflight check: prove the setup works before running the app.

    uv run python scripts/check_setup.py

Almost every first-run failure with this tool is credentials or IAM permissions,
and the symptoms are unhelpful -- an empty table, or a botocore traceback fifteen
frames deep. This checks each requirement separately and names the exact fix.

It never prints secret material. Where credentials came from is a useful diagnostic;
the key itself is not, so only the resolution *method* is shown.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta

OK = "  \033[32m✓\033[0m"
BAD = "  \033[31m✗\033[0m"
WARN = "  \033[33m!\033[0m"

# Each permission, with the minimal call that proves it and why it is needed.
PERMISSIONS = (
    (
        "ec2:DescribeRegions",
        "discovers which regions your account can price",
        lambda ec2: ec2.describe_regions(),
    ),
    (
        "ec2:DescribeInstanceTypes",
        "reads vCPU / memory / GPU specs",
        lambda ec2: ec2.describe_instance_types(InstanceTypes=["m5.large"]),
    ),
    (
        "ec2:DescribeSpotPriceHistory",
        "the actual prices and their history",
        lambda ec2: ec2.describe_spot_price_history(
            InstanceTypes=["m5.large"],
            ProductDescriptions=["Linux/UNIX"],
            StartTime=datetime.now(UTC) - timedelta(hours=1),
            MaxResults=1,
        ),
    ),
)

# Not required. Without it the spot table is complete and correct; only the
# on-demand and savings columns go blank. So this warns rather than failing --
# telling someone their setup is broken when it works would be its own lie.
OPTIONAL_PERMISSIONS = (
    (
        "pricing:GetProducts",
        "on-demand list prices, for the savings column",
        lambda pricing: pricing.get_products(
            ServiceCode="AmazonEC2",
            Filters=[
                {"Type": "TERM_MATCH", "Field": "instanceType", "Value": "m5.large"},
                {"Type": "TERM_MATCH", "Field": "regionCode", "Value": "us-east-1"},
            ],
            MaxResults=1,
        ),
    ),
)

IAM_HINT = """
    Attach this policy to the IAM user or role you are using. All four actions
    are read-only and free -- none of them can launch, modify or spend anything.

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

    A copy lives in docs/iam-policy.json. These calls do not support resource-level
    scoping, which is why Resource is "*".
"""

CREDS_HINT = """
    No AWS credentials found. Pick whichever fits how you already work:

      aws configure                 # writes ~/.aws/credentials (most common)
      aws sso login --profile NAME  # if your org uses SSO, then AWS_PROFILE=NAME
      export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=...

    Run `aws configure` yourself -- do not paste keys into files in this repo, and
    note that .gitignore does not protect you from a key pasted into tracked code.

    If you are on an EC2 instance or ECS task, an attached instance role is picked
    up automatically and needs no configuration.
"""


def main() -> int:
    print("spotfloor preflight\n")
    failed = False

    # --- Python -------------------------------------------------------------
    version = sys.version_info
    if version >= (3, 12):
        print(f"{OK} Python {version.major}.{version.minor}.{version.micro}")
    else:
        print(f"{BAD} Python {version.major}.{version.minor} -- 3.12+ required")
        return 1

    # --- boto3 --------------------------------------------------------------
    try:
        import boto3
        import botocore
    except ImportError:
        print(f"{BAD} boto3 not installed -- run: uv sync")
        return 1
    print(f"{OK} boto3 {boto3.__version__} / botocore {botocore.__version__}")

    # --- credentials --------------------------------------------------------
    session = boto3.Session()
    credentials = session.get_credentials()

    # An unset environment variable arrives as an empty string rather than as an
    # absent one, and botocore hands back a Credentials object with a blank key
    # instead of None. Test the key, not just the object.
    if credentials is None or not credentials.access_key:
        print(f"{BAD} no AWS credentials resolved")
        print(CREDS_HINT)
        return 1

    method = getattr(credentials, "method", "unknown")
    print(f"{OK} credentials resolved (source: {method})")
    if session.profile_name and session.profile_name != "default":
        print(f"    profile: {session.profile_name}")

    # --- identity -----------------------------------------------------------
    # sts:GetCallerIdentity needs no permission at all, so it isolates "the key is
    # invalid or expired" from "the key is fine but lacks EC2 permissions".
    try:
        identity = session.client("sts").get_caller_identity()
        arn = identity["Arn"]
        print(f"{OK} credentials are valid ({arn.split('/')[-1]})")
    except Exception as exc:
        print(f"{BAD} credentials were found but rejected by AWS: {_reason(exc)}")
        print("    They may be expired, revoked, or mistyped. Re-run `aws configure`")
        print("    or `aws sso login` and try again.")
        return 1

    # --- permissions --------------------------------------------------------
    print()
    ec2 = session.client("ec2", region_name=session.region_name or "us-east-1")
    missing = []
    for action, purpose, call in PERMISSIONS:
        try:
            call(ec2)
            print(f"{OK} {action:<32} {purpose}")
        except Exception as exc:
            print(f"{BAD} {action:<32} {_reason(exc)}")
            missing.append(action)
            failed = True

    if missing:
        print(IAM_HINT)
        return 1

    # The Price List Query API is a global catalog served from us-east-1, not from
    # whatever region the session defaults to.
    pricing = session.client("pricing", region_name="us-east-1")
    for action, purpose, call in OPTIONAL_PERMISSIONS:
        try:
            call(pricing)
            print(f"{OK} {action:<32} {purpose}")
        except Exception as exc:
            print(f"{WARN} {action:<32} {_reason(exc)}")
            print(f"    Optional. Without it the spot table is unaffected; only the")
            print(f"    on-demand and savings columns stay blank.")

    # --- a real price, end to end -------------------------------------------
    print()
    from spotfloor.providers.aws import AwsProvider, enabled_regions

    regions = enabled_regions(ec2)
    print(f"{OK} {len(regions)} regions enabled on this account")
    print(f"    {', '.join(regions)}")
    if len(regions) < 2:
        print(f"{WARN} a cross-region comparator needs more than one region to compare")

    provider = AwsProvider(regions=regions[:2], instance_types=("m5.large",))
    offerings = provider.fetch()
    if not offerings:
        print(f"{BAD} no prices returned -- permissions look fine, so this is unexpected")
        return 1

    print(f"{OK} fetched {len(offerings)} live quotes from {len(regions[:2])} regions")
    for offering in sorted(offerings, key=lambda o: o.price_usd_hr)[:4]:
        # On-demand offerings have no zone -- AWS charges one rate per region -- so
        # the column shows the region and says which kind of price it is.
        where = offering.zone or f"{offering.region} (region)"
        print(
            f"    m5.large  {where:<20} ${offering.price_usd_hr:.4f}/hr  "
            f"{offering.price_kind}  availability={offering.availability}"
        )
    for note in provider.notes:
        print(f"{WARN} {note}")

    print(
        "\n  availability=unknown is correct and permanent: AWS does not publish spot\n"
        "  availability, so this tool compares prices and says so. See the README."
    )
    print("\nReady. Next:\n    uv run python scripts/serve.py --backfill")
    return 1 if failed else 0


def _reason(exc: Exception) -> str:
    """A botocore error code if there is one, else the exception text."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error", {})
        code = error.get("Code")
        if code:
            return f"{code}: {error.get('Message', '')}".strip().rstrip(":")
    return f"{type(exc).__name__}: {exc}"


if __name__ == "__main__":
    sys.exit(main())
