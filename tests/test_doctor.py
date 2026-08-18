"""The live-diagnostics command.

Its whole purpose is to be trustworthy about failure, so these tests focus on
the ways a naive probe would lie: calling a blocked provider "reachable",
counting an enrichment probe as an empty source, or reporting a healthy-looking
source that returns nothing usable.
"""

from __future__ import annotations

import responses

from miami_bot.doctor import ProbeResult, probe_county, probe_source, render, run_diagnostics
from miami_bot.sources.rentcast_listings import RentCastListingsSource
from tests.fixtures import payloads
from tests.test_freesources import RENTCAST_LISTINGS


@responses.activate
def test_a_working_source_reports_coverage(http, settings):
    responses.add(responses.GET, "https://api.rentcast.io/v1/listings/rental/long-term",
                  json=RENTCAST_LISTINGS, status=200)
    settings.rentcast.api_key = "k"
    source = RentCastListingsSource(http, settings.rentcast)
    result = probe_source(source, settings)

    assert result.ok and result.count == 1
    assert result.coverage["price"] == 1
    # RentCast publishes no description and no URL -- the probe must say so.
    assert result.coverage["description"] == 0
    assert result.coverage["url"] == 0


@responses.activate
def test_a_probe_costs_one_request_not_one_per_zip(http, settings):
    responses.add(responses.GET, "https://api.rentcast.io/v1/listings/rental/long-term",
                  json=RENTCAST_LISTINGS, status=200)
    settings.rentcast.api_key = "k"
    probe_source(RentCastListingsSource(http, settings.rentcast), settings)
    assert len(responses.calls) == 1


@responses.activate
def test_the_probe_restores_the_configured_zips(http, settings):
    responses.add(responses.GET, "https://api.rentcast.io/v1/listings/rental/long-term",
                  json=[], status=200)
    settings.rentcast.api_key = "k"
    original = list(settings.search.zip_codes)
    probe_source(RentCastListingsSource(http, settings.rentcast), settings)
    assert settings.search.zip_codes == original


@responses.activate
def test_a_failing_source_is_reported_as_failed(http, settings):
    responses.add(responses.GET, "https://api.rentcast.io/v1/listings/rental/long-term",
                  json={"error": "bad key"}, status=403)
    settings.rentcast.api_key = "bad"
    result = probe_source(RentCastListingsSource(http, settings.rentcast), settings)
    assert not result.ok and "auth" in result.detail.lower()


@responses.activate
def test_a_source_returning_nothing_is_called_out(http, settings):
    responses.add(responses.GET, "https://api.rentcast.io/v1/listings/rental/long-term",
                  json=[], status=200)
    settings.rentcast.api_key = "k"
    result = probe_source(RentCastListingsSource(http, settings.rentcast), settings)
    assert result.ok and result.count == 0
    assert "0 listings" in result.detail


def test_an_unconfigured_source_is_skipped_not_failed(http, settings):
    settings.rentcast.api_key = ""
    result = probe_source(RentCastListingsSource(http, settings.rentcast), settings)
    assert result.skipped and result.status() == "SKIP"


@responses.activate
def test_a_blocked_county_is_reported_unreachable_not_reachable(http, settings):
    """MiamiDadeClient swallows transport errors by design, so a naive probe
    would report a 403-blocked service as merely 'no data'."""
    responses.add(responses.GET, settings.county.pa_proxy_url, status=403)
    settings.county.enabled = True
    result = probe_county(settings, http)
    assert not result.ok
    assert "unreachable" in result.detail


@responses.activate
def test_a_reachable_county_reports_its_specs(http, settings):
    responses.add(responses.GET, settings.county.pa_proxy_url,
                  json=payloads.MIAMIDADE_ADDRESS_SEARCH, status=200)
    responses.add(responses.GET, settings.county.pa_proxy_url,
                  json=payloads.MIAMIDADE_ADDRESS_SEARCH, status=200)
    responses.add(responses.GET, settings.county.pa_proxy_url,
                  json=payloads.MIAMIDADE_FOLIO_DETAIL, status=200)
    settings.county.enabled = True
    result = probe_county(settings, http)
    assert result.ok and "1180 sqft" in result.detail.replace(",", "")


def test_enrichment_probes_are_not_counted_as_empty_sources():
    """An AVM probe has no listing count; treating it as an empty source would
    make a healthy run exit non-zero."""
    results = [
        ProbeResult(name="rentcast_avm", ok=True, count=0, is_source=False,
                    detail="rent estimate $8,900"),
        ProbeResult(name="rapidapi_zillow", ok=True, count=3, is_source=True,
                    coverage={"price": 3}),
    ]
    output = render(results)
    assert "returned nothing" not in output
    assert "are working" in output


def test_the_report_explains_what_each_gap_breaks():
    results = [ProbeResult(
        name="rentcast_listings", ok=True, count=2, is_source=True,
        coverage={"price": 2, "beds": 2, "baths": 2, "sqft": 2,
                  "description": 0, "latitude": 2, "url": 0},
    )]
    output = render(results)
    assert "LEASE TERM and OCEAN VIEW" in output
    assert "alerts carry no link" in output


def test_diagnostics_run_with_nothing_configured(settings):
    settings.county.enabled = False
    results = run_diagnostics(settings)
    assert results and all(r.skipped for r in results)
    assert "skip" in render(results)
