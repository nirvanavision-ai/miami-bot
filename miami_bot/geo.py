"""Ocean-proximity scoring.

"Oceanfront / near-ocean" is a distance question, and none of the portal
wrappers expose it. We approximate the Atlantic shoreline for the barrier-island
stretch we monitor (South Pointe up through Golden Beach) as a polyline and
measure the perpendicular distance from a listing's coordinates to it.

The polyline is deliberately coarse -- roughly 100 m of accuracy -- which is
well inside the tolerance needed to separate an oceanfront tower on Collins Ave
from something across the Intracoastal on Biscayne Bay.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# (latitude, longitude) ordered south -> north along the Atlantic beach edge.
ATLANTIC_SHORELINE: tuple[tuple[float, float], ...] = (
    (25.7650, -80.1300),   # South Pointe Park
    (25.7700, -80.1295),   # 5th Street
    (25.7800, -80.1280),   # 11th Street
    (25.7900, -80.1265),   # Lincoln Road / 17th
    (25.8000, -80.1245),   # 23rd Street
    (25.8130, -80.1215),   # 41st Street (Mid Beach)
    (25.8250, -80.1205),   # 53rd Street
    (25.8385, -80.1200),   # 63rd Street
    (25.8480, -80.1195),   # 71st Street (North Beach)
    (25.8600, -80.1198),   # 79th Street
    (25.8700, -80.1200),   # 87th Street
    (25.8790, -80.1200),   # Surfside 91st
    (25.8890, -80.1210),   # Bal Harbour 96th
    (25.9000, -80.1225),   # Haulover Inlet
    (25.9130, -80.1210),   # Haulover Park
    (25.9200, -80.1203),   # Sunny Isles 158th
    (25.9280, -80.1200),   # Sunny Isles 163rd
    (25.9390, -80.1190),   # Sunny Isles 174th
    (25.9500, -80.1195),   # Sunny Isles 192nd
    (25.9640, -80.1200),   # Golden Beach
)

# Rough bounding box of the monitored barrier islands; anything outside is not
# a coastal Miami condo no matter what the portal's city field claims.
COASTAL_BBOX = (25.750, 25.975, -80.160, -80.110)  # lat_min, lat_max, lon_min, lon_max

_EARTH_RADIUS_MILES = 3958.7613


@dataclass(frozen=True)
class OceanProximity:
    miles: float | None
    is_oceanfront: bool
    within_bbox: bool

    @property
    def known(self) -> bool:
        return self.miles is not None

    def describe(self) -> str:
        if self.miles is None:
            return "distance unknown"
        if self.miles < 0.06:
            return "oceanfront (<0.1 mi)"
        return f"{self.miles:.2f} mi from ocean"


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in statute miles."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def _point_to_segment_miles(
    lat: float, lon: float,
    lat_a: float, lon_a: float,
    lat_b: float, lon_b: float,
) -> float:
    """Perpendicular distance to a segment using a local equirectangular
    projection -- accurate to well under a metre over segments this short."""
    lat_ref = math.radians((lat_a + lat_b) / 2.0)
    scale = math.cos(lat_ref)

    px, py = lon * scale, lat
    ax, ay = lon_a * scale, lat_a
    bx, by = lon_b * scale, lat_b

    dx, dy = bx - ax, by - ay
    if dx == 0.0 and dy == 0.0:
        return haversine_miles(lat, lon, lat_a, lon_a)

    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    closest_lon = (ax + t * dx) / scale
    closest_lat = ay + t * dy
    return haversine_miles(lat, lon, closest_lat, closest_lon)


def distance_to_ocean_miles(
    latitude: float | None,
    longitude: float | None,
    shoreline: tuple[tuple[float, float], ...] = ATLANTIC_SHORELINE,
) -> float | None:
    """Shortest distance from a point to the shoreline polyline, in miles."""
    if latitude is None or longitude is None:
        return None
    try:
        lat, lon = float(latitude), float(longitude)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None

    return min(
        _point_to_segment_miles(lat, lon, shoreline[i][0], shoreline[i][1],
                                shoreline[i + 1][0], shoreline[i + 1][1])
        for i in range(len(shoreline) - 1)
    )


def in_coastal_bbox(latitude: float | None, longitude: float | None) -> bool:
    if latitude is None or longitude is None:
        return False
    lat_min, lat_max, lon_min, lon_max = COASTAL_BBOX
    return lat_min <= float(latitude) <= lat_max and lon_min <= float(longitude) <= lon_max


def ocean_proximity(
    latitude: float | None,
    longitude: float | None,
    oceanfront_threshold_miles: float = 0.1,
) -> OceanProximity:
    miles = distance_to_ocean_miles(latitude, longitude)
    return OceanProximity(
        miles=miles,
        is_oceanfront=miles is not None and miles <= oceanfront_threshold_miles,
        within_bbox=in_coastal_bbox(latitude, longitude),
    )
