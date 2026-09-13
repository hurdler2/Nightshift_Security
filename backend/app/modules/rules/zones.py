"""Camera zones evaluated cloud-side on the alarm snapshot (spec §10.1).

We cannot push zone geometry to the recorder, so zones live here: the operator draws
a polygon on the camera's last snapshot, and detections from the AI service are tested
against it. Because everything is in normalized 0..1 coordinates, a zone survives a
change of resolution or stream quality.

Geometry is duplicated from `ai-service` on purpose: the two are separate deployables
and neither may import the other. The rules that matter — normalized coordinates,
edges count as inside — are pinned by tests on both sides.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

Point = tuple[float, float]
Polygon = list[Point]
Box = tuple[float, float, float, float]


class ZoneType(enum.StrEnum):
    NORMAL = "NORMAL"
    RESTRICTED = "RESTRICTED"
    #: Detections here are dropped before scoring — the road, a neighbour's yard.
    IGNORE = "IGNORE"
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    VEHICLE_ONLY = "VEHICLE_ONLY"
    FIRE_RISK = "FIRE_RISK"


#: Zones that raise the stakes when someone is inside them.
ELEVATED_ZONES = frozenset({ZoneType.RESTRICTED, ZoneType.FIRE_RISK})


class InvalidZone(ValueError):
    pass


def validate_polygon(points: Polygon) -> None:
    if len(points) < 3:
        raise InvalidZone("a zone needs at least 3 points")
    for x, y in points:
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise InvalidZone("zone coordinates must be normalized to 0..1")


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


def ground_anchor(box: Box) -> Point:
    """Where the subject stands: bottom-centre of the box.

    Using the centre would put a person "inside" a ground zone as soon as their head
    crossed it, which is wrong at every camera angle a site actually uses.
    """
    x1, _, x2, y2 = box
    return ((x1 + x2) / 2, y2)


@dataclass(frozen=True, slots=True)
class Zone:
    name: str
    type: ZoneType
    points: Polygon = field(default_factory=list)

    def __post_init__(self) -> None:
        validate_polygon(self.points)

    def contains(self, point: Point) -> bool:
        return point_in_polygon(point, self.points)

    def contains_box(self, box: Box) -> bool:
        return self.contains(ground_anchor(box))


@dataclass(slots=True)
class ZoneMatch:
    """Which zones a detection landed in, and what that means for scoring."""

    zones: list[Zone] = field(default_factory=list)

    @property
    def names(self) -> list[str]:
        return [z.name for z in self.zones]

    @property
    def types(self) -> set[ZoneType]:
        return {z.type for z in self.zones}

    @property
    def is_ignored(self) -> bool:
        """IGNORE wins over everything: it exists to silence a known-noisy area."""
        return ZoneType.IGNORE in self.types

    @property
    def is_elevated(self) -> bool:
        return bool(ELEVATED_ZONES & self.types) and not self.is_ignored

    @property
    def restricted(self) -> bool:
        return ZoneType.RESTRICTED in self.types and not self.is_ignored


def match_zones(box: Box, zones: list[Zone]) -> ZoneMatch:
    """Zones whose polygon contains the subject's ground point."""
    anchor = ground_anchor(box)
    return ZoneMatch(zones=[zone for zone in zones if zone.contains(anchor)])


def match_any(boxes: list[Box], zones: list[Zone]) -> ZoneMatch:
    """Union of the zones hit by any detection in the frame.

    An IGNORE hit by one detection does not excuse another detection standing in a
    restricted zone, so IGNORE is only honoured when *every* detection is in one.
    """
    if not boxes:
        return ZoneMatch()
    matches = [match_zones(box, zones) for box in boxes]
    if all(m.is_ignored for m in matches):
        return matches[0]
    interesting = [m for m in matches if not m.is_ignored]
    union: list[Zone] = []
    for match in interesting:
        for zone in match.zones:
            if zone not in union:
                union.append(zone)
    return ZoneMatch(zones=union)
