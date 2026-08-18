"""Ocean-proximity measurement, checked against real oceanfront towers."""

from __future__ import annotations

import pytest

from miami_bot.geo import (
    distance_to_ocean_miles,
    haversine_miles,
    in_coastal_bbox,
    ocean_proximity,
)

# Real buildings in the monitored corridor, with their approximate coordinates.
OCEANFRONT = {
    "Faena House (3315 Collins)": (25.8043, -80.1229),
    "The Setai (2001 Collins)": (25.7960, -80.1265),
    "Jade Signature (16901 Collins)": (25.9330, -80.1206),
    "Surf Club (9011 Collins)": (25.8776, -80.1213),
    "St Regis Bal Harbour (9703 Collins)": (25.8890, -80.1233),
}

INLAND = {
    "Sunset Harbour (bayside Miami Beach)": (25.7930, -80.1430),
    "Brickell": (25.7600, -80.1930),
}


@pytest.mark.parametrize("name,point", OCEANFRONT.items())
def test_oceanfront_towers_are_within_the_search_radius(name, point):
    miles = distance_to_ocean_miles(*point)
    assert miles is not None and miles < 0.25, f"{name} measured {miles} mi"


@pytest.mark.parametrize("name,point", INLAND.items())
def test_inland_locations_are_outside_the_search_radius(name, point):
    miles = distance_to_ocean_miles(*point)
    assert miles is not None and miles > 0.6, f"{name} measured {miles} mi"


def test_missing_or_null_island_coordinates_return_none():
    assert distance_to_ocean_miles(None, None) is None
    assert distance_to_ocean_miles(0, 0) is None
    assert distance_to_ocean_miles("abc", "def") is None


def test_coastal_bounding_box():
    assert in_coastal_bbox(25.8890, -80.1233)
    assert not in_coastal_bbox(25.7600, -80.1930)
    assert not in_coastal_bbox(None, None)


def test_haversine_matches_a_known_distance():
    # South Pointe to Bal Harbour is roughly 8.6 statute miles.
    miles = haversine_miles(25.7650, -80.1300, 25.8890, -80.1233)
    assert 8.0 < miles < 9.2


def test_proximity_describes_itself():
    assert "oceanfront" in ocean_proximity(25.8776, -80.1205).describe()
    assert ocean_proximity(None, None).describe() == "distance unknown"
