"""The no-key and free-tier listing sources, plus the filter behaviour that
makes description-less feeds usable at all."""

from __future__ import annotations

import pytest
import responses

from miami_bot.config import CraigslistSettings, RentCastSettings, Settings
from miami_bot.filters import evaluate
from miami_bot.sources.craigslist import CraigslistSource
from miami_bot.sources.rentcast_listings import RentCastListingsSource
from tests.conftest import make_listing

# --- fixtures ---------------------------------------------------------------
CRAIGSLIST_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns="http://purl.org/rss/1.0/"
         xmlns:dc="http://purl.org/dc/elements/1.1/">
  <item rdf:about="https://miami.craigslist.org/mdc/apa/d/x/7712345678.html">
    <title>$9,500 / 2br - 1450ft2 - Direct ocean view, annual lease (bal harbour)</title>
    <link>https://miami.craigslist.org/mdc/apa/d/x/7712345678.html</link>
    <description>Oceanfront 2 bed 2 bath. 12 month lease. Valet, concierge, pool, spa, gym.</description>
    <dc:date>2026-08-17T09:12:00-04:00</dc:date>
  </item>
  <item rdf:about="https://miami.craigslist.org/mdc/apa/d/y/7712345679.html">
    <title>$7,000 / 2br - 1300ft2 - Seasonal beachfront (miami beach)</title>
    <link>https://miami.craigslist.org/mdc/apa/d/y/7712345679.html</link>
    <description>Ocean view. Seasonal rental, 3 month minimum. Valet, pool, spa.</description>
    <dc:date>2026-08-17T08:00:00-04:00</dc:date>
  </item>
</rdf:RDF>"""

# RSS 2.0 layout -- Craigslist has served both over the years.
CRAIGSLIST_RSS2 = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>$8,200 / 2br - 1250ft2 - Ocean views, annual (surfside)</title>
    <link>https://miami.craigslist.org/mdc/apa/d/z/7712345680.html</link>
    <description>Annual lease only. Valet, concierge, pool.</description>
    <pubDate>Mon, 17 Aug 2026 12:00:00 -0400</pubDate>
  </item>
</channel></rss>"""

RENTCAST_LISTINGS = [
    {
        "id": "9705-Collins-Ave,-Apt-1502N,-Bal-Harbour,-FL-33154",
        "formattedAddress": "9705 Collins Ave, Apt 1502N, Bal Harbour, FL 33154",
        "addressLine1": "9705 Collins Ave",
        "addressLine2": "Apt 1502N",
        "city": "Bal Harbour", "state": "FL", "zipCode": "33154",
        "latitude": 25.8890, "longitude": -80.1233,
        "propertyType": "Condo", "bedrooms": 2, "bathrooms": 2.5,
        "squareFootage": 1450, "yearBuilt": 2018,
        "status": "Active", "price": 9500, "daysOnMarket": 6,
        "listedDate": "2026-08-11T00:00:00.000Z",
        "listingOffice": {"name": "Douglas Elliman"},
    },
    {
        "id": "too-small",
        "addressLine1": "5959 Collins Ave", "addressLine2": "Apt 904",
        "city": "Miami Beach", "state": "FL", "zipCode": "33140",
        "latitude": 25.8330, "longitude": -80.1210,
        "propertyType": "Condo", "bedrooms": 1, "bathrooms": 1,
        "squareFootage": 780, "status": "Active", "price": 6200,
    },
]


# --- Craigslist -------------------------------------------------------------
@responses.activate
def test_craigslist_parses_the_rss_feed(http, criteria):
    responses.add(responses.GET, "https://miami.craigslist.org/search/apa",
                  body=CRAIGSLIST_RSS, status=200)
    criteria.zip_codes = ["33154"]
    result = CraigslistSource(http).fetch(criteria)

    assert result.ok
    assert result.count == 2
    ocean = next(item for item in result.listings if item.price == 9500)
    assert ocean.beds == 2
    assert ocean.sqft == 1450
    assert ocean.source_id == "7712345678"
    assert ocean.url.endswith("7712345678.html")
    assert ocean.city == "Bal Harbour"        # alias map title-cases the neighbourhood
    assert ocean.lease_term.min_months == 12


@responses.activate
def test_craigslist_handles_the_rss_2_layout(http, criteria):
    responses.add(responses.GET, "https://miami.craigslist.org/search/apa",
                  body=CRAIGSLIST_RSS2, status=200)
    criteria.zip_codes = ["33154"]
    result = CraigslistSource(http).fetch(criteria)
    assert result.count == 1
    assert result.listings[0].price == 8200 and result.listings[0].sqft == 1250


@responses.activate
def test_craigslist_short_term_postings_are_still_rejected(http, criteria):
    responses.add(responses.GET, "https://miami.craigslist.org/search/apa",
                  body=CRAIGSLIST_RSS, status=200)
    criteria.zip_codes = ["33154"]
    criteria.enforce_city_whitelist = False       # CL gives neighbourhoods, not cities
    listings = CraigslistSource(http).fetch(criteria).listings

    seasonal = next(item for item in listings if item.price == 7000)
    result = evaluate(seasonal, criteria)
    assert not result.passed
    assert "lease_terms" in " ".join(result.failures)


@responses.activate
def test_craigslist_sends_the_search_constraints(http, criteria):
    responses.add(responses.GET, "https://miami.craigslist.org/search/apa",
                  body=CRAIGSLIST_RSS, status=200)
    criteria.zip_codes = ["33154"]
    CraigslistSource(http).fetch(criteria)
    url = responses.calls[0].request.url
    assert "format=rss" in url
    assert "min_price=5000" in url and "max_price=12000" in url
    assert "min_bedrooms=2" in url and "minSqft=1200" in url
    assert "postal=33154" in url


@responses.activate
def test_a_malformed_feed_is_survivable(http, criteria):
    responses.add(responses.GET, "https://miami.craigslist.org/search/apa",
                  body="<not xml at all", status=200)
    criteria.zip_codes = ["33154"]
    result = CraigslistSource(http).fetch(criteria)
    assert result.ok and result.count == 0


def test_craigslist_is_off_unless_enabled(http, criteria):
    result = CraigslistSource(http, enabled=False).fetch(criteria)
    assert result.skipped


def test_craigslist_settings_default_to_off(monkeypatch):
    assert CraigslistSettings.from_env().enabled is False
    monkeypatch.setenv("CRAIGSLIST_ENABLED", "true")
    assert CraigslistSettings.from_env().enabled is True


# --- RentCast listings ------------------------------------------------------
@responses.activate
def test_rentcast_listings_source(http, criteria):
    responses.add(responses.GET, "https://api.rentcast.io/v1/listings/rental/long-term",
                  json=RENTCAST_LISTINGS, status=200)
    criteria.zip_codes = ["33154"]
    settings = RentCastSettings(api_key="rc-key")
    result = RentCastListingsSource(http, settings).fetch(criteria)

    assert result.ok
    assert result.count == 1            # the 1bd/780sqft record is pre-filtered out
    listing = result.listings[0]
    assert listing.price == 9500
    assert listing.beds == 2 and listing.baths == 2.5 and listing.sqft == 1450
    assert listing.unit == "1502n"
    assert listing.city == "Bal Harbour"
    assert listing.broker == "Douglas Elliman"
    assert responses.calls[0].request.headers["X-Api-Key"] == "rc-key"
    assert "zipCode=33154" in responses.calls[0].request.url


@responses.activate
def test_rentcast_pushes_the_constraints_server_side(http, criteria):
    """bedrooms/bathrooms/squareFootage/price accept numeric ranges, so the
    budget and layout are filtered by RentCast rather than in wasted slots."""
    responses.add(responses.GET, "https://api.rentcast.io/v1/listings/rental/long-term",
                  json=RENTCAST_LISTINGS, status=200)
    criteria.zip_codes = ["33154"]
    RentCastListingsSource(http, RentCastSettings(api_key="k")).fetch(criteria)
    url = responses.calls[0].request.url
    assert "price=5000%3A12000" in url or "price=5000:12000" in url
    assert "bedrooms=2%3A" in url or "bedrooms=2:" in url
    assert "squareFootage=1200%3A" in url or "squareFootage=1200:" in url
    assert "status=Active" in url


@responses.activate
def test_an_empty_ranged_query_is_retried_unfiltered(http, criteria):
    """The range syntax is not pinned in the OpenAPI spec, so a mismatch must
    cost one extra call rather than silently finding nothing."""
    responses.add(responses.GET, "https://api.rentcast.io/v1/listings/rental/long-term",
                  json=[], status=200)
    responses.add(responses.GET, "https://api.rentcast.io/v1/listings/rental/long-term",
                  json=RENTCAST_LISTINGS, status=200)
    criteria.zip_codes = ["33154"]
    result = RentCastListingsSource(http, RentCastSettings(api_key="k")).fetch(criteria)
    assert len(responses.calls) == 2
    assert "price" not in responses.calls[1].request.url
    assert result.count == 1


@responses.activate
def test_a_rejected_range_query_is_retried_unfiltered(http, criteria):
    responses.add(responses.GET, "https://api.rentcast.io/v1/listings/rental/long-term",
                  json={"error": "bad range"}, status=400)
    responses.add(responses.GET, "https://api.rentcast.io/v1/listings/rental/long-term",
                  json=RENTCAST_LISTINGS, status=200)
    criteria.zip_codes = ["33154"]
    result = RentCastListingsSource(http, RentCastSettings(api_key="k")).fetch(criteria)
    assert result.ok and result.count == 1
    assert any("range filters rejected" in note for note in result.notes)


@responses.activate
def test_rentcast_listings_cost_one_call_per_zip(http, criteria):
    responses.add(responses.GET, "https://api.rentcast.io/v1/listings/rental/long-term",
                  json=RENTCAST_LISTINGS, status=200)
    criteria.zip_codes = ["33139", "33140", "33141", "33154", "33160"]
    result = RentCastListingsSource(http, RentCastSettings(api_key="k")).fetch(criteria)
    assert result.requests_made == 5      # free tier: 50/month => 10 runs


@responses.activate
def test_truncated_results_are_reported(http, criteria):
    responses.add(responses.GET, "https://api.rentcast.io/v1/listings/rental/long-term",
                  json=RENTCAST_LISTINGS, status=200,
                  headers={"X-Total-Count": "999"})
    criteria.zip_codes = ["33154"]
    result = RentCastListingsSource(http, RentCastSettings(api_key="k")).fetch(criteria)
    assert any("999 listings matched" in note for note in result.notes)


@responses.activate
def test_the_mls_number_survives_the_missing_listing_url(http, criteria):
    """RentCast returns no URL, so the MLS number is the only lookup handle."""
    record = dict(RENTCAST_LISTINGS[0], mlsNumber="A11234567")
    responses.add(responses.GET, "https://api.rentcast.io/v1/listings/rental/long-term",
                  json=[record], status=200)
    criteria.zip_codes = ["33154"]
    listing = RentCastListingsSource(http, RentCastSettings(api_key="k")).fetch(criteria).listings[0]
    assert listing.url == ""
    assert "MLS A11234567" in listing.broker
    assert "Douglas Elliman" in listing.broker


@responses.activate
def test_rentcast_listings_warn_about_missing_prose(http, criteria):
    responses.add(responses.GET, "https://api.rentcast.io/v1/listings/rental/long-term",
                  json=RENTCAST_LISTINGS, status=200)
    criteria.zip_codes = ["33154"]
    result = RentCastListingsSource(http, RentCastSettings(api_key="k")).fetch(criteria)
    assert any("no description" in note for note in result.notes)


def test_rentcast_listings_disabled_when_not_used_as_a_source(http, criteria):
    settings = RentCastSettings(api_key="k", use_as_listing_source=False)
    assert RentCastListingsSource(http, settings, enabled=False).fetch(criteria).skipped


def test_the_same_unit_from_rentcast_and_a_wrapper_dedupes(http):
    settings = RentCastSettings(api_key="k")
    from_rentcast = RentCastListingsSource(http, settings).to_listing(RENTCAST_LISTINGS[0])
    from_wrapper = make_listing()      # 9705 Collins Ave #1502N
    assert from_rentcast.dedupe_key == from_wrapper.dedupe_key


# --- the filter behaviour that makes lean feeds usable ----------------------
def test_a_listing_with_no_prose_is_flagged_not_rejected(criteria):
    """RentCast publishes no description; judging it to have zero amenities
    would describe the feed, not the building."""
    criteria.building.required_amenities = ["ocean_view"]
    bare = make_listing(title="", description="", amenities=[])
    result = evaluate(bare, criteria)
    assert result.passed, result.reason
    assert any("no description" in w for w in result.warnings)
    assert any("ocean view" in w for w in result.warnings)


def test_that_leniency_can_be_switched_off(criteria):
    criteria.building.required_amenities = ["ocean_view"]
    criteria.building.require_amenity_evidence = True
    bare = make_listing(title="", description="", amenities=[])
    result = evaluate(bare, criteria)
    assert not result.passed


def test_a_listing_with_prose_is_still_judged_strictly(criteria):
    """The leniency must not leak into listings that do carry a description."""
    criteria.building.required_amenities = ["ocean_view"]
    described = make_listing(
        title="City-view residence",
        description="Annual lease. City and garden views. Valet, concierge, pool, spa, gym.",
    )
    result = evaluate(described, criteria)
    assert not result.passed
    assert "missing required amenity" in " ".join(result.failures)


@pytest.mark.parametrize(
    "title,description,expected",
    [
        ("", "", False),
        ("Oceanfront 2BR", "", False),                       # too short to judge
        ("", "Annual lease with direct ocean views and a spa", True),
        ("Oceanfront 2BR at Bal Harbour with valet parking", "", True),
    ],
)
def test_descriptive_text_detection(title, description, expected):
    listing = make_listing(title=title, description=description, amenities=[])
    assert listing.has_descriptive_text is expected


def test_config_exposes_the_new_source_toggles(settings: Settings):
    status = settings.provider_status()
    assert "craigslist" in status and "rentcast_listings" in status
    assert "CRAIGSLIST_ENABLED" in " ".join(settings.warnings())
