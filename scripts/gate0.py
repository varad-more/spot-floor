"""GATE 0 evidence: print the availability rule and real normalized offerings.

Run: uv run python scripts/gate0.py
"""

from __future__ import annotations

import textwrap

from spotfloor.providers.vast import RELIABILITY_FLOOR, VastProvider, derive_availability


def main() -> None:
    print("=" * 78)
    print("GATE 0 -- Vast.ai availability rule (documented, derived from real fields)")
    print("=" * 78)
    print(textwrap.dedent(derive_availability.__doc__ or "").strip())
    print(f"\nRELIABILITY_FLOOR = {RELIABILITY_FLOOR}")

    print("\n" + "=" * 78)
    print("Live normalized offerings (8x H100 SXM, cheapest obtainable per region)")
    print("=" * 78)

    offerings = VastProvider(watchlist=("H100 SXM",)).fetch()
    eight_gpu = sorted(
        (o for o in offerings if o.gpu_count == 8),
        key=lambda o: o.price_per_gpu_hr,
    )

    if not eight_gpu:
        print("No 8x H100 SXM nodes listed right now.")
        return

    for o in eight_gpu:
        score = f"{o.availability_score:.4f}" if o.availability_score is not None else "n/a"
        print(
            f"  {o.region:<22} {str(o.price_kind):<10} "
            f"${o.price_usd_hr:>7.2f}/hr node  "
            f"${o.price_per_gpu_hr:>6.2f}/hr/GPU  "
            f"{str(o.availability):<12} score={score}"
        )

    print("\nOne offering, fully normalized:")
    print(textwrap.indent(eight_gpu[0].model_dump_json(indent=2), "  "))


if __name__ == "__main__":
    main()
