"""Hard-constraint evaluation.

Every criterion from the brief is enforced here, in one place, against the
normalized :class:`~miami_bot.models.Listing`. The evaluator returns *why* a
listing failed rather than a bare boolean, because during tuning the reasons are
the whole point: a search returning nothing is a filter bug until proven
otherwise.

Distinction between failures and warnings:

* **failure** -- a constraint the listing definitively violates. Disqualifying.
* **warning** -- a constraint that cannot be evaluated because the data is
  missing (no sqft, no lease term, no coordinates). The listing survives, is
  flagged, and Module 3 gets a chance to fill the gap from county records.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import SearchCriteria
from .geo import in_coastal_bbox
from .models import Listing, MatchResult
from .util.logging import get_logger
from .util.text import match_amenities, normalize_for_match, scan_keywords

log = get_logger(__name__)

# Property types that are never a condo, regardless of what the copy says.
_DISQUALIFYING_TYPES = (
    "single family", "single_family", "singlefamily", "townhouse", "town house",
    "multi family", "multi_family", "duplex", "triplex", "lot", "land",
    "manufactured", "mobile", "commercial", "office", "retail", "warehouse",
)


@dataclass
class FilterStats:
    """Aggregate rejection reasons for a run -- printed in the run summary."""

    evaluated: int = 0
    passed: int = 0
    by_reason: dict[str, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.by_reason is None:
            self.by_reason = {}

    def record(self, result: MatchResult) -> None:
        self.evaluated += 1
        if result.passed:
            self.passed += 1
            return
        for failure in result.failures:
            key = failure.split(":")[0].strip()
            self.by_reason[key] = self.by_reason.get(key, 0) + 1

    def top_reasons(self, limit: int = 8) -> list[tuple[str, int]]:
        return sorted(self.by_reason.items(), key=lambda kv: kv[1], reverse=True)[:limit]


def evaluate(listing: Listing, criteria: SearchCriteria) -> MatchResult:
    """Run every hard constraint. Order is cheapest-and-most-decisive first."""
    failures: list[str] = []
    warnings: list[str] = []

    _check_location(listing, criteria, failures, warnings)
    _check_property_type(listing, criteria, failures, warnings)
    _check_size(listing, criteria, failures, warnings)
    _check_price(listing, criteria, failures, warnings)
    _check_lease_terms(listing, criteria, failures, warnings)
    _check_building(listing, criteria, failures, warnings)

    result = MatchResult(
        listing=listing,
        passed=not failures,
        failures=failures,
        warnings=warnings,
    )
    result.score = score(listing, criteria) if result.passed else 0.0
    return result


# ---------------------------------------------------------------------------
# Individual constraints
# ---------------------------------------------------------------------------
def _check_location(
    listing: Listing, criteria: SearchCriteria, failures: list[str], warnings: list[str]
) -> None:
    if criteria.state and listing.state and listing.state.upper() != criteria.state.upper():
        failures.append(f"location: state {listing.state} is outside {criteria.state}")
        return

    if criteria.enforce_city_whitelist:
        city = (listing.city or "").strip().lower()
        if not city:
            warnings.append("location: city missing from source data")
        elif city not in criteria.normalized_cities:
            # A correct ZIP rescues a mislabelled city (portals put "Miami" on
            # Miami Beach listings constantly).
            if listing.zip_code and listing.zip_code in criteria.zip_codes:
                warnings.append(
                    f"location: city '{listing.city}' not whitelisted but ZIP {listing.zip_code} is"
                )
            else:
                failures.append(f"location: city '{listing.city}' is not a monitored municipality")
                return

    if listing.miles_from_ocean is None:
        if criteria.require_coordinates:
            failures.append("location: no coordinates, cannot verify ocean proximity")
        else:
            warnings.append("location: ocean distance unverified (no coordinates)")
        return

    if not in_coastal_bbox(listing.latitude, listing.longitude):
        failures.append("location: coordinates fall outside the coastal corridor")
        return

    if listing.miles_from_ocean > criteria.max_miles_from_ocean:
        failures.append(
            f"location: {listing.miles_from_ocean:.2f} mi from ocean "
            f"(max {criteria.max_miles_from_ocean})"
        )


def _check_property_type(
    listing: Listing, criteria: SearchCriteria, failures: list[str], warnings: list[str]
) -> None:
    declared = normalize_for_match(listing.property_type)

    if declared:
        for bad in _DISQUALIFYING_TYPES:
            if normalize_for_match(bad) in declared:
                failures.append(f"property_type: '{listing.property_type}' is not a condo")
                return
        if any(allowed in declared for allowed in criteria.normalized_property_types):
            return
        failures.append(f"property_type: '{listing.property_type}' is not an accepted type")
        return

    # No declared type: infer from the listing copy before giving up.
    blob = normalize_for_match(listing.searchable_terms())
    if any(allowed in blob for allowed in criteria.normalized_property_types):
        warnings.append("property_type: inferred from listing text")
        return
    warnings.append("property_type: not stated by source")


def _check_size(
    listing: Listing, criteria: SearchCriteria, failures: list[str], warnings: list[str]
) -> None:
    if listing.beds is None:
        warnings.append("size: bedroom count missing")
    elif listing.beds < criteria.min_beds:
        failures.append(f"size: {_fmt(listing.beds)} beds < {_fmt(criteria.min_beds)} required")

    if listing.baths is None:
        warnings.append("size: bathroom count missing")
    elif listing.baths < criteria.min_baths:
        failures.append(f"size: {_fmt(listing.baths)} baths < {_fmt(criteria.min_baths)} required")

    if listing.sqft is None:
        if criteria.require_known_sqft:
            failures.append("size: square footage unknown")
        else:
            warnings.append("size: square footage unverified (pending county lookup)")
    elif listing.sqft < criteria.min_sqft:
        failures.append(f"size: {listing.sqft:,} sqft < {criteria.min_sqft:,} required")


def _check_price(
    listing: Listing, criteria: SearchCriteria, failures: list[str], warnings: list[str]
) -> None:
    if listing.price is None:
        failures.append("price: no rent published")
        return
    if listing.price < criteria.min_price:
        failures.append(f"price: ${listing.price:,} below ${criteria.min_price:,} floor")
    elif listing.price > criteria.max_price:
        failures.append(f"price: ${listing.price:,} above ${criteria.max_price:,} ceiling")


def _check_lease_terms(
    listing: Listing, criteria: SearchCriteria, failures: list[str], warnings: list[str]
) -> None:
    blob = listing.searchable_terms()

    # 1. Disqualifying keywords, ignoring negated mentions ("NO short term").
    scan = scan_keywords(blob, criteria.excluded_keywords)
    if scan.disqualified:
        failures.append(f"lease_terms: excluded keyword(s) {', '.join(sorted(scan.hits))}")
        return
    if scan.negated:
        warnings.append(
            f"lease_terms: '{scan.negated[0]}' present but negated in the copy"
        )

    term = listing.lease_term
    if term is None or not term.known:
        # A positive phrase ("annual lease") rescues an unparseable term.
        rescued = [
            keyword for keyword in criteria.positive_term_keywords
            if normalize_for_match(keyword) in normalize_for_match(blob)
        ]
        if rescued:
            warnings.append(f"lease_terms: term inferred from '{rescued[0]}'")
            return
        if criteria.require_explicit_lease_term:
            failures.append("lease_terms: no lease term stated")
        else:
            warnings.append("lease_terms: not stated -- confirm 6-12 months with the agent")
        return

    minimum, maximum = term.min_months, term.max_months

    if minimum is not None and minimum < criteria.min_lease_months:
        failures.append(
            f"lease_terms: accepts terms as short as {minimum} months "
            f"(minimum {criteria.min_lease_months} required)"
        )
        return
    if minimum is not None and minimum > criteria.max_lease_months:
        failures.append(
            f"lease_terms: requires at least {minimum} months "
            f"(maximum {criteria.max_lease_months} acceptable)"
        )
        return
    if maximum is not None and maximum < criteria.min_lease_months:
        failures.append(
            f"lease_terms: caps out at {maximum} months "
            f"(minimum {criteria.min_lease_months} required)"
        )
        return
    if maximum is not None and maximum > criteria.max_lease_months and minimum is None:
        warnings.append(f"lease_terms: advertised up to {maximum} months; confirm a 6-12 term")


def _check_building(
    listing: Listing, criteria: SearchCriteria, failures: list[str], warnings: list[str]
) -> None:
    building = criteria.building
    blob = listing.searchable_terms()

    listing.amenity_matches = match_amenities(blob, building.luxury_amenities)
    amenity_count = len(listing.amenity_matches)

    renovated = bool(
        [k for k in building.renovation_keywords if normalize_for_match(k) in normalize_for_match(blob)]
    )

    if listing.year_built is None:
        warnings.append("building: year built unknown (pending county lookup)")
    elif listing.year_built < building.min_year_built:
        if building.allow_older_if_renovated and renovated:
            warnings.append(
                f"building: built {listing.year_built} but described as renovated"
            )
        else:
            failures.append(
                f"building: built {listing.year_built} < {building.min_year_built} "
                "and no renovation stated"
            )
            return

    missing = [
        name for name in building.required_amenities
        if name not in listing.amenity_matches
    ]

    # A structured feed with no description cannot evidence any amenity. Failing
    # on that says the feed is thin, not that the building lacks a pool -- so it
    # is flagged for confirmation instead, exactly as unknown sqft is.
    if not listing.has_descriptive_text and not building.require_amenity_evidence:
        if missing or amenity_count < building.min_amenity_matches:
            wanted = ", ".join(
                name.replace("_", " ") for name in (missing or ["luxury amenities"])
            )
            warnings.append(
                f"building: source published no description -- {wanted} unverified"
            )
        return

    if missing:
        failures.append(
            f"building: missing required amenity/amenities "
            f"{', '.join(name.replace('_', ' ') for name in missing)}"
        )
        return

    if amenity_count < building.min_amenity_matches:
        failures.append(
            f"building: only {amenity_count} luxury amenities detected "
            f"({building.min_amenity_matches} required)"
        )


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------
def score(listing: Listing, criteria: SearchCriteria) -> float:
    """0-100 desirability score used to rank alerts when a run is capped.

    Purely a presentation aid -- it never changes pass/fail.
    """
    points = 0.0

    # Ocean proximity (30) -- the defining attribute of the search.
    if listing.miles_from_ocean is not None:
        ratio = min(listing.miles_from_ocean / max(criteria.max_miles_from_ocean, 1e-6), 1.0)
        points += 30.0 * (1.0 - ratio)
    else:
        points += 10.0

    # Value within budget (20) -- cheaper inside the band scores higher.
    if listing.price is not None:
        span = max(criteria.max_price - criteria.min_price, 1)
        position = (listing.price - criteria.min_price) / span
        points += 20.0 * (1.0 - min(max(position, 0.0), 1.0))

    # Space above the floor (20).
    if listing.sqft:
        excess = (listing.sqft - criteria.min_sqft) / max(criteria.min_sqft, 1)
        points += 20.0 * min(max(excess, 0.0), 1.0)

    # Amenity depth (15).
    required = max(criteria.building.min_amenity_matches, 1)
    points += 15.0 * min(len(listing.amenity_matches) / (required * 2), 1.0)

    # Building age (10).
    if listing.year_built:
        if listing.year_built >= 2015:
            points += 10.0
        elif listing.year_built >= criteria.building.min_year_built:
            points += 6.0
        else:
            points += 2.0

    # AVM verdict (5) -- set only after Module 3 has run.
    ratio = listing.enrichment.price_to_estimate_ratio
    if ratio is not None:
        points += 5.0 if ratio <= 1.0 else max(0.0, 5.0 - (ratio - 1.0) * 25.0)

    return round(min(points, 100.0), 1)


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)
