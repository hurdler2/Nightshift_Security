"""Zone geometry (spec §17)."""

from __future__ import annotations

import pytest

from app.zones.geometry import (
    box_bottom_center,
    box_center,
    point_in_polygon,
    validate_polygon,
)

SQUARE = [(0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8)]
#: The "Warehouse Restricted" polygon from spec §17.
WAREHOUSE = [(0.51, 0.22), (0.91, 0.20), (0.94, 0.88), (0.49, 0.90)]


@pytest.mark.parametrize("point", [(0.5, 0.5), (0.21, 0.21), (0.79, 0.79)])
def test_points_inside(point):
    assert point_in_polygon(point, SQUARE)


@pytest.mark.parametrize("point", [(0.1, 0.1), (0.9, 0.5), (0.5, 0.95)])
def test_points_outside(point):
    assert not point_in_polygon(point, SQUARE)


def test_point_on_edge_counts_as_inside():
    assert point_in_polygon((0.2, 0.5), SQUARE)


def test_spec_example_polygon():
    assert point_in_polygon((0.7, 0.5), WAREHOUSE)
    assert not point_in_polygon((0.1, 0.5), WAREHOUSE)


def test_rejects_degenerate_polygon():
    with pytest.raises(ValueError, match="at least 3"):
        point_in_polygon((0.5, 0.5), [(0.1, 0.1), (0.2, 0.2)])


def test_rejects_pixel_coordinates():
    """Pixel coordinates would break the moment the stream quality changes."""
    with pytest.raises(ValueError, match="normalized"):
        validate_polygon([(0, 0), (1920, 0), (1920, 1080)])


def test_box_anchors():
    assert box_center((0.2, 0.2, 0.4, 0.6)) == pytest.approx((0.3, 0.4))
    assert box_bottom_center((0.2, 0.2, 0.4, 0.6)) == pytest.approx((0.3, 0.6))
