"""Source adapters: field mapping, pagination, quota discipline and the
tolerance that keeps them working when a wrapper changes shape."""

from __future__ import annotations

import json

import pytest
import responses

from miami_bot.config import RapidApiSettings, RealtyApiSettings, ScrapingBeeSettings
from miami_bot.sources.extract import deep_get_text, dig, find_result_array, first
from miami_bot.sources.rapidapi import (
    RealtorRapidSource,
    ZillowRapidSource,
    build_rapidapi_sources,
)
from miami_bot.sources.realtyapi import RealtyApiSource
from miami_bot.sources.scrapingbee import PORTAL_TARGETS, ScrapingBeeSource
from tests.fixtures import payloads


@pytest.fixture
def rapid_settings() -> RapidApiSettings:
    return RapidApiSettings(api_key="test-key", enabled_sources=["zillow", "realtor"])


# ---------------------------------------------------------------- extraction
def test_dig_walks_paths_and_indexes():
    payload = {"a": {"b": [{"c": 1}, {"c": 2}]}}
    assert dig(payload, "a.b[1].c") == 2
    assert dig(payload, "a.b[9].c") is None
    assert dig(payload, "a.missing.c") is None


def test_first_falls_through_to_the_next_candidate_path():
    assert first({"beds": 2}, ["description.beds", "beds"]) == 2


def test_find_result_array_survives_an_unknown_envelope():
    original = payloads.REALTOR_SEARCH["data"]["home_search"]["results"]
    mutated = {"status": "ok", "payload": {"v9": {"listingResults": original}}}
    assert len(find_result_array(mutated)) == len(original)


def test_deep_get_text_gathers_scattered_description_fields():
    text = deep_get_text(payloads.ZILLOW_DETAIL, ["description", "leaseTerm"])
    assert "Annual lease" in text and "12 Months" in text


# -------------------------------------------------------------------- Zillow
@responses.activate
def test_zillow_adapter_maps_and_prefilters(http, rapid_settings, criteria):
    responses.add(
        responses.GET,
        "https://zillow-com1.p.rapidapi.com/propertyExtendedSearch",
        json=payloads.ZILLOW_SEARCH,
        status=200,
    )
    responses.add(
        responses.GET,
        "https://zillow-com1.p.rapidapi.com/property",
        json=payloads.ZILLOW_DETAIL,
        status=200,
    )

    source = ZillowRapidSource(http, rapid_settings)
    result = source.fetch(criteria)

    assert result.ok
    addresses = {item.display_address for item in result.listings}
    # The 1bd/780sqft unit is dropped before any detail call is spent on it.
    assert not any("904" in a for a in addresses)

    oceanfront = next(
        item for item in result.listings if "1502N" in item.display_address
    )
    assert oceanfront.price == 9500
    assert oceanfront.beds == 2 and oceanfront.baths == 2.5 and oceanfront.sqft == 1450
    assert oceanfront.url.startswith("https://www.zillow.com/homedetails/")
    # Detail-phase fields
    assert oceanfront.year_built == 2018
    assert "Annual lease" in oceanfront.description
    assert oceanfront.lease_term.min_months == 6
    assert oceanfront.lease_term.max_months == 12
    assert any("Valet" in a for a in oceanfront.amenities)


@responses.activate
def test_zillow_sends_rapidapi_auth_headers(http, rapid_settings, criteria):
    responses.add(
        responses.GET,
        "https://zillow-com1.p.rapidapi.com/propertyExtendedSearch",
        json={"props": []},
        status=200,
    )
    ZillowRapidSource(http, rapid_settings).fetch(criteria)
    request = responses.calls[0].request
    assert request.headers["X-RapidAPI-Key"] == "test-key"
    assert request.headers["X-RapidAPI-Host"] == "zillow-com1.p.rapidapi.com"


@responses.activate
def test_an_auth_failure_is_reported_not_raised(http, rapid_settings, criteria):
    responses.add(
        responses.GET,
        "https://zillow-com1.p.rapidapi.com/propertyExtendedSearch",
        json={"message": "invalid key"},
        status=403,
    )
    result = ZillowRapidSource(http, rapid_settings).fetch(criteria)
    assert not result.ok and result.listings == []
    assert "auth rejected" in result.errors[0]


@responses.activate
def test_a_rate_limit_is_reported_not_raised(http, rapid_settings, criteria):
    responses.add(
        responses.GET,
        "https://zillow-com1.p.rapidapi.com/propertyExtendedSearch",
        json={"message": "too many requests"},
        status=429,
    )
    result = ZillowRapidSource(http, rapid_settings).fetch(criteria)
    assert not result.ok and "rate limited" in result.errors[0]


@responses.activate
def test_detail_lookups_are_capped(http, rapid_settings, criteria):
    many = {"props": [
        dict(payloads.ZILLOW_SEARCH["props"][0], zpid=str(i)) for i in range(30)
    ], "totalPages": 1}
    responses.add(
        responses.GET,
        "https://zillow-com1.p.rapidapi.com/propertyExtendedSearch",
        json=many, status=200,
    )
    responses.add(
        responses.GET, "https://zillow-com1.p.rapidapi.com/property",
        json=payloads.ZILLOW_DETAIL, status=200,
    )
    source = ZillowRapidSource(http, rapid_settings)
    source.max_detail_lookups = 2
    result = source.fetch(criteria)
    detail_calls = [c for c in responses.calls if "/property?" in c.request.url]
    # All 30 records are the same unit, so they collapse to one before details.
    assert len(detail_calls) <= 2
    assert result.count == 1


# ------------------------------------------------------------------ Realtor
@responses.activate
def test_realtor_adapter_maps_the_nested_payload(http, rapid_settings, criteria):
    responses.add(
        responses.POST,
        "https://realty-in-us.p.rapidapi.com/properties/v3/list",
        json=payloads.REALTOR_SEARCH, status=200,
    )
    responses.add(
        responses.GET,
        "https://realty-in-us.p.rapidapi.com/properties/v3/detail",
        json={"data": {"home": {}}}, status=200,
    )
    result = RealtorRapidSource(http, rapid_settings).fetch(criteria)
    assert result.ok and result.count == 1
    listing = result.listings[0]
    assert listing.price == 9500
    assert listing.city == "Bal Harbour" and listing.zip_code == "33154"
    assert listing.unit == "1502n"
    assert listing.broker == "Douglas Elliman"
    assert "concierge" in " ".join(listing.amenities).lower()


@responses.activate
def test_realtor_posts_the_search_filters(http, rapid_settings, criteria):
    responses.add(
        responses.POST,
        "https://realty-in-us.p.rapidapi.com/properties/v3/list",
        json={"data": {"home_search": {"results": []}}}, status=200,
    )
    RealtorRapidSource(http, rapid_settings).fetch(criteria)
    body = json.loads(responses.calls[0].request.body)
    assert body["status"] == ["for_rent"]
    assert body["list_price"] == {"min": 5000, "max": 12000}
    assert body["sqft"]["min"] == 1200


def test_the_same_unit_from_two_wrappers_shares_one_dedupe_key(http, rapid_settings):
    zillow = ZillowRapidSource(http, rapid_settings).to_listing(
        payloads.ZILLOW_SEARCH["props"][0]
    )
    realtor = RealtorRapidSource(http, rapid_settings).to_listing(
        payloads.REALTOR_SEARCH["data"]["home_search"]["results"][0]
    )
    assert zillow.dedupe_key == realtor.dedupe_key


def test_the_registry_builds_only_the_enabled_wrappers(http):
    settings = RapidApiSettings(api_key="k", enabled_sources=["zillow", "nonsense"])
    built = build_rapidapi_sources(http, settings)
    assert [s.name for s in built] == ["rapidapi_zillow"]


def test_wrappers_are_disabled_without_a_key(http, criteria):
    source = ZillowRapidSource(http, RapidApiSettings(api_key=""))
    result = source.fetch(criteria)
    assert result.skipped and result.skip_reason == "not configured"


# ----------------------------------------------------------------- RealtyAPI
@responses.activate
def test_realtyapi_adapter(http, criteria):
    responses.add(
        responses.GET, "https://api.realtyapi.io/v1/properties/rentals",
        json=payloads.REALTYAPI_SEARCH, status=200,
    )
    settings = RealtyApiSettings(api_key="ra-key")
    result = RealtyApiSource(http, settings).fetch(criteria)
    assert result.ok and result.count == 1
    listing = result.listings[0]
    assert listing.price == 7800 and listing.sqft == 1380
    assert listing.unit == "n501" and listing.city == "Surfside"
    assert listing.lease_term.min_months == 6 and listing.lease_term.max_months == 12
    assert responses.calls[0].request.headers["X-Api-Key"] == "ra-key"


@responses.activate
def test_realtyapi_query_param_auth(http, criteria):
    responses.add(
        responses.GET, "https://api.realtyapi.io/v1/properties/rentals",
        json={"listings": []}, status=200,
    )
    settings = RealtyApiSettings(api_key="ra-key", auth_style="query_param",
                                 auth_param="apikey")
    RealtyApiSource(http, settings).fetch(criteria)
    assert "apikey=ra-key" in responses.calls[0].request.url


@pytest.mark.parametrize(
    "envelope",
    [
        {"listings": [{"id": "1", "address": "9705 Collins Ave", "price": 9000}]},
        {"data": {"results": [{"id": "1", "address": "9705 Collins Ave", "price": 9000}]}},
        [{"id": "1", "address": "9705 Collins Ave", "price": 9000}],
        {"odd": {"shape": {"x": [{"id": "1", "address": "9705 Collins Ave", "price": 9000}]}}},
    ],
)
def test_realtyapi_finds_results_in_any_envelope(envelope):
    assert len(RealtyApiSource._results_from(envelope)) == 1


# --------------------------------------------------------------- ScrapingBee
def test_scrapingbee_reads_embedded_portal_state(http):
    source = ScrapingBeeSource(http, ScrapingBeeSettings(api_key="sb"))
    records = source._extract_records(payloads.SCRAPED_ZILLOW_HTML)
    assert len(records) == 1
    target = next(t for t in PORTAL_TARGETS if t.key == "zillow")
    listing = source._to_listing(records[0], target)
    assert listing.price == 10500 and listing.sqft == 1600
    assert listing.city == "Sunny Isles Beach"
    assert listing.source == "scrapingbee_zillow"
    assert listing.url == "https://www.zillow.com/homedetails/991_zpid/"


def test_scrapingbee_parses_css_extraction_output(http):
    source = ScrapingBeeSource(http, ScrapingBeeSettings(api_key="sb"))
    target = next(t for t in PORTAL_TARGETS if t.key == "redfin")
    body = json.dumps({"listings": [{
        "address": "9111 Collins Ave #501, Surfside, FL 33154",
        "price": "$8,200/mo",
        "details": ["2 bds", "2 ba", "1,340 sqft"],
        "url": "/FL/Surfside/x/home/123",
        "photo": "https://redfin/x.jpg",
    }]})
    records = source._records_from_css(body, target)
    assert records[0]["beds"] == 2 and records[0]["sqft"] == 1340
    listing = source._to_listing(records[0], target)
    assert listing.price == 8200
    assert listing.url == "https://www.redfin.com/FL/Surfside/x/home/123"


def test_stealth_proxy_replaces_premium_proxy(http):
    settings = ScrapingBeeSettings(api_key="sb", premium_proxy=True, stealth_proxy=True)
    params = ScrapingBeeSource(http, settings)._request_params()
    assert params["stealth_proxy"] is True
    assert "premium_proxy" not in params
    assert params["render_js"] is True


def test_premium_proxy_is_used_when_stealth_is_off(http):
    settings = ScrapingBeeSettings(api_key="sb", premium_proxy=True, stealth_proxy=False)
    params = ScrapingBeeSource(http, settings)._request_params()
    assert params["premium_proxy"] is True and "stealth_proxy" not in params


@responses.activate
def test_scrapingbee_respects_its_request_budget(http, criteria):
    responses.add(
        responses.GET, "https://app.scrapingbee.com/api/v1/",
        body="<html>no listings here</html>", status=200,
    )
    settings = ScrapingBeeSettings(api_key="sb", max_requests_per_run=3)
    source = ScrapingBeeSource(http, settings)
    source._client = None                      # force the raw-endpoint path
    result = source.fetch(criteria)
    assert source._requests_used <= 3
    assert any("budget" in note for note in result.notes)


def test_greedy_script_capture_is_trimmed_to_balanced_json(http):
    from miami_bot.sources.scrapingbee import _balanced_json
    assert _balanced_json('{"a":{"b":"}"}} ;var junk={"x":1}') == '{"a":{"b":"}"}}'
