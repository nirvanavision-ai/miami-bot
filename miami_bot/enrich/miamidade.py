"""Miami-Dade County Property Appraiser / GIS lookups.

This is the authoritative answer to "is it really 1,200+ sq ft, and when was the
building actually built?". Portal listings inflate square footage routinely (by
quoting the *total* area including balconies, or by copying the floor plan's
marketing number), and year-built is frequently blank or wrong on rentals.

Two public, credential-free paths are used:

1. **PA public service proxy** -- the same JSON service the county's own
   Property Search site calls. Address search returns folio numbers; the folio
   lookup returns the assessed building facts.
2. **ArcGIS parcel layer** -- queried when the PA proxy is unavailable or the
   address search returns nothing.

Both are best-effort. A county lookup that fails leaves the listing flagged as
"unverified" rather than dropping it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import CountySettings
from ..models import Listing
from ..util.http import HttpClient, HttpError
from ..util.logging import get_logger
from ..util.text import parse_sqft, parse_year_built

log = get_logger(__name__)


@dataclass
class CountyRecord:
    """Assessed facts for one folio."""

    folio: str = ""
    site_address: str = ""
    unit: str = ""
    sqft: int | None = None
    year_built: int | None = None
    beds: float | None = None
    baths: float | None = None
    use_code: str = ""
    source: str = ""

    @property
    def has_specs(self) -> bool:
        return self.sqft is not None or self.year_built is not None


class MiamiDadeClient:
    """Public-record verification for Miami-Dade parcels."""

    #: The PA proxy is a shared public service; keep the page size modest.
    search_page_size = 50

    def __init__(self, http: HttpClient, settings: CountySettings) -> None:
        self.http = http
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return self.settings.enabled

    # ------------------------------------------------------------------ API
    def lookup(self, listing: Listing) -> CountyRecord | None:
        """Best-effort county record for a listing's unit."""
        parts = listing.address_parts
        if parts is None or not parts.is_usable:
            return None

        street = parts.street_line.title()
        try:
            record = self._lookup_via_pa_proxy(street, parts.unit)
            if record is not None:
                return record
        except HttpError as exc:
            log.info("miamidade: PA proxy unavailable (%s); falling back to GIS", exc)

        try:
            return self._lookup_via_gis(street, parts.unit)
        except HttpError as exc:
            log.info("miamidade: GIS lookup failed for %s (%s)", street, exc)
            return None

    # -------------------------------------------------------------- PA proxy
    def _lookup_via_pa_proxy(self, street: str, unit: str) -> CountyRecord | None:
        params = {
            "Operation": "GetPropertySearchByAddress",
            "clientAppName": "PropertySearch",
            "myAddress": street,
            "myUnit": unit.upper(),
            "from": "0",
            "to": str(self.search_page_size),
        }
        payload = self.http.get_json(self.settings.pa_proxy_url, params=params)
        matches = _as_list(
            _pick(payload, "MinimumPropertyInfos", "PropertyInfos", "Results", "results")
        )
        if not matches:
            return None

        chosen = _choose_match(matches, unit)
        folio = str(
            _pick(chosen, "Folio", "folio", "FolioNumber", "strapNumber") or ""
        ).replace("-", "")
        if not folio:
            return None

        detail = self._folio_detail(folio)
        record = CountyRecord(
            folio=folio,
            site_address=str(_pick(chosen, "SiteAddress", "Address", "siteAddress") or ""),
            unit=str(_pick(chosen, "Unit", "unit", "UnitNo") or ""),
            source="miamidade_pa_proxy",
        )
        if detail:
            _apply_detail(record, detail)
        return record

    def _folio_detail(self, folio: str) -> dict[str, Any] | None:
        params = {
            "Operation": "GetPropertySearchByFolio",
            "clientAppName": "PropertySearch",
            "folioNumber": folio,
        }
        try:
            payload = self.http.get_json(self.settings.pa_proxy_url, params=params)
        except HttpError as exc:
            log.debug("miamidade: folio detail failed for %s: %s", folio, exc)
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    # ------------------------------------------------------------------ GIS
    def _lookup_via_gis(self, street: str, unit: str) -> CountyRecord | None:
        """ArcGIS parcel query as a second source for the same facts."""
        # Escape single quotes for the SQL-ish `where` clause.
        safe_street = street.replace("'", "''").upper()
        where = f"UPPER(TRUE_SITE_ADDR) LIKE '%{safe_street}%'"
        params = {
            "where": where,
            "outFields": "*",
            "returnGeometry": "false",
            "resultRecordCount": self.search_page_size,
            "f": "json",
        }
        payload = self.http.get_json(self.settings.gis_url, params=params)
        features = _as_list(payload.get("features") if isinstance(payload, dict) else None)
        if not features:
            return None

        attributes = [
            feature.get("attributes", {}) for feature in features if isinstance(feature, dict)
        ]
        attributes = [a for a in attributes if isinstance(a, dict) and a]
        if not attributes:
            return None

        chosen = _choose_match(attributes, unit)
        record = CountyRecord(
            folio=str(_pick(chosen, "FOLIO", "Folio", "PARCELNO") or ""),
            site_address=str(_pick(chosen, "TRUE_SITE_ADDR", "SITE_ADDR", "ADDRESS") or ""),
            unit=str(_pick(chosen, "TRUE_SITE_UNIT", "UNIT", "SITE_UNIT") or ""),
            sqft=parse_sqft(
                _pick(chosen, "BLDG_EFFECTIVE_AREA", "FLOOR_AREA", "LIVING_AREA", "SQFT")
            ),
            year_built=parse_year_built(_pick(chosen, "YEAR_BUILT", "ACT_YR_BLT", "EFF_YR_BLT")),
            beds=_as_float(_pick(chosen, "BEDROOM_COUNT", "BEDROOMS")),
            baths=_as_float(_pick(chosen, "BATHROOM_COUNT", "BATHROOMS")),
            use_code=str(_pick(chosen, "DOR_CODE", "USE_CODE", "LAND_USE") or ""),
            source="miamidade_gis",
        )
        return record if record.folio or record.has_specs else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _apply_detail(record: CountyRecord, detail: dict[str, Any]) -> None:
    """Copy assessed facts out of a folio-detail payload.

    The PA service nests these under ``PropertyInfo`` and spells living area
    several different ways depending on the property class, so every known
    spelling is tried in decreasing order of specificity.
    """
    info = detail.get("PropertyInfo") if isinstance(detail.get("PropertyInfo"), dict) else detail

    record.sqft = parse_sqft(
        _pick(
            info,
            "BuildingHeatedArea",      # closest to "living area" for condos
            "AdjustedSquareFeet",
            "BuildingEffectiveArea",
            "BuildingGrossArea",
            "FloorArea",
        )
    )
    record.year_built = parse_year_built(
        _pick(info, "YearBuilt", "ActualYearBuilt", "EffectiveYearBuilt")
    )
    record.beds = _as_float(_pick(info, "BedroomCount", "Bedrooms"))
    record.baths = _as_float(_pick(info, "BathroomCount", "Bathrooms", "FullBathroomCount"))
    record.use_code = str(_pick(info, "DORDescription", "DORCode", "UseCode") or "")
    if not record.site_address:
        record.site_address = str(_pick(info, "SiteAddress", "Address") or "")
    if not record.unit:
        record.unit = str(_pick(info, "UnitNo", "Unit") or "")


def _choose_match(candidates: list[dict[str, Any]], unit: str) -> dict[str, Any]:
    """Prefer the record whose unit matches; otherwise take the first.

    A Collins Ave address search returns every unit in the tower, so picking
    blindly would attribute a studio's square footage to a 2-bedroom.
    """
    if not unit:
        return candidates[0]

    from ..normalize import normalize_unit

    wanted = normalize_unit(unit)
    for candidate in candidates:
        raw_unit = _pick(candidate, "Unit", "unit", "UnitNo", "TRUE_SITE_UNIT", "SITE_UNIT")
        if raw_unit and normalize_unit(raw_unit) == wanted:
            return candidate
    # Some records fold the unit into the address string.
    for candidate in candidates:
        address = str(_pick(candidate, "SiteAddress", "TRUE_SITE_ADDR", "Address") or "")
        if wanted and wanted in normalize_unit(address.split()[-1] if address.split() else ""):
            return candidate
    return candidates[0]


def _pick(payload: Any, *keys: str) -> Any:
    if not isinstance(payload, dict):
        return None
    for key in keys:
        if key in payload and payload[key] not in (None, "", []):
            return payload[key]
    # Case-insensitive second pass -- the PA service is inconsistent about casing.
    lowered = {str(k).lower(): v for k, v in payload.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value not in (None, "", []):
            return value
    return None


def _as_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
