"""Live self-diagnosis: prove each configured provider actually works.

The test suite pins behaviour against recorded payloads. It cannot tell you
whether *your* key is valid, whether a wrapper has changed shape since the
fixtures were written, or whether a source that returns HTTP 200 is returning
anything usable. This does, with one small real request per provider.

The important output is not "did it respond" but the **field coverage table**:
a wrapper that returns 40 listings with no description silently defeats the
lease-term and amenity filters, and looks identical to a healthy one until you
notice you are getting no matches.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .config import Settings
from .enrich.miamidade import MiamiDadeClient
from .enrich.rentcast import RentCastClient
from .models import Listing
from .sources.base import ListingSource
from .sources.craigslist import CraigslistSource
from .sources.rapidapi import build_rapidapi_sources
from .sources.realtyapi import RealtyApiSource
from .sources.rentcast_listings import RentCastListingsSource
from .sources.scrapingbee import ScrapingBeeSource
from .util.http import HttpClient
from .util.logging import get_logger

log = get_logger(__name__)

#: Fields whose absence silently disables a downstream filter.
CRITICAL_FIELDS = ("price", "beds", "baths", "sqft", "description", "latitude", "url")

#: What breaks when each field is missing, in plain terms.
_CONSEQUENCE = {
    "price": "budget filter cannot run -- listing is dropped",
    "beds": "bedroom minimum degrades to a warning",
    "baths": "bathroom minimum degrades to a warning",
    "sqft": "1,200 sqft floor degrades to a warning (county lookup may recover it)",
    "description": "LEASE TERM and OCEAN VIEW cannot be judged -- both become advisory",
    "latitude": "ocean distance cannot be measured",
    "url": "alerts carry no link to the listing",
}


@dataclass
class ProbeResult:
    name: str
    ok: bool = False
    skipped: bool = False
    detail: str = ""
    count: int = 0
    seconds: float = 0.0
    #: Listing sources report a count; enrichment providers do not, and must
    #: not be judged as "returned nothing".
    is_source: bool = False
    coverage: dict[str, int] = field(default_factory=dict)
    samples: list[str] = field(default_factory=list)

    def status(self) -> str:
        if self.skipped:
            return "SKIP"
        return "OK" if self.ok else "FAIL"


def _coverage(listings: list[Listing]) -> dict[str, int]:
    """How many of the sampled listings actually carry each critical field."""
    counts: dict[str, int] = {}
    for name in CRITICAL_FIELDS:
        populated = 0
        for listing in listings:
            value = getattr(listing, name, None)
            if name == "description":
                populated += 1 if listing.has_descriptive_text else 0
            elif value not in (None, "", [], 0):
                populated += 1
        counts[name] = populated
    return counts


def probe_source(source: ListingSource, settings: Settings) -> ProbeResult:
    """Run one narrow real query against a source."""
    result = ProbeResult(name=source.name, is_source=True)
    if not source.enabled:
        result.skipped = True
        result.detail = "not configured"
        return result

    # Narrow the search to a single ZIP so a probe costs one request, not five.
    criteria = settings.search
    original_zips = list(criteria.zip_codes)
    criteria.zip_codes = original_zips[:1] or ["33154"]

    started = time.monotonic()
    try:
        outcome = source.fetch(criteria)
    finally:
        criteria.zip_codes = original_zips
    result.seconds = time.monotonic() - started

    result.count = outcome.count
    if outcome.errors:
        result.ok = False
        result.detail = outcome.errors[0][:160]
        return result

    result.ok = True
    if outcome.count == 0:
        result.detail = (
            "responded cleanly but returned 0 listings -- either the ZIP genuinely "
            "has no matching inventory, or the response shape has changed"
        )
        return result

    sample = outcome.listings[:10]
    result.coverage = _coverage(sample)
    result.samples = [item.summary() for item in sample[:3]]
    result.detail = f"{outcome.count} listings from {criteria.zip_codes[0] if criteria.zip_codes else '?'}"
    return result


def probe_rentcast_avm(settings: Settings, http: HttpClient) -> ProbeResult:
    result = ProbeResult(name="rentcast_avm")
    if not settings.rentcast.enabled:
        result.skipped = True
        result.detail = "RENTCAST_API_KEY unset"
        return result

    probe = Listing(
        source="doctor",
        address="9705 Collins Ave #1502N, Bal Harbour, FL 33154",
        beds=2, baths=2, sqft=1450, property_type="Condo",
    )
    started = time.monotonic()
    try:
        estimate = RentCastClient(http, settings.rentcast).rent_estimate(probe)
    except Exception as exc:
        result.detail = f"{type(exc).__name__}: {exc}"[:160]
        return result
    result.seconds = time.monotonic() - started
    result.ok = True
    result.detail = (
        f"rent estimate ${estimate.rent:,}" if estimate and estimate.rent
        else "responded, but no estimate for the probe address (not necessarily a fault)"
    )
    return result


def probe_county(settings: Settings, http: HttpClient) -> ProbeResult:
    result = ProbeResult(name="miamidade_county")
    if not settings.county.enabled:
        result.skipped = True
        result.detail = "MIAMIDADE_ENABLED=false"
        return result

    probe = Listing(
        source="doctor",
        address="9705 Collins Ave #1502N, Bal Harbour, FL 33154",
    )
    started = time.monotonic()

    # MiamiDadeClient.lookup() deliberately swallows transport errors so a
    # county outage never breaks a run. That makes it useless as a probe: a
    # 403 and a genuine no-match both return None. So reachability is checked
    # directly first, and only then is the parsed result inspected.
    try:
        http.request(
            "GET",
            settings.county.pa_proxy_url,
            params={
                "Operation": "GetPropertySearchByAddress",
                "clientAppName": "PropertySearch",
                "myAddress": "9705 Collins Ave",
                "myUnit": "",
                "from": "0",
                "to": "10",
            },
        )
    except Exception as exc:
        result.seconds = time.monotonic() - started
        result.detail = f"unreachable -- {type(exc).__name__}: {exc}"[:180]
        return result

    try:
        record = MiamiDadeClient(http, settings.county).lookup(probe)
    except Exception as exc:
        result.detail = f"{type(exc).__name__}: {exc}"[:160]
        return result
    result.seconds = time.monotonic() - started
    result.ok = True
    if record and record.has_specs:
        result.detail = (
            f"folio {record.folio}: {record.sqft or '?'} sqft, built {record.year_built or '?'}"
        )
    else:
        result.detail = (
            "reachable but returned no specs for the probe address -- county "
            "verification will be unavailable"
        )
    return result


def run_diagnostics(settings: Settings) -> list[ProbeResult]:
    """Probe every configured provider once."""
    http = HttpClient(
        timeout=settings.http.timeout,
        max_retries=1,
        rate_limit_seconds=settings.http.rate_limit_seconds,
    )
    try:
        sources: list[ListingSource] = [
            *build_rapidapi_sources(http, settings.rapidapi),
            RealtyApiSource(http, settings.realtyapi),
            RentCastListingsSource(
                http, settings.rentcast,
                enabled=settings.rentcast.use_as_listing_source,
            ),
            CraigslistSource(
                http, enabled=settings.craigslist.enabled, sites=settings.craigslist.sites
            ),
            ScrapingBeeSource(http, settings.scrapingbee),
        ]
        results = [probe_source(source, settings) for source in sources]
        results.append(probe_rentcast_avm(settings, http))
        results.append(probe_county(settings, http))
        return results
    finally:
        http.close()


def render(results: list[ProbeResult]) -> str:
    lines: list[str] = ["", "=" * 74, "PROVIDER DIAGNOSTICS", "=" * 74]

    for result in results:
        marker = {"OK": "ok  ", "FAIL": "FAIL", "SKIP": "skip"}[result.status()]
        timing = f"{result.seconds:5.1f}s" if result.seconds else "     "
        lines.append(f"  [{marker}] {result.name:22} {timing}  {result.detail}")
        for sample in result.samples:
            lines.append(f"           · {sample}")

    producing = [r for r in results if r.ok and r.count and r.coverage]
    if producing:
        lines += ["", "-" * 74, "FIELD COVERAGE (of the listings sampled)", "-" * 74]
        header = "  " + "source".ljust(22) + "".join(f"{f[:5]:>8}" for f in CRITICAL_FIELDS)
        lines.append(header)
        for result in producing:
            row = "  " + result.name.ljust(22)
            for name in CRITICAL_FIELDS:
                have = result.coverage.get(name, 0)
                total = min(result.count, 10)
                row += f"{f'{have}/{total}':>8}"
            lines.append(row)

        gaps = {}
        for result in producing:
            for name in CRITICAL_FIELDS:
                if result.coverage.get(name, 0) == 0:
                    gaps.setdefault(name, []).append(result.name)
        if gaps:
            lines += ["", "  Consequences of the gaps above:"]
            for name, sources in gaps.items():
                lines.append(f"    {name} missing from {', '.join(sources)}")
                lines.append(f"      -> {_CONSEQUENCE[name]}")

    failed = [r for r in results if r.status() == "FAIL"]
    empty = [r for r in results if r.is_source and r.ok and r.count == 0 and not r.skipped]
    lines += ["", "-" * 74]
    if failed:
        lines.append(f"  {len(failed)} provider(s) FAILED: {', '.join(r.name for r in failed)}")
    if empty:
        lines.append(f"  {len(empty)} responded but returned nothing: "
                     f"{', '.join(r.name for r in empty)}")
    if not failed and not empty:
        working = [r for r in results if r.ok]
        lines.append(f"  All {len(working)} configured provider(s) are working.")
    lines.append("=" * 74)
    return "\n".join(lines)
