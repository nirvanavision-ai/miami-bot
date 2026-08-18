"""End-to-end: ingest -> dedupe -> filter -> enrich -> persist -> alert.

These run the real Pipeline with mocked HTTP, so the wiring between modules --
including the staleness decision that trips the ScrapingBee fallback and the
post-enrichment re-filter -- is exercised, not stubbed.
"""

from __future__ import annotations

import json

import pytest
import responses

from miami_bot.pipeline import Pipeline
from miami_bot.sources.base import SourceResult
from tests.conftest import make_listing
from tests.fixtures import payloads

DISCORD = "https://discord.test/webhook"


@pytest.fixture
def wired(settings, tmp_path):
    """Settings with every provider configured to a mockable endpoint."""
    settings.database_path = str(tmp_path / "pipeline.db")
    settings.rapidapi.api_key = "rapid-key"
    settings.rapidapi.enabled_sources = ["zillow"]
    settings.scrapingbee.api_key = "sb-key"
    settings.rentcast.api_key = "rc-key"
    settings.alerts.discord_webhook_url = DISCORD
    settings.http.rate_limit_seconds = 0.0
    settings.county.enabled = False
    settings.pipeline.enrichment.rentcast = False
    settings.search.zip_codes = ["33154"]           # one ZIP keeps mocks simple
    return settings


def _mock_zillow(props, detail=None):
    responses.add(
        responses.GET, "https://zillow-com1.p.rapidapi.com/propertyExtendedSearch",
        json={"props": props, "totalPages": 1}, status=200,
    )
    responses.add(
        responses.GET, "https://zillow-com1.p.rapidapi.com/property",
        json=detail or payloads.ZILLOW_DETAIL, status=200,
    )


@responses.activate
def test_a_qualifying_listing_flows_all_the_way_to_an_alert(wired):
    _mock_zillow(payloads.ZILLOW_SEARCH["props"])
    responses.add(responses.POST, DISCORD, status=204)

    with Pipeline(wired) as pipeline:
        summary = pipeline.run()

    assert summary.matched == 1
    assert summary.new_listings == 1
    assert summary.alerts_sent == 1

    body = json.loads(next(c.request.body for c in responses.calls if DISCORD in c.request.url))
    embed = body["embeds"][0]
    assert "9705 Collins Ave" in embed["title"]
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields["Lease term"] == "6-12 months"
    assert fields["Price"] == "$9,500/mo"


@responses.activate
def test_the_second_run_is_quiet_when_nothing_changed(wired):
    _mock_zillow(payloads.ZILLOW_SEARCH["props"])
    responses.add(responses.POST, DISCORD, status=204)

    with Pipeline(wired) as pipeline:
        pipeline.run()
    discord_calls_after_first = len([c for c in responses.calls if DISCORD in c.request.url])

    with Pipeline(wired) as pipeline:
        second = pipeline.run()

    assert second.new_listings == 0
    assert second.alerts_sent == 0
    assert len([c for c in responses.calls if DISCORD in c.request.url]) == discord_calls_after_first


@responses.activate
def test_a_price_drop_on_a_later_run_alerts_again(wired):
    _mock_zillow(payloads.ZILLOW_SEARCH["props"])
    responses.add(responses.POST, DISCORD, status=204)
    with Pipeline(wired) as pipeline:
        pipeline.run()

    responses.reset()
    dropped = [dict(payloads.ZILLOW_SEARCH["props"][0], price="$8,600/mo")]
    _mock_zillow(dropped)
    responses.add(responses.POST, DISCORD, status=204)

    with Pipeline(wired) as pipeline:
        summary = pipeline.run()

    assert summary.price_drops == 1
    body = json.loads(next(c.request.body for c in responses.calls if DISCORD in c.request.url))
    assert body["embeds"][0]["title"].startswith("📉")
    assert "$9,500 → $8,600" in body["embeds"][0]["description"]


@responses.activate
def test_the_scraper_engages_when_the_primary_api_fails(wired):
    responses.add(
        responses.GET, "https://zillow-com1.p.rapidapi.com/propertyExtendedSearch",
        json={"message": "service unavailable"}, status=503,
    )
    responses.add(
        responses.GET, "https://app.scrapingbee.com/api/v1/",
        body=payloads.SCRAPED_ZILLOW_HTML, status=200,
    )
    responses.add(responses.POST, DISCORD, status=204)

    with Pipeline(wired) as pipeline:
        pipeline.fallback_source._client = None      # force the raw-endpoint path
        summary = pipeline.run()

    assert summary.scraper_used
    assert "failed" in summary.scraper_reason
    assert summary.deduped >= 1
    scraped = [r for r in summary.source_results if r.source == "scrapingbee"]
    assert scraped and scraped[0].count >= 1


@responses.activate
def test_the_scraper_engages_when_the_primary_api_returns_too_little(wired):
    _mock_zillow([payloads.ZILLOW_SEARCH["props"][0]])       # 1 result, floor is 5
    responses.add(
        responses.GET, "https://app.scrapingbee.com/api/v1/",
        body=payloads.SCRAPED_ZILLOW_HTML, status=200,
    )
    responses.add(responses.POST, DISCORD, status=204)

    with Pipeline(wired) as pipeline:
        pipeline.fallback_source._client = None
        summary = pipeline.run()

    assert summary.scraper_used and "only 1 candidates" in summary.scraper_reason


@responses.activate
def test_the_scraper_stays_idle_when_the_primaries_are_healthy(wired):
    healthy = [
        dict(payloads.ZILLOW_SEARCH["props"][0], zpid=str(i),
             address=f"9705 Collins Ave #{1500 + i}N, Bal Harbour, FL 33154")
        for i in range(8)
    ]
    _mock_zillow(healthy)
    responses.add(responses.POST, DISCORD, status=204)

    with Pipeline(wired) as pipeline:
        summary = pipeline.run()

    assert not summary.scraper_used
    assert not any("scrapingbee" in c.request.url for c in responses.calls)


@responses.activate
def test_the_same_unit_from_two_portals_alerts_once(wired):
    wired.rapidapi.enabled_sources = ["zillow", "realtor"]
    _mock_zillow([payloads.ZILLOW_SEARCH["props"][0]])
    responses.add(
        responses.POST, "https://realty-in-us.p.rapidapi.com/properties/v3/list",
        json=payloads.REALTOR_SEARCH, status=200,
    )
    responses.add(
        responses.GET, "https://realty-in-us.p.rapidapi.com/properties/v3/detail",
        json={"data": {"home": {}}}, status=200,
    )
    responses.add(responses.POST, DISCORD, status=204)

    with Pipeline(wired) as pipeline:
        summary = pipeline.run()

    assert summary.fetched == 2          # both portals returned the unit
    assert summary.deduped == 1          # collapsed to one
    assert summary.new_listings == 1

    body = json.loads(next(c.request.body for c in responses.calls if DISCORD in c.request.url))
    assert len(body["embeds"]) == 1


@responses.activate
def test_county_data_can_reverse_a_match_after_enrichment(wired):
    wired.county.enabled = True
    wired.pipeline.enrichment.county_assessor = True
    inflated = [dict(payloads.ZILLOW_SEARCH["props"][0], livingArea=1650)]
    _mock_zillow(inflated, detail={"zpid": "43567890", "yearBuilt": 2018,
                                   "description": "Annual lease. Valet, concierge, pool, spa."})
    responses.add(responses.GET, wired.county.pa_proxy_url,
                  json=payloads.MIAMIDADE_ADDRESS_SEARCH, status=200)
    responses.add(responses.GET, wired.county.pa_proxy_url,
                  json=payloads.MIAMIDADE_FOLIO_DETAIL, status=200)      # 1,180 sqft
    responses.add(responses.POST, DISCORD, status=204)

    with Pipeline(wired) as pipeline:
        summary = pipeline.run()

    assert summary.matched == 0
    assert summary.alerts_sent == 0
    assert "size" in dict(summary.filter_stats.top_reasons())


@responses.activate
def test_a_run_survives_a_total_outage(wired):
    responses.add(
        responses.GET, "https://zillow-com1.p.rapidapi.com/propertyExtendedSearch",
        json={}, status=500,
    )
    responses.add(responses.GET, "https://app.scrapingbee.com/api/v1/", status=500)

    with Pipeline(wired) as pipeline:
        pipeline.fallback_source._client = None
        summary = pipeline.run()

    assert summary.matched == 0
    assert summary.alerts_sent == 0
    assert all(not r.ok for r in summary.source_results if not r.skipped)
    assert "SUMMARY" in summary.render()


@responses.activate
def test_dry_run_persists_but_does_not_alert(wired):
    wired.dry_run = True
    _mock_zillow(payloads.ZILLOW_SEARCH["props"])
    responses.add(responses.POST, DISCORD, status=204)

    with Pipeline(wired) as pipeline:
        summary = pipeline.run()
        assert pipeline.db.stats()["listings_matched"] == 1

    assert summary.matched == 1
    assert not any(DISCORD in c.request.url for c in responses.calls)


def test_merging_across_sources_unions_photos_and_specs():
    zillow = make_listing(source="rapidapi_zillow", sqft=1450, year_built=None,
                          photos=["https://a.jpg"])
    redfin = make_listing(source="rapidapi_redfin",
                          address="9705 Collins Avenue APT 1502-N, Bal Harbour, FL 33154-2932",
                          sqft=None, year_built=2018, price=9200,
                          photos=["https://b.jpg"])
    merged = Pipeline._merge_across_sources([zillow, redfin])
    assert len(merged) == 1
    unit = merged[0]
    assert unit.sqft == 1450 and unit.year_built == 2018
    assert unit.price == 9200                 # the lower advertised rent wins
    assert unit.photos == ["https://a.jpg", "https://b.jpg"]


def test_staleness_decision_matrix(wired):
    with Pipeline(wired) as pipeline:
        healthy = [SourceResult(source="a", listings=[make_listing()] * 6)]
        assert pipeline._should_use_fallback(healthy, healthy[0].listings) == (False, "")

        failed = [SourceResult(source="a", errors=["boom"])]
        used, reason = pipeline._should_use_fallback(failed, [])
        assert used and "failed" in reason

        thin = [SourceResult(source="a", listings=[make_listing()])]
        used, reason = pipeline._should_use_fallback(thin, thin[0].listings)
        assert used and "only 1 candidates" in reason

        skipped = [SourceResult(source="a", skipped=True, skip_reason="not configured")]
        used, reason = pipeline._should_use_fallback(skipped, [])
        assert used and "no primary source" in reason

        wired.pipeline.always_run_scraper = True
        assert pipeline._should_use_fallback(healthy, healthy[0].listings)[0]
