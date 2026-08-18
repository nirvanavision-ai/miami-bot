"""Module 1: RapidAPI portal wrappers (Zillow, Realtor, Redfin).

These marketplace wrappers re-publish consumer-portal search results, which is
how the pipeline reaches active rental inventory without MLS/IDX broker
credentials.

Two design decisions dominate this file:

**Two-phase fetching.** The search endpoint is cheap and returns numeric specs;
the detail endpoint is expensive and is the only place the description and lease
term live. Fetching details for every search hit would burn a month of quota in
one run. So we search, discard anything that already fails a numeric constraint,
and only then pull details for the survivors -- capped by
``max_detail_lookups``.

**Declarative field maps.** Every provider field is a list of candidate paths
(see :mod:`miami_bot.sources.extract`). When a wrapper renames a field, the
adapter degrades to a warning on one field instead of returning nothing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ..config import RapidApiSettings, SearchCriteria
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

log = get_logger(__name__)


@dataclass
class FieldMap:
    """Candidate JSON paths for each field of the internal schema."""

    source_id: Sequence[str] = ()
    url: Sequence[str] = ()
    address: Sequence[str] = ()
    unit: Sequence[str] = ()
    city: Sequence[str] = ()
    state: Sequence[str] = ()
    zip_code: Sequence[str] = ()
    latitude: Sequence[str] = ()
    longitude: Sequence[str] = ()
    property_type: Sequence[str] = ()
    beds: Sequence[str] = ()
    baths: Sequence[str] = ()
    sqft: Sequence[str] = ()
    year_built: Sequence[str] = ()
    price: Sequence[str] = ()
    status: Sequence[str] = ()
    available_date: Sequence[str] = ()
    days_on_market: Sequence[str] = ()
    broker: Sequence[str] = ()
    title: Sequence[str] = ()
    photos: Sequence[str] = ()
    amenities: Sequence[str] = ()
    #: Keys searched anywhere in the document for description / lease text.
    description_keys: Sequence[str] = ()


class RapidApiSource(ListingSource):
    """Shared behaviour for every RapidAPI marketplace wrapper."""

    field_map: FieldMap = FieldMap()
    #: Cap on detail lookups per run. Detail calls are the quota hogs.
    max_detail_lookups: int = 40
    #: Cap on search pages per ZIP.
    max_pages: int = 3

    def __init__(self, http: HttpClient, settings: RapidApiSettings) -> None:
        super().__init__(http)
        self.settings = settings

    # ------------------------------------------------------------- overrides
    @property
    def host(self) -> str:  # pragma: no cover - subclass responsibility
        raise NotImplementedError

    @property
    def enabled(self) -> bool:
        return bool(self.settings.api_key and self.host)

    @property
    def headers(self) -> dict[str, str]:
        return {
            "X-RapidAPI-Key": self.settings.api_key,
            "X-RapidAPI-Host": self.host,
            # Some wrappers only honour the lowercase spellings.
            "x-rapidapi-key": self.settings.api_key,
            "x-rapidapi-host": self.host,
        }

    def search_pages(
        self, criteria: SearchCriteria, result: SourceResult
    ) -> list[dict[str, Any]]:  # pragma: no cover - subclass responsibility
        raise NotImplementedError

    def fetch_detail(self, item: dict[str, Any], result: SourceResult) -> dict[str, Any] | None:
        """Return the detail payload for a search hit, or None when unsupported."""
        return None

    # ------------------------------------------------------------------ core
    def _fetch(self, criteria: SearchCriteria, result: SourceResult) -> list[Listing]:
        raw_items = self.search_pages(criteria, result)
        log.info("%s: %d raw search results", self.name, len(raw_items))

        listings: list[Listing] = []
        for item in raw_items:
            listing = self.to_listing(item)
            if listing is not None:
                listings.append(listing)

        listings = self._dedupe_within_source(listings)
        candidates = [item for item in listings if _passes_cheap_prefilter(item, criteria)]
        log.info(
            "%s: %d parsed, %d survive the numeric pre-filter",
            self.name, len(listings), len(candidates),
        )

        # Phase 2: enrich survivors with detail-only fields (description, lease
        # term, year built), which is what the hard filters actually need.
        for listing in candidates[: self.max_detail_lookups]:
            try:
                detail = self.fetch_detail(listing.raw, result)
            except HttpError as exc:
                result.errors.append(f"detail lookup failed for {listing.source_id}: {exc}")
                continue
            if detail:
                self.apply_detail(listing, detail)

        if len(candidates) > self.max_detail_lookups:
            message = (
                f"{len(candidates) - self.max_detail_lookups} candidates exceeded the "
                f"detail-lookup cap of {self.max_detail_lookups} and were evaluated on "
                "search data alone"
            )
            log.warning("%s: %s", self.name, message)
            result.notes.append(message)

        return candidates

    # ------------------------------------------------------------- mapping
    def to_listing(self, item: dict[str, Any]) -> Listing | None:
        """Map one provider record onto the internal schema."""
        fields = self.field_map
        price = first(item, fields.price, parse_price)
        address = first(item, fields.address, clean_text)
        if not address and not (first(item, fields.latitude) and first(item, fields.longitude)):
            return None  # unusable: no way to identify or locate the unit

        description = deep_get_text(item, fields.description_keys) if fields.description_keys else ""

        listing = self.make_listing(
            source_id=str(first(item, fields.source_id, default="") or ""),
            url=self.absolute_url(first(item, fields.url, clean_text, default="")),
            address=address or "",
            unit=first(item, fields.unit, clean_text, default="") or "",
            city=first(item, fields.city, clean_text, default="") or "",
            state=first(item, fields.state, clean_text, default="FL") or "FL",
            zip_code=first(item, fields.zip_code, clean_text, default="") or "",
            latitude=_as_float(first(item, fields.latitude)),
            longitude=_as_float(first(item, fields.longitude)),
            property_type=first(item, fields.property_type, clean_text, default="") or "",
            beds=first(item, fields.beds, parse_beds),
            baths=first(item, fields.baths, parse_baths),
            sqft=first(item, fields.sqft, parse_sqft),
            year_built=first(item, fields.year_built, parse_year_built),
            price=price,
            status=first(item, fields.status, clean_text, default="") or "",
            available_date=first(item, fields.available_date, clean_text),
            days_on_market=_as_int(first(item, fields.days_on_market)),
            broker=first(item, fields.broker, clean_text, default="") or "",
            title=first(item, fields.title, clean_text, default="") or "",
            description=description,
            photos=collect_strings(item, fields.photos, limit=12),
            amenities=collect_strings(item, fields.amenities, limit=40),
            raw=item,
        )
        return listing

    def apply_detail(self, listing: Listing, detail: dict[str, Any]) -> None:
        """Fold a detail payload into an already-mapped listing."""
        fields = self.field_map

        description = deep_get_text(
            detail, fields.description_keys or ("description", "text", "remarks")
        )
        if description and len(description) > len(listing.description):
            listing.description = description

        if listing.year_built is None:
            listing.year_built = first(detail, fields.year_built, parse_year_built)
        if listing.sqft is None:
            listing.sqft = first(detail, fields.sqft, parse_sqft)
        if listing.beds is None:
            listing.beds = first(detail, fields.beds, parse_beds)
        if listing.baths is None:
            listing.baths = first(detail, fields.baths, parse_baths)
        if not listing.property_type:
            listing.property_type = first(detail, fields.property_type, clean_text, default="") or ""
        if listing.price is None:
            listing.price = first(detail, fields.price, parse_price)

        new_photos = collect_strings(detail, fields.photos, limit=12)
        listing.photos.extend(p for p in new_photos if p not in listing.photos)
        new_amenities = collect_strings(detail, fields.amenities, limit=60)
        existing = {a.lower() for a in listing.amenities}
        listing.amenities.extend(a for a in new_amenities if a.lower() not in existing)

        listing.raw["detail"] = detail
        listing.refresh_derived()

    def absolute_url(self, url: str) -> str:
        """Wrappers often return a site-relative path."""
        if not url:
            return ""
        if url.startswith("http://") or url.startswith("https://"):
            return url
        return f"{self.site_base_url.rstrip('/')}/{url.lstrip('/')}"

    site_base_url: str = ""

    # ------------------------------------------------------------- utilities
    @staticmethod
    def _dedupe_within_source(listings: list[Listing]) -> list[Listing]:
        """Collapse repeats inside one provider's own paginated results."""
        seen: dict[str, Listing] = {}
        for listing in listings:
            key = listing.dedupe_key
            if key in seen:
                seen[key].merge_from(listing)
            else:
                seen[key] = listing
        return list(seen.values())

    def request_json(self, path: str, **kwargs: Any) -> Any:
        url = f"https://{self.host}{path}"
        headers = dict(self.headers)
        headers.update(kwargs.pop("headers", {}) or {})
        return self.http.get_json(url, headers=headers, **kwargs)

    def post_json(self, path: str, **kwargs: Any) -> Any:
        url = f"https://{self.host}{path}"
        headers = dict(self.headers)
        headers["Content-Type"] = "application/json"
        headers.update(kwargs.pop("headers", {}) or {})
        return self.http.post_json(url, headers=headers, **kwargs)


# ---------------------------------------------------------------------------
# Zillow wrapper
# ---------------------------------------------------------------------------
class ZillowRapidSource(RapidApiSource):
    name = "rapidapi_zillow"
    label = "Zillow (RapidAPI)"
    site_base_url = "https://www.zillow.com"
    search_path = "/propertyExtendedSearch"
    detail_path = "/property"

    field_map = FieldMap(
        source_id=("zpid", "id"),
        url=("detailUrl", "url", "hdpUrl"),
        address=("address", "streetAddress", "addressStreet", "location.address"),
        unit=("unit", "unitNumber", "address.unit", "hdpData.homeInfo.unit"),
        # Both spellings are covered: the property API returns flat camelCase
        # fields, while the search-results payload nests them under hdpData.
        city=("addressCity", "city", "address.city", "hdpData.homeInfo.city"),
        state=("addressState", "state", "address.state", "hdpData.homeInfo.state"),
        zip_code=("addressZipcode", "zipcode", "address.zipcode", "hdpData.homeInfo.zipcode"),
        latitude=("latitude", "lat", "location.latitude", "latLong.latitude",
                  "hdpData.homeInfo.latitude"),
        longitude=("longitude", "lng", "long", "location.longitude", "latLong.longitude",
                   "hdpData.homeInfo.longitude"),
        property_type=("propertyType", "homeType", "resoFacts.homeType",
                       "hdpData.homeInfo.homeType"),
        beds=("bedrooms", "beds", "hdpData.homeInfo.bedrooms"),
        baths=("bathrooms", "baths", "hdpData.homeInfo.bathrooms"),
        sqft=("livingArea", "livingAreaValue", "resoFacts.livingArea", "area",
              "hdpData.homeInfo.livingArea", "lotAreaValue"),
        year_built=("yearBuilt", "resoFacts.yearBuilt", "hdpData.homeInfo.yearBuilt"),
        price=("price", "unformattedPrice", "priceForHDP", "hdpData.homeInfo.price",
               "rentZestimate"),
        status=("listingStatus", "homeStatus"),
        available_date=("resoFacts.availabilityDate", "availabilityDate"),
        days_on_market=("daysOnZillow", "resoFacts.daysOnZillow", "timeOnZillow"),
        broker=("brokerName", "attributionInfo.brokerName", "listing_agent.name"),
        title=("statusText", "resoFacts.propertySubType"),
        photos=("imgSrc", "hiResImageLink", "photos[*]", "responsivePhotos[*]",
                "originalPhotos[*].mixedSources.jpeg[0].url", "carouselPhotos[*]"),
        amenities=("resoFacts.associationAmenities", "resoFacts.communityFeatures",
                   "resoFacts.interiorFeatures", "resoFacts.exteriorFeatures",
                   "resoFacts.appliances", "resoFacts.hasView", "resoFacts.view"),
        description_keys=("description", "leaseTerm", "publicRemarks", "remarks",
                          "homeDescription", "marketingName"),
    )

    @property
    def host(self) -> str:
        return self.settings.zillow_host

    def search_pages(self, criteria: SearchCriteria, result: SourceResult) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for location in _locations(criteria):
            for page in range(1, self.max_pages + 1):
                params = {
                    "location": location,
                    "status_type": "ForRent",
                    "home_type": "Condos",
                    "rentMinPrice": criteria.min_price,
                    "rentMaxPrice": criteria.max_price,
                    "beds_min": int(criteria.min_beds),
                    "baths_min": int(criteria.min_baths),
                    "sqftMin": criteria.min_sqft,
                    "page": page,
                    "sort": "Newest",
                }
                payload = self.request_json(self.search_path, params=params)
                result.requests_made += 1

                props = payload.get("props") if isinstance(payload, dict) else None
                if not props:
                    props = find_result_array(payload)
                if not props:
                    break
                items.extend(props)

                total_pages = (payload or {}).get("totalPages") if isinstance(payload, dict) else None
                if not total_pages or page >= int(total_pages):
                    break
        return items

    def fetch_detail(self, item: dict[str, Any], result: SourceResult) -> dict[str, Any] | None:
        zpid = item.get("zpid") or item.get("id")
        if not zpid:
            return None
        payload = self.request_json(self.detail_path, params={"zpid": str(zpid)})
        result.requests_made += 1
        return payload if isinstance(payload, dict) else None


# ---------------------------------------------------------------------------
# Realtor.com wrapper
# ---------------------------------------------------------------------------
class RealtorRapidSource(RapidApiSource):
    name = "rapidapi_realtor"
    label = "Realtor.com (RapidAPI)"
    site_base_url = "https://www.realtor.com/realestateandhomes-detail"
    search_path = "/properties/v3/list"
    detail_path = "/properties/v3/detail"

    field_map = FieldMap(
        source_id=("property_id", "listing_id", "propertyId"),
        url=("href", "permalink", "rdc_web_url"),
        address=("location.address.line", "address.line", "location.address.formatted_address"),
        unit=("location.address.unit", "address.unit"),
        city=("location.address.city", "address.city"),
        state=("location.address.state_code", "address.state_code", "location.address.state"),
        zip_code=("location.address.postal_code", "address.postal_code"),
        latitude=("location.address.coordinate.lat", "location.coordinate.lat", "address.coordinate.lat"),
        longitude=("location.address.coordinate.lon", "location.coordinate.lon", "address.coordinate.lon"),
        property_type=("description.type", "prop_type", "description.sub_type"),
        beds=("description.beds", "description.beds_max", "beds"),
        baths=("description.baths", "description.baths_consolidated", "baths"),
        sqft=("description.sqft", "building_size.size", "description.lot_sqft"),
        year_built=("description.year_built", "year_built"),
        price=("list_price", "price", "list_price_min", "community.price_max"),
        status=("status", "listing_status"),
        available_date=("description.date_available", "list_date"),
        days_on_market=("days_on_market", "flags.is_new_listing"),
        broker=("branding[0].name", "source.agents[0].office_name", "advertisers[0].office.name"),
        title=("description.name", "branding[0].name"),
        photos=("primary_photo.href", "photos[*].href", "photo.href"),
        amenities=("tags", "details[*].text", "description.tags", "features[*].text"),
        description_keys=("text", "description", "lease_term", "remarks", "public_remarks"),
    )

    @property
    def host(self) -> str:
        return self.settings.realtor_host

    def search_pages(self, criteria: SearchCriteria, result: SourceResult) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for postal_code in criteria.zip_codes or [""]:
            for page in range(self.max_pages):
                body = {
                    "limit": 200,
                    "offset": page * 200,
                    "status": ["for_rent"],
                    "type": ["condos", "condo_townhome", "condo_townhome_rowhome_coop", "apartment"],
                    "list_price": {"min": criteria.min_price, "max": criteria.max_price},
                    "beds": {"min": int(criteria.min_beds)},
                    "baths": {"min": int(criteria.min_baths)},
                    "sqft": {"min": criteria.min_sqft},
                    "sort": {"direction": "desc", "field": "list_date"},
                }
                if postal_code:
                    body["postal_code"] = postal_code
                else:
                    body["city"] = criteria.cities[0] if criteria.cities else "Miami Beach"
                    body["state_code"] = criteria.state

                payload = self.post_json(self.search_path, json_body=body)
                result.requests_made += 1

                results = (
                    payload.get("data", {}).get("home_search", {}).get("results")
                    if isinstance(payload, dict) else None
                )
                if results is None:
                    results = find_result_array(payload)
                if not results:
                    break
                items.extend(results)
                if len(results) < 200:
                    break
        return items

    def fetch_detail(self, item: dict[str, Any], result: SourceResult) -> dict[str, Any] | None:
        property_id = item.get("property_id") or item.get("listing_id")
        if not property_id:
            return None
        payload = self.request_json(self.detail_path, params={"property_id": str(property_id)})
        result.requests_made += 1
        if isinstance(payload, dict):
            return payload.get("data", {}).get("home") or payload
        return None

    def absolute_url(self, url: str) -> str:
        if url and not url.startswith("http"):
            return f"https://www.realtor.com/realestateandhomes-detail/{url.lstrip('/')}"
        return url


# ---------------------------------------------------------------------------
# Redfin wrapper
# ---------------------------------------------------------------------------
class RedfinRapidSource(RapidApiSource):
    name = "rapidapi_redfin"
    label = "Redfin (RapidAPI)"
    site_base_url = "https://www.redfin.com"
    #: Redfin wrappers on RapidAPI differ more than the others; both the path
    #: and the location parameter name are overridable from settings.
    search_path = "/properties/search-rent"
    location_param = "location"

    field_map = FieldMap(
        source_id=("propertyId", "listingId", "id", "mlsId"),
        url=("url", "detailUrl", "propertyUrl"),
        address=("streetLine.value", "streetLine", "address", "addressLine1", "fullAddress"),
        unit=("unitNumber.value", "unitNumber", "unit"),
        city=("city", "address.city"),
        state=("state", "stateCode", "address.state"),
        zip_code=("zip", "postalCode", "zipCode", "address.zip"),
        latitude=("latLong.latitude", "latLong.value.latitude", "latitude", "lat"),
        longitude=("latLong.longitude", "latLong.value.longitude", "longitude", "lng"),
        property_type=("propertyType", "uiPropertyType", "propertyTypeName"),
        beds=("beds", "bedrooms", "numBeds"),
        baths=("baths", "bathrooms", "numBaths"),
        sqft=("sqFt.value", "sqFt", "squareFeet", "livingArea"),
        year_built=("yearBuilt.value", "yearBuilt"),
        price=("price.value", "price", "rentPriceRange.min", "listingPrice"),
        status=("mlsStatus", "status", "searchStatus"),
        available_date=("availableDate", "dateAvailable"),
        days_on_market=("dom.value", "daysOnMarket", "timeOnRedfin.value"),
        broker=("listingAgent.name", "brokerName", "mlsBrokerName"),
        title=("marketingName", "buildingName"),
        photos=("photos[*].url", "photoUrl", "primaryPhotoUrl", "media[*].url"),
        amenities=("amenities", "buildingAmenities", "features[*].text", "keyFacts[*].text"),
        description_keys=("remarks", "description", "listingRemarks", "leaseTerm", "marketingRemarks"),
    )

    @property
    def host(self) -> str:
        return self.settings.redfin_host

    def search_pages(self, criteria: SearchCriteria, result: SourceResult) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for location in _locations(criteria):
            params = {
                self.location_param: location,
                "min_price": criteria.min_price,
                "max_price": criteria.max_price,
                "min_beds": int(criteria.min_beds),
                "min_baths": int(criteria.min_baths),
                "min_sqft": criteria.min_sqft,
                "property_type": "condo",
            }
            payload = self.request_json(self.search_path, params=params)
            result.requests_made += 1
            found = find_result_array(payload)
            if found:
                items.extend(found)
        return items


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _locations(criteria: SearchCriteria) -> list[str]:
    """Query strings for wrappers that take a free-text location.

    ZIP codes are preferred: they are unambiguous, whereas "Miami Beach, FL"
    quietly includes or excludes neighbouring municipalities depending on the
    upstream geocoder.
    """
    if criteria.zip_codes:
        return list(criteria.zip_codes)
    return [f"{city}, {criteria.state}" for city in criteria.cities]


def _passes_cheap_prefilter(listing: Listing, criteria: SearchCriteria) -> bool:
    """Numeric-only gate applied before spending a detail call.

    Deliberately lenient: anything unknown passes, because the detail lookup or
    the county assessor may still supply it. Only a *stated* violation drops the
    listing here.
    """
    if listing.price is not None and not (
        criteria.min_price <= listing.price <= criteria.max_price
    ):
        return False
    if listing.beds is not None and listing.beds < criteria.min_beds:
        return False
    if listing.baths is not None and listing.baths < criteria.min_baths:
        return False
    if listing.sqft is not None and listing.sqft < criteria.min_sqft:
        return False
    # 1.5x slack: the exact ocean check happens in filters.py, after the county
    # lookup may have corrected the coordinates.
    return not (
        listing.miles_from_ocean is not None
        and listing.miles_from_ocean > criteria.max_miles_from_ocean * 1.5
    )


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def build_rapidapi_sources(http: HttpClient, settings: RapidApiSettings) -> list[RapidApiSource]:
    """Instantiate the wrappers named in ``RAPIDAPI_ENABLED_SOURCES``."""
    registry = {
        "zillow": ZillowRapidSource,
        "realtor": RealtorRapidSource,
        "redfin": RedfinRapidSource,
    }
    sources: list[RapidApiSource] = []
    for key in settings.enabled_sources:
        factory = registry.get(key.strip().lower())
        if factory is None:
            log.warning("unknown RapidAPI source %r in RAPIDAPI_ENABLED_SOURCES", key)
            continue
        sources.append(factory(http, settings))
    return sources
