"""Module 1: RealtyAPI aggregator client.

Unlike the RapidAPI wrappers, which have well-known (if unstable) shapes, a
RealtyAPI-style vendor endpoint varies by account and plan. This adapter is
therefore fully configuration-driven:

* ``REALTYAPI_BASE_URL`` + ``REALTYAPI_SEARCH_PATH`` set the endpoint,
* ``REALTYAPI_AUTH_STYLE`` picks between header, bearer and query-param auth,
* the response is parsed through the same tolerant path machinery as everything
  else, with :func:`~miami_bot.sources.extract.find_result_array` as the
  structural fallback.

The practical effect is that pointing this at a different aggregator is an
``.env`` change, not a code change.
"""

from __future__ import annotations

from typing import Any

from ..config import RealtyApiSettings, SearchCriteria
from ..models import Listing
from ..util.http import HttpClient
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
from .rapidapi import FieldMap, _as_float, _as_int, _passes_cheap_prefilter

log = get_logger(__name__)


class RealtyApiSource(ListingSource):
    name = "realtyapi"
    label = "RealtyAPI"

    #: Broad candidate paths -- aggregators normalize to MLS-ish names, so we
    #: cover both the RESO spellings and the common camelCase variants.
    field_map = FieldMap(
        source_id=("id", "listingId", "listing_id", "mlsNumber", "ListingId", "ListingKey"),
        url=("url", "listingUrl", "detailUrl", "permalink", "href"),
        address=(
            "address.line", "address.streetAddress", "address.full", "address",
            "StreetAddress", "UnparsedAddress", "location.address", "streetAddress",
        ),
        unit=("address.unit", "unitNumber", "UnitNumber", "address.unitNumber", "unit"),
        city=("address.city", "city", "City", "location.city"),
        state=("address.state", "state", "StateOrProvince", "location.state"),
        zip_code=("address.postalCode", "address.zip", "postalCode", "PostalCode", "zip"),
        latitude=("coordinates.latitude", "address.latitude", "latitude", "geo.lat", "Latitude"),
        longitude=("coordinates.longitude", "address.longitude", "longitude", "geo.lng", "Longitude"),
        property_type=("propertyType", "property_type", "PropertySubType", "type", "homeType"),
        beds=("bedrooms", "beds", "BedroomsTotal", "details.bedrooms"),
        baths=("bathrooms", "baths", "BathroomsTotalInteger", "details.bathrooms"),
        sqft=("squareFeet", "livingArea", "sqft", "LivingArea", "details.squareFeet", "size"),
        year_built=("yearBuilt", "YearBuilt", "details.yearBuilt"),
        price=("price", "rent", "listPrice", "ListPrice", "rentPrice", "monthlyRent"),
        status=("status", "listingStatus", "StandardStatus", "MlsStatus"),
        available_date=("availableDate", "AvailabilityDate", "dateAvailable"),
        days_on_market=("daysOnMarket", "DaysOnMarket", "dom"),
        broker=("office.name", "listOfficeName", "ListOfficeName", "agent.office", "brokerage"),
        title=("title", "name", "headline", "marketingName"),
        photos=("photos[*]", "photos[*].url", "images[*]", "images[*].url",
                "media[*].MediaURL", "primaryPhoto", "thumbnail"),
        amenities=("amenities", "features", "AssociationAmenities", "InteriorFeatures",
                   "ExteriorFeatures", "communityFeatures", "tags"),
        description_keys=("description", "remarks", "PublicRemarks", "publicRemarks",
                          "leaseTerm", "LeaseTerm", "text", "summary"),
    )

    #: Pagination knobs. Aggregators split roughly evenly between these two
    #: conventions, so we send both -- extras are ignored by every API we know.
    page_size = 200
    max_pages = 5

    def __init__(self, http: HttpClient, settings: RealtyApiSettings) -> None:
        super().__init__(http)
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return self.settings.enabled

    # ------------------------------------------------------------------ auth
    def _auth(self) -> tuple[dict[str, str], dict[str, str]]:
        """Return (headers, query params) carrying the credential."""
        style = self.settings.auth_style
        if style == "header_bearer":
            return {"Authorization": f"Bearer {self.settings.api_key}"}, {}
        if style == "query_param":
            return {}, {self.settings.auth_param: self.settings.api_key}
        return {"X-Api-Key": self.settings.api_key, "x-api-key": self.settings.api_key}, {}

    # ------------------------------------------------------------------ core
    def _fetch(self, criteria: SearchCriteria, result: SourceResult) -> list[Listing]:
        headers, auth_params = self._auth()
        url = f"{self.settings.base_url}{self.settings.search_path}"

        raw_items: list[dict[str, Any]] = []
        for postal_code in criteria.zip_codes or [""]:
            for page in range(1, self.max_pages + 1):
                params: dict[str, Any] = {
                    "status": "active",
                    "propertyType": "condo",
                    "minPrice": criteria.min_price,
                    "maxPrice": criteria.max_price,
                    "minBeds": int(criteria.min_beds),
                    "minBaths": int(criteria.min_baths),
                    "minSquareFeet": criteria.min_sqft,
                    "state": criteria.state,
                    "limit": self.page_size,
                    "page": page,
                    "offset": (page - 1) * self.page_size,
                }
                if postal_code:
                    params["postalCode"] = postal_code
                    params["zip"] = postal_code
                elif criteria.cities:
                    params["city"] = criteria.cities[0]
                params.update(auth_params)

                payload = self.http.get_json(url, params=params, headers=headers)
                result.requests_made += 1

                items = self._results_from(payload)
                if not items:
                    break
                raw_items.extend(items)
                if len(items) < self.page_size:
                    break

        listings: list[Listing] = []
        seen: dict[str, Listing] = {}
        for item in raw_items:
            listing = self.to_listing(item)
            if listing is None:
                continue
            key = listing.dedupe_key
            if key in seen:
                seen[key].merge_from(listing)
            else:
                seen[key] = listing
                listings.append(listing)

        candidates = [item for item in listings if _passes_cheap_prefilter(item, criteria)]
        log.info(
            "%s: %d raw, %d parsed, %d after pre-filter",
            self.name, len(raw_items), len(listings), len(candidates),
        )
        return candidates

    @staticmethod
    def _results_from(payload: Any) -> list[dict[str, Any]]:
        """Pull the results array out of whatever envelope the vendor uses."""
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            for key in ("listings", "results", "properties", "data", "items", "records"):
                value = payload.get(key)
                if isinstance(value, list) and value:
                    return [item for item in value if isinstance(item, dict)]
                if isinstance(value, dict):
                    for nested_key in ("listings", "results", "properties", "items"):
                        nested = value.get(nested_key)
                        if isinstance(nested, list) and nested:
                            return [item for item in nested if isinstance(item, dict)]
        return find_result_array(payload)

    # --------------------------------------------------------------- mapping
    def to_listing(self, item: dict[str, Any]) -> Listing | None:
        fields = self.field_map
        address = first(item, fields.address, clean_text)
        latitude = _as_float(first(item, fields.latitude))
        longitude = _as_float(first(item, fields.longitude))
        if not address and latitude is None:
            return None

        return self.make_listing(
            source_id=str(first(item, fields.source_id, default="") or ""),
            url=first(item, fields.url, clean_text, default="") or "",
            address=address or "",
            unit=first(item, fields.unit, clean_text, default="") or "",
            city=first(item, fields.city, clean_text, default="") or "",
            state=first(item, fields.state, clean_text, default="FL") or "FL",
            zip_code=first(item, fields.zip_code, clean_text, default="") or "",
            latitude=latitude,
            longitude=longitude,
            property_type=first(item, fields.property_type, clean_text, default="") or "",
            beds=first(item, fields.beds, parse_beds),
            baths=first(item, fields.baths, parse_baths),
            sqft=first(item, fields.sqft, parse_sqft),
            year_built=first(item, fields.year_built, parse_year_built),
            price=first(item, fields.price, parse_price),
            status=first(item, fields.status, clean_text, default="") or "",
            available_date=first(item, fields.available_date, clean_text),
            days_on_market=_as_int(first(item, fields.days_on_market)),
            broker=first(item, fields.broker, clean_text, default="") or "",
            title=first(item, fields.title, clean_text, default="") or "",
            description=deep_get_text(item, fields.description_keys),
            photos=collect_strings(item, fields.photos, limit=12),
            amenities=collect_strings(item, fields.amenities, limit=40),
            raw=item,
        )
