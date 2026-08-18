"""Module 3: AVM comparison and county-record validation."""

from __future__ import annotations

import pytest
import responses

from miami_bot.enrich.enricher import Enricher
from miami_bot.enrich.housecanary import HouseCanaryClient
from miami_bot.enrich.miamidade import MiamiDadeClient
from miami_bot.enrich.rentcast import RentCastClient
from miami_bot.filters import evaluate
from tests.conftest import make_listing
from tests.fixtures import payloads


@pytest.fixture
def configured(settings):
    settings.rentcast.api_key = "rc-key"
    settings.housecanary.api_key = "hc-key"
    settings.housecanary.api_secret = "hc-secret"
    settings.pipeline.enrichment.housecanary = True
    settings.county.enabled = True
    return settings


# ------------------------------------------------------------------ RentCast
@responses.activate
def test_rentcast_rent_avm(http, configured, listing):
    responses.add(
        responses.GET, "https://api.rentcast.io/v1/avm/rent/long-term",
        json=payloads.RENTCAST_RENT_AVM, status=200,
    )
    estimate = RentCastClient(http, configured.rentcast).rent_estimate(listing)
    assert estimate.rent == 8900 and estimate.low == 8300 and estimate.high == 9500
    assert estimate.comparable_count == 3

    request = responses.calls[0].request
    assert request.headers["X-Api-Key"] == "rc-key"
    # Unit specs must be sent, or the AVM prices the building rather than the unit.
    assert "squareFootage=1450" in request.url
    assert "bedrooms=2" in request.url
    assert "propertyType=Condo" in request.url


@responses.activate
def test_rentcast_returns_none_when_the_address_is_unknown(http, configured, listing):
    responses.add(
        responses.GET, "https://api.rentcast.io/v1/avm/rent/long-term",
        json={"error": "not found"}, status=404,
    )
    assert RentCastClient(http, configured.rentcast).rent_estimate(listing) is None


def test_rentcast_skips_an_unusable_address(http, configured):
    listing = make_listing(address="Address withheld", latitude=None, longitude=None)
    assert RentCastClient(http, configured.rentcast).rent_estimate(listing) is None


# --------------------------------------------------------------- HouseCanary
@responses.activate
def test_housecanary_reads_both_components_from_one_call(http, configured, listing):
    responses.add(
        responses.GET, "https://api.housecanary.com/v2/property/component_mget",
        json=payloads.HOUSECANARY_MGET, status=200,
    )
    rent, value = HouseCanaryClient(http, configured.housecanary).estimates(listing)
    assert rent.rent == 9100 and rent.source == "housecanary"
    assert value == 1920000
    assert len(responses.calls) == 1


@responses.activate
def test_housecanary_honours_a_non_zero_api_code(http, configured, listing):
    responses.add(
        responses.GET, "https://api.housecanary.com/v2/property/component_mget",
        json=[{"property/value_rental": {"api_code": 204,
                                         "api_code_description": "no data"}}],
        status=200,
    )
    rent, value = HouseCanaryClient(http, configured.housecanary).estimates(listing)
    assert rent is None and value is None


# ------------------------------------------------------------- Miami-Dade
@responses.activate
def test_county_lookup_picks_the_matching_unit(http, configured, listing):
    responses.add(
        responses.GET, configured.county.pa_proxy_url,
        json=payloads.MIAMIDADE_ADDRESS_SEARCH, status=200,
    )
    responses.add(
        responses.GET, configured.county.pa_proxy_url,
        json=payloads.MIAMIDADE_FOLIO_DETAIL, status=200,
    )
    record = MiamiDadeClient(http, configured.county).lookup(listing)
    assert record is not None
    assert record.folio == "1222350010720"       # unit 1502N, not 1503N
    assert record.sqft == 1180                   # heated area, not effective area
    assert record.year_built == 2016


@responses.activate
def test_county_falls_back_to_gis_when_the_pa_proxy_is_down(http, configured, listing):
    responses.add(responses.GET, configured.county.pa_proxy_url, status=503)
    responses.add(
        responses.GET, configured.county.gis_url,
        json={"features": [{"attributes": {
            "FOLIO": "1222350010720", "TRUE_SITE_ADDR": "9705 COLLINS AVE",
            "TRUE_SITE_UNIT": "1502N", "BLDG_EFFECTIVE_AREA": 1240, "YEAR_BUILT": 2016,
        }}]},
        status=200,
    )
    record = MiamiDadeClient(http, configured.county).lookup(listing)
    assert record is not None and record.source == "miamidade_gis"
    assert record.sqft == 1240


@responses.activate
def test_a_total_county_outage_is_not_fatal(http, configured, listing):
    responses.add(responses.GET, configured.county.pa_proxy_url, status=503)
    responses.add(responses.GET, configured.county.gis_url, status=500)
    assert MiamiDadeClient(http, configured.county).lookup(listing) is None


# ------------------------------------------------------------------ Enricher
@responses.activate
def test_enrichment_fills_gaps_and_prices_the_listing(http, configured):
    responses.add(responses.GET, configured.county.pa_proxy_url,
                  json=payloads.MIAMIDADE_ADDRESS_SEARCH, status=200)
    responses.add(responses.GET, configured.county.pa_proxy_url,
                  json={"PropertyInfo": {"BuildingHeatedArea": 1450, "YearBuilt": 2018}},
                  status=200)
    responses.add(responses.GET, "https://api.rentcast.io/v1/avm/rent/long-term",
                  json=payloads.RENTCAST_RENT_AVM, status=200)
    responses.add(responses.GET, "https://api.rentcast.io/v1/avm/value",
                  json=payloads.RENTCAST_VALUE_AVM, status=200)

    listing = make_listing(sqft=None, year_built=None)
    Enricher(http, configured).enrich(listing)

    assert listing.sqft == 1450 and listing.year_built == 2018
    enrichment = listing.enrichment
    assert enrichment.rent_estimate == 8900
    assert enrichment.price_to_estimate_ratio == pytest.approx(9500 / 8900, rel=1e-3)
    assert enrichment.verdict == "at market"
    assert enrichment.gross_yield_pct == pytest.approx(6.16, rel=1e-2)


@responses.activate
def test_the_county_overrides_an_inflated_marketing_sqft(http, configured, criteria):
    responses.add(responses.GET, configured.county.pa_proxy_url,
                  json=payloads.MIAMIDADE_ADDRESS_SEARCH, status=200)
    responses.add(responses.GET, configured.county.pa_proxy_url,
                  json=payloads.MIAMIDADE_FOLIO_DETAIL, status=200)   # 1180 sqft
    responses.add(responses.GET, "https://api.rentcast.io/v1/avm/rent/long-term",
                  json=payloads.RENTCAST_RENT_AVM, status=200)
    responses.add(responses.GET, "https://api.rentcast.io/v1/avm/value",
                  json=payloads.RENTCAST_VALUE_AVM, status=200)

    listing = make_listing(sqft=1650)            # the listing claims 1,650
    assert evaluate(listing, criteria).passed    # ...and passes on that claim

    Enricher(http, configured).enrich(listing)
    assert listing.sqft == 1180
    assert listing.enrichment.county_conflict

    # Re-running the filter after enrichment is what catches it.
    result = evaluate(listing, criteria)
    assert not result.passed and "size" in " ".join(result.failures)


@responses.activate
def test_an_overpriced_listing_is_labelled(http, configured):
    responses.add(responses.GET, "https://api.rentcast.io/v1/avm/rent/long-term",
                  json={"rent": 7000}, status=200)
    responses.add(responses.GET, "https://api.rentcast.io/v1/avm/value", status=404)
    configured.county.enabled = False
    listing = make_listing(price=9500)
    Enricher(http, configured).enrich(listing)
    assert listing.enrichment.verdict == "above market"


@responses.activate
def test_a_bargain_is_labelled(http, configured):
    responses.add(responses.GET, "https://api.rentcast.io/v1/avm/rent/long-term",
                  json={"rent": 11000}, status=200)
    responses.add(responses.GET, "https://api.rentcast.io/v1/avm/value", status=404)
    configured.county.enabled = False
    listing = make_listing(price=9500)
    Enricher(http, configured).enrich(listing)
    assert listing.enrichment.verdict == "below market"


def test_enrichment_is_disabled_without_providers(http, settings):
    settings.county.enabled = False
    enricher = Enricher(http, settings)
    assert not enricher.enabled
    enricher.enrich_all([make_listing()])       # must not raise


@responses.activate
def test_a_provider_error_is_captured_not_raised(http, configured):
    responses.add(responses.GET, configured.county.pa_proxy_url, status=500)
    responses.add(responses.GET, configured.county.gis_url, status=500)
    responses.add(responses.GET, "https://api.rentcast.io/v1/avm/rent/long-term", status=500)
    responses.add(responses.GET, "https://api.rentcast.io/v1/avm/value", status=500)
    configured.pipeline.enrichment.housecanary = False
    listing = make_listing()
    Enricher(http, configured).enrich(listing)
    assert listing.enrichment.rent_estimate is None
    assert listing.enrichment.errors
