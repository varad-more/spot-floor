"""Canonical SKU vocabulary.

The convergence tests are the important ones: if Vast's name and AWS's name for
the same silicon do not land on the same canonical SKU, every cross-provider
price comparison in the product is comparing unlike things.
"""

from __future__ import annotations

import pytest

from ec2_spot_prices.gpu import bucket_vram_gb, canonical_gpu_model, is_canonical


@pytest.mark.parametrize(
    ("vast_name", "vast_vram", "aws_name", "aws_vram", "aws_family", "expected"),
    [
        # The headline comparison: Vast 8xH100 vs AWS p5.48xlarge must be the same SKU.
        ("H100 SXM", 81920, "H100", 81920, "p5", "H100_SXM_80GB"),
        # A100 40GB: Vast's SXM4 vs AWS p4d.
        ("A100 SXM4", 40960, "A100", 40960, "p4d", "A100_SXM4_40GB"),
        # L40S, where AWS under-reports VRAM as 45776 MiB (44.7 GiB) for a 48GB card.
        ("L40S", 49152, "L40S", 45776, "g6e", "L40S_48GB"),
    ],
)
def test_providers_converge_on_same_sku(
    vast_name: str,
    vast_vram: int,
    aws_name: str,
    aws_vram: int,
    aws_family: str,
    expected: str,
) -> None:
    assert canonical_gpu_model(vast_name, vast_vram) == expected
    assert canonical_gpu_model(aws_name, aws_vram, aws_instance_family=aws_family) == expected


def test_vram_splits_a100_into_distinct_skus() -> None:
    """A100 40GB and 80GB are different parts at different prices; never collapse them."""
    forty = canonical_gpu_model("A100", 40960, aws_instance_family="p4d")
    eighty = canonical_gpu_model("A100", 81920, aws_instance_family="p4de")
    assert forty == "A100_SXM4_40GB"
    assert eighty == "A100_SXM4_80GB"
    assert forty != eighty


def test_h100_variants_stay_distinct() -> None:
    """SXM, PCIe and NVL H100s differ materially in price and interconnect."""
    models = {
        canonical_gpu_model("H100 SXM", 81920),
        canonical_gpu_model("H100 PCIE", 81920),
        canonical_gpu_model("H100 NVL", 96256),
    }
    assert len(models) == 3


@pytest.mark.parametrize(
    ("vram_mib", "expected"),
    [
        (22888, 24),  # AWS's usable-VRAM figure for a 24GB L4 / A10G.
        (45776, 48),  # ... and for a 48GB L40S.
        (81920, 80),
        (40960, 40),
        (None, None),
        (0, None),
        (999_999, None),  # Absurd input snaps to nothing rather than a wrong bucket.
    ],
)
def test_vram_bucketing(vram_mib: int | None, expected: int | None) -> None:
    assert bucket_vram_gb(vram_mib) == expected


def test_unknown_silicon_degrades_without_fabricating() -> None:
    """A GPU we don't model must not crash ingestion, and must not be silently mislabeled."""
    model = canonical_gpu_model("H400 Ultra", 204800)
    assert model.startswith("UNMAPPED_")
    assert not is_canonical(model)
    assert is_canonical("H100_SXM_80GB")


def test_vendor_noise_is_stripped() -> None:
    assert canonical_gpu_model("NVIDIA Tesla V100", 32768) == "V100_32GB"
