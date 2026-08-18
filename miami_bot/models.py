"""The single internal listing schema.

Every source adapter -- RapidAPI/Zillow, RapidAPI/Realtor, RapidAPI/Redfin,
RealtyAPI, and the ScrapingBee fallback -- normalizes into :class:`Listing`.
Nothing downstream (filters, dedupe, enrichment, alerts) ever touches a
provider-shaped payload.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .geo import ocean_proximity
from .normalize import AddressParts, building_key, geo_key, parse_address, unit_key
from .util.text import LeaseTerm, clean_text, parse_lease_term


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


@dataclass
class Enrichment:
    """Module 3 output: AVM comparisons and county-verified physical specs."""

    # RentCast / HouseCanary
    rent_estimate: int | None = None
    rent_estimate_low: int | None = None
    rent_estimate_high: int | None = None
    rent_estimate_source: str | None = None
    value_estimate: int | None = None
    value_estimate_source: str | None = None

    # Derived
    price_to_estimate_ratio: float | None = None
    gross_yield_pct: float | None = None
    verdict: str | None = None            # "below market" | "at market" | "above market"

    # County assessor / GIS
    county_sqft: int | None = None
    county_year_built: int | None = None
    county_folio: str | None = None
    county_source: str | None = None
    county_conflict: bool = False         # portal vs county disagree materially

    errors: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """True when nothing was actually enriched.

        An empty Enrichment must not overwrite a stored one -- a listing that
        was skipped this run (because it failed the filters before Module 3 ran)
        would otherwise lose the AVM and county data from an earlier run.
        """
        return not any((
            self.rent_estimate, self.value_estimate, self.county_sqft,
            self.county_year_built, self.county_folio,
        ))

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str)

    @classmethod
    def from_json(cls, payload: str | None) -> Enrichment:
        if not payload:
            return cls()
        try:
            data = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            return cls()
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Listing:
    """One rental unit as seen by one source at one point in time."""

    # --- provenance ---------------------------------------------------------
    source: str                                  # "rapidapi_zillow", "scrapingbee_redfin", ...
    source_id: str = ""                          # provider's own id (zpid, mls, listing id)
    url: str = ""

    # --- address ------------------------------------------------------------
    address: str = ""                            # raw display address from the provider
    unit: str = ""
    city: str = ""
    state: str = "FL"
    zip_code: str = ""
    latitude: float | None = None
    longitude: float | None = None

    # --- specs --------------------------------------------------------------
    property_type: str = ""
    beds: float | None = None
    baths: float | None = None
    sqft: int | None = None
    year_built: int | None = None

    # --- commercials --------------------------------------------------------
    price: int | None = None                     # monthly rent, USD
    available_date: str | None = None
    days_on_market: int | None = None
    status: str = ""

    # --- copy ---------------------------------------------------------------
    title: str = ""
    description: str = ""
    amenities: list[str] = field(default_factory=list)
    photos: list[str] = field(default_factory=list)
    broker: str = ""

    # --- derived (filled by post_init / enrichment) -------------------------
    address_parts: AddressParts | None = None
    unit_hash: str = ""
    building_hash: str = ""
    geo_hash: str = ""
    lease_term: LeaseTerm | None = None
    miles_from_ocean: float | None = None
    is_oceanfront: bool = False
    amenity_matches: dict[str, list[str]] = field(default_factory=dict)
    enrichment: Enrichment = field(default_factory=Enrichment)

    fetched_at: datetime = field(default_factory=utcnow)
    raw: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ setup
    def __post_init__(self) -> None:
        self.refresh_derived()

    def refresh_derived(self) -> None:
        """Recompute hashes, lease term and ocean distance from current fields.

        Called on construction and again after enrichment fills in gaps.
        """
        parts = parse_address(
            self.address,
            city=self.city,
            state=self.state,
            zip_code=self.zip_code,
            unit=self.unit,
        )
        self.address_parts = parts
        self.city = parts.city or clean_text(self.city)
        self.state = parts.state or clean_text(self.state) or "FL"
        self.zip_code = parts.zip5 or clean_text(self.zip_code)
        self.unit = parts.unit or self.unit

        self.unit_hash = unit_key(parts)
        self.building_hash = building_key(parts)
        self.geo_hash = geo_key(self.latitude, self.longitude, parts.unit)

        self.lease_term = parse_lease_term(self.title, self.description, self.searchable_terms())

        proximity = ocean_proximity(self.latitude, self.longitude)
        self.miles_from_ocean = proximity.miles
        self.is_oceanfront = proximity.is_oceanfront

    # ------------------------------------------------------------- identities
    @property
    def dedupe_key(self) -> str:
        """Stable cross-portal identity, best available.

        Falls back through unit hash -> geo hash -> provider id so that a
        listing is never silently merged with an unrelated one.
        """
        if self.unit_hash:
            return f"u:{self.unit_hash}"
        if self.geo_hash:
            return f"g:{self.geo_hash}"
        return f"s:{self.source}:{self.source_id or self.url}"

    @property
    def source_key(self) -> str:
        """Identity of this specific sighting (one row per source per unit)."""
        return f"{self.source}:{self.source_id or self.url or self.dedupe_key}"

    @property
    def display_address(self) -> str:
        if self.address_parts and self.address_parts.is_usable:
            return self.address_parts.display()
        return clean_text(self.address) or "(address unavailable)"

    # ------------------------------------------------------------------- text
    def searchable_terms(self) -> str:
        """Everything a keyword filter should look at, as one blob."""
        chunks = [
            self.title,
            self.description,
            " ".join(self.amenities),
            self.status,
            self.property_type,
        ]
        return " . ".join(clean_text(c) for c in chunks if c)

    @property
    def primary_photo(self) -> str | None:
        return self.photos[0] if self.photos else None

    @property
    def price_per_sqft(self) -> float | None:
        if self.price and self.sqft:
            return round(self.price / self.sqft, 2)
        return None

    # -------------------------------------------------------------- merging
    def merge_from(self, other: Listing) -> None:
        """Fill blanks on this listing from a duplicate found on another portal.

        The incumbent wins on every field it already has; the duplicate only
        contributes what is missing. Photos and amenities union together, which
        is how a Redfin sighting can supply the floor plan a Zillow sighting
        lacked.
        """
        for attribute in (
            "source_id", "url", "address", "unit", "city", "zip_code",
            "property_type", "title", "description", "broker", "status",
            "available_date",
        ):
            if not getattr(self, attribute) and getattr(other, attribute):
                setattr(self, attribute, getattr(other, attribute))

        for attribute in (
            "beds", "baths", "sqft", "year_built", "latitude", "longitude",
            "days_on_market",
        ):
            if getattr(self, attribute) in (None, 0) and getattr(other, attribute) not in (None, 0):
                setattr(self, attribute, getattr(other, attribute))

        # Keep the lower advertised rent -- that is the one a renter can act on.
        if other.price and (self.price is None or other.price < self.price):
            self.price = other.price

        seen_photos = set(self.photos)
        self.photos.extend(p for p in other.photos if p and p not in seen_photos)
        seen_amenities = {a.lower() for a in self.amenities}
        self.amenities.extend(a for a in other.amenities if a.lower() not in seen_amenities)

        self.raw.setdefault("merged_sources", [])
        self.raw["merged_sources"].append(
            {"source": other.source, "source_id": other.source_id, "url": other.url}
        )
        self.refresh_derived()

    # ----------------------------------------------------------- persistence
    def to_row(self) -> dict[str, Any]:
        return {
            "dedupe_key": self.dedupe_key,
            "unit_hash": self.unit_hash,
            "building_hash": self.building_hash,
            "geo_hash": self.geo_hash,
            "source": self.source,
            "source_id": self.source_id,
            "source_key": self.source_key,
            "url": self.url,
            "address": self.display_address,
            "raw_address": self.address,
            "unit": self.unit,
            "city": self.city,
            "state": self.state,
            "zip_code": self.zip_code,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "property_type": self.property_type,
            "beds": self.beds,
            "baths": self.baths,
            "sqft": self.sqft,
            "year_built": self.year_built,
            "price": self.price,
            "available_date": self.available_date,
            "days_on_market": self.days_on_market,
            "status": self.status,
            "title": self.title,
            "description": self.description,
            "amenities": json.dumps(self.amenities),
            "photos": json.dumps(self.photos),
            "broker": self.broker,
            "lease_min_months": self.lease_term.min_months if self.lease_term else None,
            "lease_max_months": self.lease_term.max_months if self.lease_term else None,
            "lease_term_text": self.lease_term.describe() if self.lease_term else "",
            "miles_from_ocean": self.miles_from_ocean,
            "is_oceanfront": int(self.is_oceanfront),
            "amenity_matches": json.dumps(self.amenity_matches),
            "enrichment": self.enrichment.to_json(),
            "fetched_at": _iso(self.fetched_at),
            "raw": json.dumps(self.raw, default=str)[:200000],
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Listing:
        listing = cls(
            source=row.get("source", ""),
            source_id=row.get("source_id", "") or "",
            url=row.get("url", "") or "",
            address=row.get("raw_address") or row.get("address", "") or "",
            unit=row.get("unit", "") or "",
            city=row.get("city", "") or "",
            state=row.get("state", "FL") or "FL",
            zip_code=row.get("zip_code", "") or "",
            latitude=row.get("latitude"),
            longitude=row.get("longitude"),
            property_type=row.get("property_type", "") or "",
            beds=row.get("beds"),
            baths=row.get("baths"),
            sqft=row.get("sqft"),
            year_built=row.get("year_built"),
            price=row.get("price"),
            available_date=row.get("available_date"),
            days_on_market=row.get("days_on_market"),
            status=row.get("status", "") or "",
            title=row.get("title", "") or "",
            description=row.get("description", "") or "",
            amenities=_load_json(row.get("amenities"), []),
            photos=_load_json(row.get("photos"), []),
            broker=row.get("broker", "") or "",
        )
        listing.amenity_matches = _load_json(row.get("amenity_matches"), {})
        listing.enrichment = Enrichment.from_json(row.get("enrichment"))
        listing.raw = _load_json(row.get("raw"), {})
        return listing

    def summary(self) -> str:
        bits = [
            self.display_address,
            f"${self.price:,}/mo" if self.price else "price n/a",
            f"{_fmt(self.beds)}bd/{_fmt(self.baths)}ba",
            f"{self.sqft:,} sqft" if self.sqft else "sqft n/a",
            self.lease_term.describe() if self.lease_term else "term n/a",
        ]
        return " | ".join(bits)


@dataclass
class MatchResult:
    """Outcome of running a listing through the hard-constraint filters."""

    listing: Listing
    passed: bool
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    score: float = 0.0

    @property
    def reason(self) -> str:
        if self.passed:
            return "; ".join(self.warnings) if self.warnings else "all constraints met"
        return "; ".join(self.failures)


def _load_json(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        loaded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default
    return loaded if isinstance(loaded, type(default)) else default


def _fmt(value: float | None) -> str:
    if value is None:
        return "?"
    return str(int(value)) if float(value).is_integer() else str(value)
