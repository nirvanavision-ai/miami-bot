"""RentCast active rental listings -- Module 1 on a free-tier key.

RentCast is already a Module 3 provider here (rent AVM). Its
``/listings/rental/long-term`` endpoint is a *listing source* in its own right,
and it is the best-value option in this project:

* the free tier covers a personal search comfortably, and one call per ZIP with
  ``limit=500`` costs 5 requests per run,
* it is a sanctioned API, so there is no proxy, no bot wall and no ToS grey
  area,
* the records are structured and clean -- no price strings to unpick.

The one real gap: **RentCast returns no description and no photos.** Nothing
here can evidence "ocean view" or a lease term. That is why
:meth:`Listing.has_descriptive_text` exists -- such listings surface flagged
for confirmation rather than being rejected for amenities they were never given
a chance to state.
"""

from __future__ import annotations

from typing import Any

from ..config import RentCastSettings, SearchCriteria
from ..models import Listing
from ..util.http import HttpClient, HttpError, NotFound
from ..util.logging import get_logger
from ..util.text import clean_text, parse_baths, parse_beds, parse_price, parse_sqft
from .base import ListingSource, SourceResult
from .rapidapi import _as_float, _as_int, _passes_cheap_prefilter

log = get_logger(__name__)

#: RentCast's own propertyType vocabulary, mapped from ours.
_CONDO_TYPES = ("Condo", "Apartment")


class RentCastListingsSource(ListingSource):
    name = "rentcast_listings"
    label = "RentCast (active rentals)"

    #: Maximum the API accepts in one call. One call per ZIP keeps free-tier
    #: usage at 5 requests per run.
    page_size = 500
    #: Ignore listings that have sat unchanged for longer than this.
    max_days_old = 90

    def __init__(self, http: HttpClient, settings: RentCastSettings, enabled: bool = True) -> None:
        super().__init__(http)
        self.settings = settings
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return bool(self._enabled and self.settings.enabled)

    def _fetch(self, criteria: SearchCriteria, result: SourceResult) -> list[Listing]:
        headers = {"X-Api-Key": self.settings.api_key, "Accept": "application/json"}
        url = f"{self.settings.base_url}/listings/rental/long-term"

        records: list[dict[str, Any]] = []
        for zip_code in criteria.zip_codes or [""]:
            scope: dict[str, Any] = {}
            if zip_code:
                scope["zipCode"] = zip_code
            else:
                scope["city"] = criteria.cities[0] if criteria.cities else "Miami Beach"
                scope["state"] = criteria.state

            found = self._search(url, headers, scope, criteria, result)
            records.extend(found)

        seen: dict[str, Listing] = {}
        for record in records:
            listing = self.to_listing(record)
            if listing is None:
                continue
            key = listing.dedupe_key
            if key in seen:
                seen[key].merge_from(listing)
            else:
                seen[key] = listing

        listings = list(seen.values())
        candidates = [item for item in listings if _passes_cheap_prefilter(item, criteria)]
        log.info(
            "%s: %d records, %d unique, %d after pre-filter",
            self.name, len(records), len(listings), len(candidates),
        )
        if candidates:
            result.notes.append(
                "RentCast publishes no description, photos or listing URL; amenity and "
                "lease-term checks on these listings are advisory"
            )
        return candidates

    def _search(
        self,
        url: str,
        headers: dict[str, str],
        scope: dict[str, Any],
        criteria: SearchCriteria,
        result: SourceResult,
    ) -> list[dict[str, Any]]:
        """One scoped search, narrowed server-side where the API allows it.

        ``bedrooms``, ``bathrooms``, ``squareFootage`` and ``price`` accept
        numeric ranges, so the budget and layout constraints are pushed to
        RentCast rather than being paid for in wasted result slots.

        The range syntax is not pinned in the OpenAPI spec (it links out to the
        docs), so a mismatch is treated as a real possibility: a ranged query
        that comes back empty, or is rejected outright, is retried once
        unfiltered. A wrong guess therefore costs one extra call, never a
        silently empty search.
        """
        ranged = dict(scope)
        ranged.update({
            "status": "Active",
            "limit": self.page_size,
            "daysOld": self.max_days_old,
            "bedrooms": _range(criteria.min_beds, None),
            "bathrooms": _range(criteria.min_baths, None),
            "squareFootage": _range(criteria.min_sqft, None),
            "price": _range(criteria.min_price, criteria.max_price),
        })
        plain = dict(scope)
        plain.update({"status": "Active", "limit": self.page_size,
                      "daysOld": self.max_days_old})

        for attempt, params in enumerate((ranged, plain)):
            try:
                response = self.http.request("GET", url, params=params, headers=headers)
            except NotFound:
                return []
            except HttpError as exc:
                if attempt == 0 and getattr(exc, "status", None) == 400:
                    log.warning(
                        "%s: server-side range filters rejected (%s); retrying unfiltered",
                        self.name, exc,
                    )
                    result.notes.append("range filters rejected; retried unfiltered")
                    continue
                raise
            result.requests_made += 1

            records = _records_from(response)
            total = response.headers.get("X-Total-Count")
            if total and total.isdigit() and int(total) > len(records):
                result.notes.append(
                    f"{scope.get('zipCode', 'search')}: {total} listings matched but "
                    f"{len(records)} returned -- raise the page size or paginate"
                )

            if records or attempt == 1:
                return records

            log.info(
                "%s: ranged query returned nothing for %s; retrying unfiltered",
                self.name, scope,
            )
        return []

    # --------------------------------------------------------------- mapping
    def to_listing(self, record: dict[str, Any]) -> Listing | None:
        line = clean_text(record.get("addressLine1"))
        formatted = clean_text(record.get("formattedAddress"))
        if not line and not formatted:
            return None

        # addressLine2 carries the unit ("Apt 1502"); formattedAddress folds it
        # into one string. Prefer the split form -- it hashes more reliably.
        address = line or formatted
        unit = clean_text(record.get("addressLine2"))

        agent = record.get("listingAgent") or {}
        office = record.get("listingOffice") or {}
        broker = clean_text(office.get("name") or agent.get("name") or "")
        # RentCast returns no listing URL, so the MLS number is the only handle
        # for looking the unit up. Attaching it to the broker keeps it visible
        # in the alert instead of buried in the raw payload.
        mls_number = clean_text(record.get("mlsNumber"))
        if mls_number:
            broker = f"{broker} (MLS {mls_number})" if broker else f"MLS {mls_number}"

        return self.make_listing(
            source_id=str(record.get("id") or ""),
            url="",   # RentCast publishes no listing URL -- see mls_number above
            address=address,
            unit=unit,
            city=clean_text(record.get("city")),
            state=clean_text(record.get("state")) or "FL",
            zip_code=clean_text(record.get("zipCode")),
            latitude=_as_float(record.get("latitude")),
            longitude=_as_float(record.get("longitude")),
            property_type=clean_text(record.get("propertyType")),
            beds=parse_beds(record.get("bedrooms")),
            baths=parse_baths(record.get("bathrooms")),
            sqft=parse_sqft(record.get("squareFootage")),
            year_built=_as_int(record.get("yearBuilt")),
            price=parse_price(record.get("price")),
            status=clean_text(record.get("status")),
            available_date=clean_text(record.get("listedDate")) or None,
            days_on_market=_as_int(record.get("daysOnMarket")),
            broker=broker,
            raw=record,
        )


def _range(minimum: float | int | None, maximum: float | int | None) -> str:
    """RentCast numeric-range syntax: ``min:max``, either bound optional."""
    low = "" if minimum is None else f"{int(minimum)}"
    high = "" if maximum is None else f"{int(maximum)}"
    return f"{low}:{high}"


def _records_from(response: Any) -> list[dict[str, Any]]:
    try:
        payload = response.json()
    except ValueError:
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        nested = payload.get("listings") or payload.get("data")
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, dict)]
    return []
