"""Module 2: ScrapingBee fallback scraper.

This is what runs when the Module 1 wrappers go down, go stale, or return
suspiciously little. It fetches the consumer portals directly through
ScrapingBee's headless browser with residential/stealth proxies, which is what
gets past the bot walls those portals put in front of automated clients.

Extraction strategy, in order of preference:

1. **Embedded state JSON.** Zillow, Redfin and Realtor are all JS applications
   that ship their search results inside the HTML as ``__NEXT_DATA__``,
   ``__INITIAL_STATE__`` or similar. That JSON is the same shape the portal's
   own API returns, so we can reuse the Module 1 field maps verbatim.
2. **JSON-LD.** ``application/ld+json`` blocks carry schema.org
   ``Residence``/``Offer`` records on detail pages.
3. **Server-side CSS extraction.** ScrapingBee's ``extract_rules`` runs the
   selectors on their infrastructure and hands back JSON -- no local HTML
   parsing, and it survives markup churn better than a local scraper.

Every request is counted against ``SCRAPINGBEE_MAX_REQUESTS_PER_RUN`` because
premium/stealth proxy calls cost real credits.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ..config import ScrapingBeeSettings, SearchCriteria
from ..models import Listing
from ..util.http import HttpClient, HttpError
from ..util.logging import get_logger
from ..util.text import (
    clean_text,
    parse_baths,
    parse_beds,
    parse_price,
    parse_sqft,
    parse_year_built,
)
from .base import ListingSource, SourceResult
from .extract import collect_strings, deep_get_text, find_result_array, first
from .rapidapi import (
    FieldMap,
    RealtorRapidSource,
    RedfinRapidSource,
    ZillowRapidSource,
    _as_float,
    _as_int,
    _passes_cheap_prefilter,
)

log = get_logger(__name__)

SCRAPINGBEE_ENDPOINT = "https://app.scrapingbee.com/api/v1/"

try:  # the official SDK is preferred; the raw endpoint is an exact fallback
    from scrapingbee import ScrapingBeeClient  # type: ignore
    _SDK_AVAILABLE = True
except ImportError:  # pragma: no cover
    ScrapingBeeClient = None  # type: ignore[assignment]
    _SDK_AVAILABLE = False


# Script blocks that carry a portal's server-rendered search state.
_STATE_PATTERNS = (
    re.compile(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL | re.IGNORECASE),
    re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>", re.DOTALL),
    re.compile(r"window\.__reactServerState\s*=\s*(\{.*?\})\s*;?\s*</script>", re.DOTALL),
    re.compile(r"root\.__STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>", re.DOTALL),
    re.compile(r'<!--\s*({"queryState".*?})\s*-->', re.DOTALL),
)

_JSON_LD_PATTERN = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


@dataclass
class PortalTarget:
    """One consumer-portal search page plus the field map for its payload."""

    key: str
    label: str
    url_template: str
    field_map: FieldMap
    #: CSS extraction rules used when no embedded JSON is found.
    extract_rules: dict[str, Any]

    def url_for(self, zip_code: str, criteria: SearchCriteria) -> str:
        return self.url_template.format(
            zip=zip_code,
            min_price=criteria.min_price,
            max_price=criteria.max_price,
            min_beds=int(criteria.min_beds),
            min_baths=int(criteria.min_baths),
            min_sqft=criteria.min_sqft,
        )


PORTAL_TARGETS: tuple[PortalTarget, ...] = (
    PortalTarget(
        key="zillow",
        label="Zillow",
        url_template="https://www.zillow.com/homes/for_rent/{zip}_rb/",
        field_map=ZillowRapidSource.field_map,
        extract_rules={
            "listings": {
                "selector": "article[data-test='property-card'], li article",
                "type": "list",
                "output": {
                    "address": "address, [data-test='property-card-addr']",
                    "price": "[data-test='property-card-price'], span[data-test='property-card-price']",
                    "details": {"selector": "ul li", "type": "list"},
                    "url": {"selector": "a", "output": "@href"},
                    "photo": {"selector": "img", "output": "@src"},
                },
            }
        },
    ),
    PortalTarget(
        key="redfin",
        label="Redfin",
        url_template="https://www.redfin.com/zipcode/{zip}/apartments-for-rent",
        field_map=RedfinRapidSource.field_map,
        extract_rules={
            "listings": {
                "selector": "div.HomeCardContainer, div.bp-Homecard",
                "type": "list",
                "output": {
                    "address": ".homeAddressV2, .bp-Homecard__Address",
                    "price": ".homecardV2Price, .bp-Homecard__Price--value",
                    "details": {"selector": ".stats, .bp-Homecard__Stats > div", "type": "list"},
                    "url": {"selector": "a", "output": "@href"},
                    "photo": {"selector": "img", "output": "@src"},
                },
            }
        },
    ),
    PortalTarget(
        key="realtor",
        label="Realtor.com",
        url_template="https://www.realtor.com/apartments/{zip}",
        field_map=RealtorRapidSource.field_map,
        extract_rules={
            "listings": {
                "selector": "div[data-testid='card-content'], li[data-testid='result-card']",
                "type": "list",
                "output": {
                    "address": "[data-testid='card-address'], .card-address",
                    "price": "[data-testid='card-price'], .card-price",
                    "details": {"selector": "[data-testid='card-meta'] li, .property-meta li",
                                "type": "list"},
                    "url": {"selector": "a", "output": "@href"},
                    "photo": {"selector": "img", "output": "@src"},
                },
            }
        },
    ),
)

_PORTAL_BASE_URLS = {
    "zillow": "https://www.zillow.com",
    "redfin": "https://www.redfin.com",
    "realtor": "https://www.realtor.com",
}


class ScrapingBeeSource(ListingSource):
    """Anti-bot scraper standing in for the primary APIs."""

    name = "scrapingbee"
    label = "ScrapingBee (portal scrape)"
    is_fallback = True

    def __init__(
        self,
        http: HttpClient,
        settings: ScrapingBeeSettings,
        targets: Iterable[PortalTarget] = PORTAL_TARGETS,
    ) -> None:
        super().__init__(http)
        self.settings = settings
        self.targets = list(targets)
        self._requests_used = 0
        self._client = (
            ScrapingBeeClient(api_key=settings.api_key)
            if _SDK_AVAILABLE and settings.api_key else None
        )

    @property
    def enabled(self) -> bool:
        return bool(self.settings.api_key)

    @property
    def budget_remaining(self) -> int:
        return max(0, self.settings.max_requests_per_run - self._requests_used)

    # ------------------------------------------------------------- transport
    def _request_params(self, extract_rules: dict[str, Any] | None = None) -> dict[str, Any]:
        """Build ScrapingBee parameters.

        ``stealth_proxy`` supersedes ``premium_proxy`` -- sending both is
        rejected by the API -- and it implies JS rendering.
        """
        params: dict[str, Any] = {
            "render_js": self.settings.render_js,
            "country_code": self.settings.country_code,
            "wait": self.settings.wait_ms,
            "block_resources": False,       # portals hide prices behind CSS/JS
            "return_page_source": True,     # post-hydration DOM, not the raw shell
        }
        if self.settings.stealth_proxy:
            params["stealth_proxy"] = True
            params["render_js"] = True
        elif self.settings.premium_proxy:
            params["premium_proxy"] = True

        if extract_rules:
            params["extract_rules"] = json.dumps(extract_rules)
        return params

    def _scrape(self, url: str, extract_rules: dict[str, Any] | None = None) -> str:
        """Fetch one page, returning the response body (HTML or extracted JSON)."""
        if self.budget_remaining <= 0:
            raise HttpError(
                f"ScrapingBee request budget exhausted "
                f"({self.settings.max_requests_per_run} per run)"
            )
        params = self._request_params(extract_rules)
        self._requests_used += 1

        if self._client is not None:
            response = self._client.get(url, params=params)
            if response.status_code >= 400:
                raise HttpError(
                    f"ScrapingBee returned {response.status_code} for {url}",
                    status=response.status_code,
                    body=(response.text or "")[:400],
                )
            return response.text

        # Raw-endpoint fallback: identical semantics, booleans as strings.
        query = {"api_key": self.settings.api_key, "url": url}
        for key, value in params.items():
            query[key] = str(value).lower() if isinstance(value, bool) else value
        response = self.http.request(
            "GET", SCRAPINGBEE_ENDPOINT, params=query, timeout=180, allow_cache=False
        )
        return response.text

    # ------------------------------------------------------------------ core
    def _fetch(self, criteria: SearchCriteria, result: SourceResult) -> list[Listing]:
        self._requests_used = 0
        collected: dict[str, Listing] = {}

        for target in self.targets:
            for zip_code in criteria.zip_codes:
                if self.budget_remaining <= 0:
                    result.notes.append(
                        f"stopped early: request budget of "
                        f"{self.settings.max_requests_per_run} exhausted"
                    )
                    log.warning("%s: request budget exhausted", self.name)
                    return list(collected.values())

                url = target.url_for(zip_code, criteria)
                try:
                    body = self._scrape(url)
                    result.requests_made += 1
                except HttpError as exc:
                    result.errors.append(f"{target.key}/{zip_code}: {exc}")
                    log.warning("%s: %s %s failed: %s", self.name, target.key, zip_code, exc)
                    continue

                items = self._extract_records(body)
                if not items and self.budget_remaining > 0:
                    # Nothing embedded -- pay for one server-side CSS extraction.
                    try:
                        extracted = self._scrape(url, extract_rules=target.extract_rules)
                        result.requests_made += 1
                        items = self._records_from_css(extracted, target)
                    except HttpError as exc:
                        result.errors.append(f"{target.key}/{zip_code} css: {exc}")
                        items = []

                log.info(
                    "%s: %s %s -> %d records (%d requests used)",
                    self.name, target.key, zip_code, len(items), self._requests_used,
                )
                for item in items:
                    listing = self._to_listing(item, target)
                    if listing is None:
                        continue
                    key = listing.dedupe_key
                    if key in collected:
                        collected[key].merge_from(listing)
                    else:
                        collected[key] = listing

        listings = list(collected.values())
        candidates = [item for item in listings if _passes_cheap_prefilter(item, criteria)]
        log.info("%s: %d parsed, %d after pre-filter", self.name, len(listings), len(candidates))
        return candidates

    # ------------------------------------------------------------ extraction
    @staticmethod
    def _extract_records(body: str) -> list[dict[str, Any]]:
        """Pull listing records out of embedded state JSON or JSON-LD."""
        if not body:
            return []

        # The response may already be JSON (extract_rules or a JSON endpoint).
        stripped = body.lstrip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return find_result_array(json.loads(body))
            except json.JSONDecodeError:
                pass

        records: list[dict[str, Any]] = []
        for pattern in _STATE_PATTERNS:
            for match in pattern.finditer(body):
                blob = _balanced_json(match.group(1))
                if not blob:
                    continue
                try:
                    payload = json.loads(blob)
                except json.JSONDecodeError:
                    continue
                found = find_result_array(payload)
                if len(found) > len(records):
                    records = found
            if records:
                break

        if records:
            return records

        for match in _JSON_LD_PATTERN.finditer(body):
            try:
                payload = json.loads(match.group(1).strip())
            except json.JSONDecodeError:
                continue
            candidates = payload if isinstance(payload, list) else [payload]
            for candidate in candidates:
                if isinstance(candidate, dict):
                    records.extend(find_result_array(candidate) or [candidate])
        return records

    @staticmethod
    def _records_from_css(body: str, target: PortalTarget) -> list[dict[str, Any]]:
        """Normalize ScrapingBee's ``extract_rules`` output into flat records.

        The card markup gives an address, a price and a bag of stat strings
        ("2 bds", "2 ba", "1,450 sqft") that have to be pulled apart.
        """
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return []

        cards = payload.get("listings") if isinstance(payload, dict) else payload
        if not isinstance(cards, list):
            return []

        base = _PORTAL_BASE_URLS.get(target.key, "")
        records: list[dict[str, Any]] = []
        for card in cards:
            if not isinstance(card, dict):
                continue
            details = card.get("details") or []
            if isinstance(details, str):
                details = [details]
            blob = " ".join(str(d) for d in details)

            url = clean_text(card.get("url"))
            if url and not url.startswith("http"):
                url = f"{base}{url if url.startswith('/') else '/' + url}"

            records.append({
                "address": clean_text(card.get("address")),
                "price": parse_price(card.get("price")),
                "beds": _stat(blob, r"(\d+(?:\.\d+)?)\s*(?:bd|bds|bed|beds|br)\b"),
                "baths": _stat(blob, r"(\d+(?:\.\d+)?)\s*(?:ba|bath|baths|br)\b"),
                "sqft": _stat(blob, r"([\d,]+)\s*(?:sqft|sq\s?ft|square feet)\b"),
                "url": url,
                "photos": [clean_text(card.get("photo"))] if card.get("photo") else [],
                "description": blob,
                "propertyType": "condo",
                "_scraped_via": "css_extract_rules",
            })
        return records

    def _to_listing(self, item: dict[str, Any], target: PortalTarget) -> Listing | None:
        """Map a scraped record using the portal's Module 1 field map.

        Scraped state JSON and API JSON come from the same upstream, so the same
        candidate paths apply. Flat CSS records fall through to their own keys.
        """
        fields = target.field_map
        address = first(item, (*fields.address, "address"), clean_text)
        latitude = _as_float(first(item, fields.latitude))
        longitude = _as_float(first(item, fields.longitude))
        if not address and latitude is None:
            return None

        url = first(item, (*fields.url, "url"), clean_text, default="") or ""
        if url and not url.startswith("http"):
            base = _PORTAL_BASE_URLS.get(target.key, "")
            url = f"{base}{url if url.startswith('/') else '/' + url}"

        listing = Listing(
            source=f"{self.name}_{target.key}",
            source_id=str(first(item, (*fields.source_id, "id"), default="") or ""),
            url=url,
            address=address or "",
            unit=first(item, fields.unit, clean_text, default="") or "",
            city=first(item, fields.city, clean_text, default="") or "",
            state=first(item, fields.state, clean_text, default="FL") or "FL",
            zip_code=first(item, fields.zip_code, clean_text, default="") or "",
            latitude=latitude,
            longitude=longitude,
            property_type=first(item, (*fields.property_type, "propertyType"),
                                clean_text, default="") or "",
            beds=first(item, (*fields.beds, "beds"), parse_beds),
            baths=first(item, (*fields.baths, "baths"), parse_baths),
            sqft=first(item, (*fields.sqft, "sqft"), parse_sqft),
            year_built=first(item, fields.year_built, parse_year_built),
            price=first(item, (*fields.price, "price"), parse_price),
            status=first(item, fields.status, clean_text, default="") or "",
            available_date=first(item, fields.available_date, clean_text),
            days_on_market=_as_int(first(item, fields.days_on_market)),
            broker=first(item, fields.broker, clean_text, default="") or "",
            title=first(item, fields.title, clean_text, default="") or "",
            description=deep_get_text(item, (*fields.description_keys, "description")),
            photos=collect_strings(item, (*fields.photos, "photos")),
            amenities=collect_strings(item, fields.amenities, limit=40),
            raw={"portal": target.key, "scraped": True, **item},
        )
        return listing


def _stat(blob: str, pattern: str) -> float | None:
    match = re.search(pattern, blob, flags=re.IGNORECASE)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _balanced_json(text: str) -> str:
    """Trim a captured script body back to its first balanced JSON object.

    Regex alone cannot match nested braces, so a greedy capture often runs past
    the end of the object into the rest of the script.
    """
    text = text.strip()
    start = text.find("{")
    if start == -1:
        return ""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return ""
