"""Normalized polygon geometry for camera zones (spec §17).

All coordinates are 0..1 so a zone survives resolution and stream-quality changes.
"""

from __future__ import annotations

Point = tuple[float, float]
Polygon = list[Point]


def validate_polygon(points: Polygon) -> None:
    if len(points) < 3:
        raise ValueError("a zone needs at least 3 points")
    for x, y in points:
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise ValueError("zone coordinates must be normalized to 0..1")


def point_in_polygon(point: Point, polygon: Polygon) -> bool:
    """Ray casting; a point exactly on an edge counts as inside."""
    validate_polygon(polygon)
    x, y = point
    inside = False
    count = len(polygon)
    for index in range(count):
        x1, y1 = polygon[index]
        x2, y2 = polygon[(index + 1) % count]
        if _on_segment(point, (x1, y1), (x2, y2)):
            return True
        if (y1 > y) != (y2 > y):
            x_intersect = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < x_intersect:
                inside = not inside
    return inside


def _on_segment(p: Point, a: Point, b: Point, tolerance: float = 1e-9) -> bool:
    cross = (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
    if abs(cross) > tolerance:
        return False
    within_x = min(a[0], b[0]) - tolerance <= p[0] <= max(a[0], b[0]) + tolerance
    within_y = min(a[1], b[1]) - tolerance <= p[1] <= max(a[1], b[1]) + tolerance
    return within_x and within_y


def box_center(box: tuple[float, float, float, float]) -> Point:
    x1, y1, x2, y2 = box
    return ((x1 + x2) / 2, (y1 + y2) / 2)


def box_bottom_center(box: tuple[float, float, float, float]) -> Point:
    """Where a person actually stands — the right anchor for ground zones."""
    x1, _, x2, y2 = box
    return ((x1 + x2) / 2, y2)
