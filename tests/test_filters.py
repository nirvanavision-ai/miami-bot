"""Hard-constraint enforcement: every criterion from the brief, plus the
distinction between a definite failure and missing data."""

from __future__ import annotations

import pytest

from miami_bot.filters import FilterStats, evaluate, score
from tests.conftest import make_listing


def reasons(result) -> str:
    return " ".join(result.failures)


def test_a_fully_qualifying_listing_passes(criteria):
    result = evaluate(make_listing(), criteria)
    assert result.passed, result.reason


# --- budget ---------------------------------------------------------------
@pytest.mark.parametrize("price", [4999, 12001, 25000])
def test_price_outside_the_band_fails(criteria, price):
    result = evaluate(make_listing(price=price), criteria)
    assert not result.passed and "price" in reasons(result)


@pytest.mark.parametrize("price", [5000, 8500, 12000])
def test_price_inside_the_band_passes(criteria, price):
    assert evaluate(make_listing(price=price), criteria).passed


def test_a_listing_with_no_price_fails(criteria):
    result = evaluate(make_listing(price=None), criteria)
    assert not result.passed and "price" in reasons(result)


# --- size -----------------------------------------------------------------
@pytest.mark.parametrize(
    "field,value", [("beds", 1), ("baths", 1.5), ("sqft", 1199)]
)
def test_below_minimum_size_fails(criteria, field, value):
    result = evaluate(make_listing(**{field: value}), criteria)
    assert not result.passed and "size" in reasons(result)


def test_missing_sqft_warns_rather_than_failing(criteria):
    result = evaluate(make_listing(sqft=None), criteria)
    assert result.passed
    assert any("square footage" in w for w in result.warnings)


def test_missing_sqft_fails_when_the_config_demands_it(criteria):
    criteria.require_known_sqft = True
    result = evaluate(make_listing(sqft=None), criteria)
    assert not result.passed and "size" in reasons(result)


# --- location -------------------------------------------------------------
def test_bayside_listing_fails_ocean_proximity(criteria):
    result = evaluate(
        make_listing(
            address="1900 Sunset Harbour Dr #2210, Miami Beach, FL 33139",
            latitude=25.7930, longitude=-80.1430,
        ),
        criteria,
    )
    assert not result.passed and "location" in reasons(result)


def test_downtown_listing_fails_the_coastal_corridor(criteria):
    result = evaluate(
        make_listing(
            address="1300 Brickell Bay Dr #2201, Miami, FL 33131",
            city="Miami", latitude=25.7600, longitude=-80.1930,
        ),
        criteria,
    )
    assert not result.passed and "location" in reasons(result)


def test_unwhitelisted_city_fails(criteria):
    result = evaluate(
        make_listing(address="20155 NE 38th Ct #1501, Aventura, FL 33180",
                     city="Aventura", latitude=25.9560, longitude=-80.1430),
        criteria,
    )
    assert not result.passed and "location" in reasons(result)


def test_a_monitored_zip_rescues_a_mislabelled_city(criteria):
    # Portals routinely put "Miami" on Miami Beach listings.
    result = evaluate(
        make_listing(address="5959 Collins Ave #1904, Miami, FL 33140",
                     city="Miami", latitude=25.8330, longitude=-80.1210),
        criteria,
    )
    assert result.passed
    assert any("ZIP" in w for w in result.warnings)


def test_missing_coordinates_warn_but_do_not_fail(criteria):
    result = evaluate(make_listing(latitude=None, longitude=None), criteria)
    assert result.passed
    assert any("ocean distance" in w for w in result.warnings)


def test_missing_coordinates_fail_when_required(criteria):
    criteria.require_coordinates = True
    result = evaluate(make_listing(latitude=None, longitude=None), criteria)
    assert not result.passed


# --- lease terms ----------------------------------------------------------
@pytest.mark.parametrize(
    "description",
    [
        "Seasonal rental available now. Valet, concierge, pool, spa.",
        "Short term welcome. Valet, concierge, pool, spa.",
        "Perfect vacation home. Valet, concierge, pool, spa.",
        "Airbnb-ready unit. Valet, concierge, pool, spa.",
        "Daily and weekly rates. Valet, concierge, pool, spa.",
    ],
)
def test_short_term_keywords_disqualify(criteria, description):
    result = evaluate(make_listing(description=description), criteria)
    assert not result.passed and "lease_terms" in reasons(result)


def test_a_three_month_minimum_is_rejected(criteria):
    result = evaluate(
        make_listing(description="3 month minimum. Valet, concierge, pool, spa."),
        criteria,
    )
    assert not result.passed and "lease_terms" in reasons(result)


def test_a_two_year_minimum_is_rejected(criteria):
    result = evaluate(
        make_listing(description="24 month minimum lease. Valet, concierge, pool, spa."),
        criteria,
    )
    assert not result.passed and "lease_terms" in reasons(result)


def test_negated_short_term_language_still_passes(criteria):
    result = evaluate(
        make_listing(
            description=(
                "NO short term rentals - annual lease only. "
                "Valet, concierge, oceanfront pool, spa."
            )
        ),
        criteria,
    )
    assert result.passed, result.reason


def test_an_unstated_term_warns_rather_than_failing(criteria):
    result = evaluate(
        make_listing(description="Valet, concierge, oceanfront pool, spa, gym."),
        criteria,
    )
    assert result.passed
    assert any("lease_terms" in w for w in result.warnings)


def test_an_unstated_term_fails_when_the_config_demands_it(criteria):
    criteria.require_explicit_lease_term = True
    result = evaluate(
        make_listing(description="Valet, concierge, oceanfront pool, spa, gym."),
        criteria,
    )
    assert not result.passed


# --- property type --------------------------------------------------------
@pytest.mark.parametrize("property_type", ["Single Family", "TOWNHOUSE", "Multi Family", "Land"])
def test_non_condo_types_fail(criteria, property_type):
    result = evaluate(make_listing(property_type=property_type), criteria)
    assert not result.passed and "property_type" in reasons(result)


@pytest.mark.parametrize("property_type", ["Condo", "CONDOMINIUM", "condo", "Apartment"])
def test_condo_synonyms_pass(criteria, property_type):
    assert evaluate(make_listing(property_type=property_type), criteria).passed


# --- building quality -----------------------------------------------------
def test_too_few_amenities_fails(criteria):
    result = evaluate(
        make_listing(description="Annual lease. Parking included."), criteria
    )
    assert not result.passed and "building" in reasons(result)


def test_an_old_building_without_a_renovation_claim_fails(criteria):
    result = evaluate(make_listing(year_built=1972), criteria)
    assert not result.passed and "building" in reasons(result)


def test_an_old_building_passes_when_described_as_renovated(criteria):
    result = evaluate(
        make_listing(
            year_built=1972,
            description=(
                "Fully renovated 2022. Annual lease. "
                "Valet, concierge, oceanfront pool, spa, fitness center."
            ),
        ),
        criteria,
    )
    assert result.passed, result.reason


# --- scoring & stats ------------------------------------------------------
def test_closer_to_the_ocean_scores_higher(criteria):
    oceanfront = make_listing(latitude=25.8776, longitude=-80.1205)
    inland = make_listing(latitude=25.8776, longitude=-80.1280)
    evaluate(oceanfront, criteria)
    evaluate(inland, criteria)
    assert score(oceanfront, criteria) > score(inland, criteria)


def test_cheaper_within_budget_scores_higher(criteria):
    cheap, dear = make_listing(price=5500), make_listing(price=11800)
    evaluate(cheap, criteria)
    evaluate(dear, criteria)
    assert score(cheap, criteria) > score(dear, criteria)


def test_filter_stats_tallies_rejection_reasons(criteria):
    stats = FilterStats()
    for listing in (make_listing(), make_listing(price=20000), make_listing(sqft=500)):
        stats.record(evaluate(listing, criteria))
    assert stats.evaluated == 3 and stats.passed == 1
    assert dict(stats.top_reasons())["price"] == 1


# --- required amenities ---------------------------------------------------
def test_a_required_amenity_is_mandatory_not_merely_counted(criteria):
    """ocean_view alone must gate the listing, independent of the count."""
    criteria.building.required_amenities = ["ocean_view"]
    # Five amenities -- comfortably over the count of 3 -- but no ocean view.
    # The title is cleared too: the default fixture says "Oceanfront 2BR", and
    # the amenity scan reads every text field, not just the description.
    result = evaluate(
        make_listing(
            title="City-view residence",
            description=(
                "Annual lease. City and garden views. "
                "Valet, concierge, pool, spa, fitness center."
            ),
        ),
        criteria,
    )
    assert not result.passed
    assert "missing required amenity" in reasons(result)


def test_a_listing_with_the_required_amenity_passes(criteria):
    criteria.building.required_amenities = ["ocean_view"]
    result = evaluate(
        make_listing(
            description=(
                "Annual lease 6-12 months. Direct ocean views from every room. "
                "Valet, concierge, pool, spa, fitness center."
            )
        ),
        criteria,
    )
    assert result.passed, result.reason
    assert "ocean_view" in result.listing.amenity_matches


@pytest.mark.parametrize(
    "phrase",
    ["ocean view", "ocean views", "oceanfront", "direct ocean access", "sea views",
     "panoramic ocean vistas", "unobstructed ocean", "beach view", "ocean facing"],
)
def test_ocean_view_vocabulary(criteria, phrase):
    criteria.building.required_amenities = ["ocean_view"]
    result = evaluate(
        make_listing(
            description=(
                f"Annual lease. {phrase} throughout. "
                "Valet, concierge, pool, spa, gym."
            )
        ),
        criteria,
    )
    assert result.passed, f"{phrase!r} should register as an ocean view: {result.reason}"


def test_no_required_amenities_means_the_count_alone_decides(criteria):
    criteria.building.required_amenities = []
    result = evaluate(
        make_listing(description="Annual lease. Valet, concierge, pool, spa."),
        criteria,
    )
    assert result.passed


def test_two_bed_two_bath_apartments_are_accepted(criteria):
    """'Apartment' is how several portals label a condo unit for rent."""
    result = evaluate(
        make_listing(
            property_type="Apartment", beds=2, baths=2,
            description=(
                "Annual lease 6-12 months. Ocean view residence. "
                "Valet, concierge, pool, spa, gym."
            ),
        ),
        criteria,
    )
    assert result.passed, result.reason
