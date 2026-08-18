"""Craigslist rentals via the published RSS feed -- no API key of any kind.

This is the only genuinely zero-credential source of *active rental listings*
in the project. It deliberately consumes the RSS feed (``?format=rss``), which
Craigslist publishes for automated readers, rather than scraping their search
HTML -- their terms prohibit the latter and they enforce it.

What that buys and what it costs:

* **Buys**: free, no key, no proxy, and genuine by-owner inventory that never
  reaches Zillow or the MLS wrappers.
* **Costs**: the feed carries no coordinates, so ocean distance falls back to a
  warning, and the item title is the only structured field -- Craigslist encodes
  price, bedrooms and area into it as ``$8500 / 2br - 1400ft2 - <headline>``.

The pipeline already treats missing data as a warning rather than a rejection,
so these listings surface flagged rather than being silently dropped.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable

from ..config import SearchCriteria
from ..models import Listing
from ..util.http import HttpClient, HttpError
from ..util.logging import get_logger
from ..util.text import clean_text, parse_beds, parse_price, parse_sqft
from .base import ListingSource, SourceResult

log = get_logger(__name__)

# Craigslist regional sites covering the monitored municipalities.
DEFAULT_SITES = ("miami",)

# "$8,500 / 2br - 1400ft2 - Oceanfront Bal Harbour condo"
_TITLE_PATTERN = re.compile(
    r"^\s*(?P<price>\$[\d,]+)?\s*/?\s*"
    r"(?:(?P<beds>\d+)\s*br)?\s*-?\s*"
    r"(?:(?P<sqft>[\d,]+)\s*ft2?)?\s*-?\s*"
    r"(?P<headline>.*)$",
    re.IGNORECASE,
)

# Neighbourhood is appended in parentheses: "... (bal harbour)"
_NEIGHBOURHOOD_PATTERN = re.compile(r"\(([^()]{2,60})\)\s*$")

_NAMESPACES = {
    "rss": "http://purl.org/rss/1.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "enc": "http://purl.oclc.org/net/rss_2.0/enc#",
}


class CraigslistSource(ListingSource):
    name = "craigslist"
    label = "Craigslist (RSS)"

    #: Craigslist caps a feed at 25 items; page with the `s` offset parameter.
    page_size = 25
    max_pages = 4

    def __init__(
        self,
        http: HttpClient,
        enabled: bool = True,
        sites: Iterable[str] = DEFAULT_SITES,
    ) -> None:
        super().__init__(http)
        self._enabled = enabled
        self.sites = list(sites)

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------ core
    def _fetch(self, criteria: SearchCriteria, result: SourceResult) -> list[Listing]:
        collected: dict[str, Listing] = {}

        for site in self.sites:
            for postal in criteria.zip_codes or [""]:
                for page in range(self.max_pages):
                    params = {
                        "format": "rss",
                        "min_price": criteria.min_price,
                        "max_price": criteria.max_price,
                        "min_bedrooms": int(criteria.min_beds),
                        "min_bathrooms": int(criteria.min_baths),
                        "minSqft": criteria.min_sqft,
                        "availabilityMode": 0,
                        "s": page * self.page_size,
                    }
                    if postal:
                        params["postal"] = postal
                        params["search_distance"] = 2      # miles around the ZIP

                    url = f"https://{site}.craigslist.org/search/apa"
                    try:
                        response = self.http.request("GET", url, params=params)
                    except HttpError as exc:
                        result.errors.append(f"{site}/{postal or 'all'}: {exc}")
                        break

                    items = self._parse_feed(response.text)
                    result.requests_made += 1
                    if not items:
                        break

                    for item in items:
                        listing = self._to_listing(item, postal, criteria)
                        if listing is None:
                            continue
                        key = listing.dedupe_key
                        if key in collected:
                            collected[key].merge_from(listing)
                        else:
                            collected[key] = listing

                    if len(items) < self.page_size:
                        break

        listings = list(collected.values())
        log.info("%s: %d unique postings", self.name, len(listings))
        return listings

    # --------------------------------------------------------------- parsing
    @staticmethod
    def _parse_feed(body: str) -> list[dict[str, str]]:
        """Extract items from either RSS 1.0/RDF or RSS 2.0.

        Craigslist has served both over the years and switches without notice,
        so both element layouts are handled.
        """
        if not body or not body.lstrip().startswith("<"):
            return []
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            log.warning("craigslist: unparseable feed (%s)", exc)
            return []

        nodes = root.findall(".//rss:item", _NAMESPACES) or root.findall(".//item")
        items: list[dict[str, str]] = []
        for node in nodes:
            item = {
                "title": _text(node, ("rss:title", "title")),
                "link": _text(node, ("rss:link", "link")),
                "description": _text(node, ("rss:description", "description")),
                "date": _text(node, ("dc:date", "pubDate")),
            }
            if item["title"] or item["link"]:
                items.append(item)
        return items

    def _to_listing(
        self, item: dict[str, str], postal: str, criteria: SearchCriteria
    ) -> Listing | None:
        title = clean_text(item.get("title"))
        if not title:
            return None

        match = _TITLE_PATTERN.match(title)
        groups = match.groupdict() if match else {}
        headline = clean_text(groups.get("headline") or title)

        neighbourhood = ""
        area_match = _NEIGHBOURHOOD_PATTERN.search(headline)
        if area_match:
            neighbourhood = clean_text(area_match.group(1))
            headline = clean_text(headline[: area_match.start()])

        description = clean_text(item.get("description"))
        link = clean_text(item.get("link"))

        # Craigslist gives no street address. The neighbourhood is the only
        # locality signal, so the ZIP that produced the hit stands in for it and
        # the posting id carries the identity.
        posting_id = _posting_id(link)

        listing = self.make_listing(
            source_id=posting_id,
            url=link,
            address="",
            city=neighbourhood,
            state=criteria.state,
            zip_code=postal,
            property_type="apartment",
            beds=parse_beds(groups.get("beds")),
            sqft=parse_sqft((groups.get("sqft") or "").replace(",", "")),
            price=parse_price(groups.get("price")),
            title=headline,
            description=description,
            available_date=clean_text(item.get("date")) or None,
            raw={"feed_title": title, "neighbourhood": neighbourhood},
        )
        return listing


def _text(node: ET.Element, paths: tuple[str, ...]) -> str:
    for path in paths:
        found = node.find(path, _NAMESPACES) if ":" in path else node.find(path)
        if found is not None and found.text:
            return found.text.strip()
    return ""


def _posting_id(link: str) -> str:
    """Craigslist posting ids are the numeric stem of the URL."""
    match = re.search(r"/(\d{6,})\.html", link or "")
    return match.group(1) if match else (link or "")
