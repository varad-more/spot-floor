"""The sparkline's one correctness claim: a gap is drawn as a gap.

Everything else is cosmetics. Joining across a missing bucket would draw a
straight line between two real observations and assert we watched the price
glide between them, which is exactly the fabrication the storage layer refuses
to commit.
"""

from __future__ import annotations

import re

from ec2_spot_prices.web.sparkline import sparkline_svg


def polylines(svg: str) -> list[str]:
    return re.findall(r"<polyline\b[^>]*>", svg)


def test_a_gap_breaks_the_path_into_separate_polylines() -> None:
    svg = sparkline_svg([1.0, 2.0, None, 3.0, 4.0])
    assert len(polylines(svg)) == 2, "the path was joined across a missing bucket"


def test_a_continuous_series_is_one_path() -> None:
    svg = sparkline_svg([1.0, 2.0, 3.0, 4.0])
    assert len(polylines(svg)) == 1


def test_an_isolated_observation_is_drawn_as_a_dot() -> None:
    """A one-point polyline renders nothing, so sparse data would look like no data."""
    svg = sparkline_svg([None, 5.0, None])
    assert polylines(svg) == []
    assert svg.count("<circle") == 1


def test_nothing_observed_renders_an_explicit_empty_chart() -> None:
    svg = sparkline_svg([None, None, None])
    assert "spark-empty" in svg
    assert "<polyline" not in svg and "<circle" not in svg
    assert "no observations" in svg


def test_a_flat_series_does_not_divide_by_zero() -> None:
    svg = sparkline_svg([2.0, 2.0, 2.0])
    assert len(polylines(svg)) == 1
    assert "nan" not in svg.lower()


def test_higher_prices_sit_higher_on_the_chart() -> None:
    """SVG's y axis grows downward, so the mapping has to be inverted."""
    svg = sparkline_svg([1.0, 2.0])
    points = re.search(r'points="([^"]+)"', svg).group(1).split()
    y_cheap = float(points[0].split(",")[1])
    y_dear = float(points[1].split(",")[1])
    assert y_dear < y_cheap


def test_a_single_observation_overall_still_renders() -> None:
    svg = sparkline_svg([3.0])
    assert "spark-empty" not in svg
    assert svg.count("<circle") == 1
