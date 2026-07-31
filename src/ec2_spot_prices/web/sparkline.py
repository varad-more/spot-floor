"""Inline SVG sparklines, rendered server-side.

No charting library and no CDN: the chart is a string of SVG built from the same
numbers the API returns, so the page has no external dependencies and the drawing
is a pure function you can assert on.

The one rule that matters here is the one it would be easiest to violate for a
prettier line: **a gap is drawn as a gap.** ``None`` means the floor was not
observed in that bucket, so the path breaks. Joining across it would draw a
straight line between two real observations and imply we watched the price glide
between them.
"""

from __future__ import annotations

from typing import Sequence

# An isolated observation between two gaps is a run of length one. A <polyline>
# with a single point renders nothing at all, so those are drawn as dots instead
# -- otherwise a sparse series looks like no data rather than intermittent data.
_DOT_RADIUS = 1.6


def _runs(values: Sequence[float | None]) -> list[list[tuple[int, float]]]:
    """Split into maximal runs of consecutive observed values, keeping x indices."""
    runs: list[list[tuple[int, float]]] = []
    current: list[tuple[int, float]] = []
    for index, value in enumerate(values):
        if value is None:
            if current:
                runs.append(current)
                current = []
        else:
            current.append((index, value))
    if current:
        runs.append(current)
    return runs


def sparkline_svg(
    values: Sequence[float | None],
    *,
    width: int = 168,
    height: int = 34,
    pad: float = 3.0,
    stroke: str = "currentColor",
) -> str:
    """Render a price series as inline SVG. Returns an empty-state SVG if nothing observed."""
    observed = [v for v in values if v is not None]
    if not observed:
        return (
            f'<svg class="spark spark-empty" viewBox="0 0 {width} {height}" '
            f'width="{width}" height="{height}" role="img" '
            f'aria-label="no observations in this window"></svg>'
        )

    low, high = min(observed), max(observed)
    span = high - low

    inner_h = height - 2 * pad
    inner_w = width - 2 * pad
    step = inner_w / max(len(values) - 1, 1)

    def x_of(index: int) -> float:
        return pad + index * step

    def y_of(value: float) -> float:
        # A flat series has no range to normalize against; centre it rather than
        # dividing by zero or pinning it to the axis.
        if span == 0:
            return pad + inner_h / 2
        # SVG y grows downward, so a high price must map to a small y.
        return pad + inner_h * (1 - (value - low) / span)

    parts: list[str] = []
    for run in _runs(values):
        if len(run) == 1:
            index, value = run[0]
            parts.append(
                f'<circle cx="{x_of(index):.2f}" cy="{y_of(value):.2f}" '
                f'r="{_DOT_RADIUS}" fill="{stroke}"/>'
            )
            continue
        points = " ".join(f"{x_of(i):.2f},{y_of(v):.2f}" for i, v in run)
        parts.append(
            f'<polyline points="{points}" fill="none" stroke="{stroke}" '
            f'stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>'
        )

    label = (
        # Whole-instance $/hr: the caller passes `floor_usd_hr`, which is
        # `price_usd_hr`, not the per-GPU view. Screen readers were being told
        # every row -- CPU boxes included -- was priced per GPU.
        f"price range ${low:.4f} to ${high:.4f} per hour "
        f"over {len(values)} buckets, {len(observed)} observed"
    )
    return (
        f'<svg class="spark" viewBox="0 0 {width} {height}" width="{width}" '
        f'height="{height}" preserveAspectRatio="none" role="img" '
        f'aria-label="{label}">{"".join(parts)}</svg>'
    )
