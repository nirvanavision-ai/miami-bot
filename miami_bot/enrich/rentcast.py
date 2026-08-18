"""RentCast: long-term rent AVM and property-record lookup.

Two endpoints matter here:

``/avm/rent/long-term``
    The rent estimate the asking price gets compared against. Passing the unit's
    beds/baths/sqft alongside the address materially tightens the estimate,
    because otherwise RentCast infers them from its own record of the parcel --
    which for a condo tower is the *building*, not the unit.

``/properties``
    RentCast's copy of the public property record. Used as a second opinion on
    square footage and year built when the county lookup comes back empty.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import RentCastSettings
from ..models import Listing
from ..util.http import HttpClient, NotFound
from ..util.logging import get_logger

log = get_logger(__name__)

# RentCast's propertyType vocabulary.
_PROPERTY_TYPE_MAP = {
    "condo": "Condo",
    "condominium": "Condo",
    "co-op": "Condo",
    "coop": "Condo",
    "apartment": "Apartment",
    "townhouse": "Townhouse",
    "single family": "Single Family",
}


@dataclass
class RentEstimate:
    rent: int | None = None
    low: int | None = None
    high: int | None = None
    comparable_count: int = 0
    source: str = "rentcast"


class RentCastClient:
    """Thin client over the RentCast v1 API."""

    def __init__(self, http: HttpClient, settings: RentCastSettings) -> None:
        self.http = http
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return self.settings.enabled

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": self.settings.api_key, "Accept": "application/json"}

    def _address_param(self, listing: Listing) -> str | None:
        """RentCast wants a single-line address including the unit."""
        parts = listing.address_parts
        if parts is None or not parts.is_usable:
            return None
        line = parts.street_line.title()
        if parts.unit:
            line = f"{line} #{parts.unit.upper()}"
        locality = ", ".join(p for p in (parts.city, parts.state) if p)
        postal = parts.zip5
        return ", ".join(p for p in (line, locality, postal) if p)

    # ------------------------------------------------------------------ AVM
    def rent_estimate(self, listing: Listing) -> RentEstimate | None:
        """Long-term rent AVM for this specific unit."""
        address = self._address_param(listing)
        if not address:
            return None

        params: dict[str, Any] = {"address": address}
        property_type = _PROPERTY_TYPE_MAP.get((listing.property_type or "").strip().lower())
        if property_type:
            params["propertyType"] = property_type
        if listing.beds is not None:
            params["bedrooms"] = listing.beds
        if listing.baths is not None:
            params["bathrooms"] = listing.baths
        if listing.sqft:
            params["squareFootage"] = listing.sqft

        try:
            payload = self.http.get_json(
                f"{self.settings.base_url}/avm/rent/long-term",
                params=params,
                headers=self._headers,
            )
        except NotFound:
            log.info("rentcast: no rent AVM for %s", address)
            return None

        if not isinstance(payload, dict):
            return None
        return RentEstimate(
            rent=_as_int(payload.get("rent")),
            low=_as_int(payload.get("rentRangeLow")),
            high=_as_int(payload.get("rentRangeHigh")),
            comparable_count=len(payload.get("comparables") or []),
        )

    def value_estimate(self, listing: Listing) -> int | None:
        """Sale-value AVM, used for the gross-yield figure in alerts."""
        address = self._address_param(listing)
        if not address:
            return None
        try:
            payload = self.http.get_json(
                f"{self.settings.base_url}/avm/value",
                params={"address": address},
                headers=self._headers,
            )
        except NotFound:
            return None
        return _as_int(payload.get("price")) if isinstance(payload, dict) else None

    # ------------------------------------------------------- property record
    def property_record(self, listing: Listing) -> dict[str, Any] | None:
        """RentCast's public-record view of the unit (sqft, year built, beds)."""
        address = self._address_param(listing)
        if not address:
            return None
        try:
            payload = self.http.get_json(
                f"{self.settings.base_url}/properties",
                params={"address": address},
                headers=self._headers,
            )
        except NotFound:
            return None

        records = payload if isinstance(payload, list) else [payload]
        for record in records:
            if isinstance(record, dict) and record:
                return record
        return None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(round(number)) if number > 0 else None
