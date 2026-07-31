"""Canonical GPU SKU vocabulary.

Providers name the same silicon differently: Vast says ``"H100 SXM"``, AWS's
``DescribeInstanceTypes`` says ``"H100"`` and leaves the interconnect implicit in
the instance family. Comparing prices across providers is only honest if both
sides are resolved to the *same SKU*, so every provider funnels its raw name
through :func:`canonical_gpu_model`.

Two details are what make this correct rather than decorative:

* **VRAM disambiguates real SKU splits.** ``A100`` ships as both 40GB and 80GB at
  materially different prices; they are not interchangeable and must not collapse
  into one model string.
* **VRAM is bucketed, not trusted verbatim.** AWS reports an L4 as 22888 MiB
  (22.35 GiB) because some VRAM is reserved. Rounding to the nearest real-world
  bucket maps that to the 24GB part it actually is.

Unrecognized silicon degrades to a stable sanitized token rather than raising or
guessing, so a new GPU launch never crashes ingestion -- it just shows up
unmapped. :func:`is_canonical` tells callers which happened.
"""

from __future__ import annotations

import re

# Real-world VRAM sizes in GB. Reported VRAM snaps to the nearest of these.
_VRAM_BUCKETS = (8, 11, 12, 16, 20, 24, 32, 40, 48, 80, 94, 141, 180, 192)

# (regex over the cleaned vendor name, vram_gb or None for any) -> canonical SKU.
# Ordered: first match wins, so more specific variants precede bare families.
_SKU_RULES: list[tuple[re.Pattern[str], int | None, str]] = [
    # Hopper
    (re.compile(r"^H100 ?NVL$"), None, "H100_NVL_94GB"),
    (re.compile(r"^H100 ?PCIE$"), None, "H100_PCIE_80GB"),
    (re.compile(r"^H100( ?SXM\d?)?$"), None, "H100_SXM_80GB"),
    (re.compile(r"^H200 ?NVL$"), None, "H200_NVL_141GB"),
    (re.compile(r"^H200( ?SXM\d?)?$"), None, "H200_SXM_141GB"),
    # Blackwell
    (re.compile(r"^B200$"), None, "B200_180GB"),
    (re.compile(r"^B300$"), None, "B300_192GB"),
    # Ampere datacenter -- VRAM is the SKU split, so it is required here.
    (re.compile(r"^A100 ?PCIE$"), 40, "A100_PCIE_40GB"),
    (re.compile(r"^A100 ?PCIE$"), 80, "A100_PCIE_80GB"),
    (re.compile(r"^A100( ?SXM\d?)?$"), 40, "A100_SXM4_40GB"),
    (re.compile(r"^A100( ?SXM\d?)?$"), 80, "A100_SXM4_80GB"),
    (re.compile(r"^A40$"), None, "A40_48GB"),
    (re.compile(r"^A10G$"), None, "A10G_24GB"),
    # Ada / Lovelace
    (re.compile(r"^L40S$"), None, "L40S_48GB"),
    (re.compile(r"^L40$"), None, "L40_48GB"),
    (re.compile(r"^L4$"), None, "L4_24GB"),
    # Volta / Turing
    (re.compile(r"^V100$"), 16, "V100_16GB"),
    (re.compile(r"^V100$"), 32, "V100_32GB"),
    (re.compile(r"^T4$"), None, "T4_16GB"),
    # Consumer cards that show up on marketplaces
    (re.compile(r"^RTX 4090$"), None, "RTX_4090_24GB"),
    (re.compile(r"^RTX 5090$"), None, "RTX_5090_32GB"),
    (re.compile(r"^RTX 3090$"), None, "RTX_3090_24GB"),
]

# Vendor prefixes and marketing words that carry no SKU information.
_NOISE = re.compile(r"\b(NVIDIA|TESLA|GEFORCE|QUADRO)\b")

# AWS omits the interconnect from the GPU name; the instance family implies it.
# EC2 offers no PCIe H100, so p5 is unambiguously SXM.
_AWS_FAMILY_VARIANT = {
    "p5": "SXM",
    "p5e": "SXM",
    "p5en": "SXM",
    "p4d": "SXM4",
    "p4de": "SXM4",
    "p3": "SXM2",
}


def bucket_vram_gb(vram_mib: int | None) -> int | None:
    """Snap reported VRAM to the nearest real-world size.

    Providers report usable rather than nominal VRAM (AWS calls a 24GB L4 22888
    MiB), so exact comparison would fail. Returns ``None`` if VRAM is unknown or
    is too far from any real SKU to be trustworthy.
    """
    if not vram_mib or vram_mib <= 0:
        return None
    gb = vram_mib / 1024
    nearest = min(_VRAM_BUCKETS, key=lambda b: abs(b - gb))
    # Guard against snapping wildly-off values onto a bucket they aren't.
    return nearest if abs(nearest - gb) / nearest <= 0.15 else None


def canonical_gpu_model(
    raw_name: str,
    vram_mib: int | None = None,
    aws_instance_family: str | None = None,
) -> str:
    """Resolve a provider's GPU name to a canonical cross-provider SKU.

    ``aws_instance_family`` supplies the interconnect that AWS leaves out of the
    GPU name (``p4d`` + ``"A100"`` -> SXM4). Falls back to ``UNMAPPED_<name>`` for
    silicon we do not model, which keeps ingestion alive without inventing a SKU.
    """
    name = _NOISE.sub("", (raw_name or "").upper())
    name = re.sub(r"\s+", " ", name).strip()
    if not name:
        return "UNKNOWN"

    if aws_instance_family:
        variant = _AWS_FAMILY_VARIANT.get(aws_instance_family.lower())
        # Only inject the variant when AWS gave us a bare family name.
        if variant and not re.search(r"SXM|PCIE|NVL", name):
            name = f"{name} {variant}"

    vram_gb = bucket_vram_gb(vram_mib)
    for pattern, required_vram, canonical in _SKU_RULES:
        if pattern.match(name) and (required_vram is None or required_vram == vram_gb):
            return canonical

    slug = re.sub(r"[^A-Z0-9]+", "_", name).strip("_")
    return f"UNMAPPED_{slug}{f'_{vram_gb}GB' if vram_gb else ''}"


def is_canonical(gpu_model: str) -> bool:
    """False for SKUs we could not resolve, so tests and dashboards can flag them."""
    return not gpu_model.startswith(("UNMAPPED_", "UNKNOWN"))
