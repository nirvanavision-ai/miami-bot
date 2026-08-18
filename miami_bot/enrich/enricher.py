"""Module 3 orchestration: validate specs, then price the listing.

Order matters. County verification runs *first* because it can change square
footage, and square footage is both a hard constraint and an input to the rent
AVM. Pricing a listing on a marketing sqft number and then discovering the
county says it is 900 sq ft wastes an AVM call and produces a misleading
estimate.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import EnrichmentCriteria, Settings
from ..models import Listing
from ..util.http import HttpClient, HttpError
from ..util.logging import get_logger
from .housecanary import HouseCanaryClient
from .miamidade import MiamiDadeClient
from .rentcast import RentCastClient, RentEstimate

log = get_logger(__name__)

#: Portal vs county square footage disagreement beyond this fraction is flagged.
SQFT_CONFLICT_RATIO = 0.15
#: Year-built disagreement beyond this many years is flagged.
YEAR_CONFLICT_TOLERANCE = 2


@dataclass
class EnrichmentStats:
    attempted: int = 0
    county_hits: int = 0
    county_conflicts: int = 0
    sqft_filled: int = 0
    year_filled: int = 0
    rent_estimates: int = 0
    errors: int = 0

    def summary(self) -> str:
        return (
            f"enrichment: {self.attempted} listings, {self.county_hits} county hits "
            f"({self.sqft_filled} sqft filled, {self.year_filled} year filled, "
            f"{self.county_conflicts} conflicts), {self.rent_estimates} rent AVMs, "
            f"{self.errors} errors"
        )


class Enricher:
    """Runs the optional enrichment providers over a batch of listings."""

    def __init__(self, http: HttpClient, settings: Settings) -> None:
        self.settings = settings
        self.criteria: EnrichmentCriteria = settings.pipeline.enrichment
        self.rentcast = RentCastClient(http, settings.rentcast)
        self.housecanary = HouseCanaryClient(http, settings.housecanary)
        self.county = MiamiDadeClient(http, settings.county)
        self.stats = EnrichmentStats()

    @property
    def enabled(self) -> bool:
        return self.criteria.enabled and (
            self.rentcast.enabled or self.housecanary.enabled or self.county.enabled
        )

    # ------------------------------------------------------------------ API
    def enrich_all(self, listings: list[Listing]) -> None:
        if not self.enabled:
            log.info("enrichment disabled or unconfigured; skipping")
            return
        for listing in listings:
            self.enrich(listing)
        log.info("%s", self.stats.summary())

    def enrich(self, listing: Listing) -> None:
        """Enrich one listing in place. Never raises."""
        self.stats.attempted += 1
        enrichment = listing.enrichment

        if self.criteria.county_assessor and self.county.enabled:
            self._apply_county(listing)

        rent = self._rent_estimate(listing)
        if rent and rent.rent:
            enrichment.rent_estimate = rent.rent
            enrichment.rent_estimate_low = rent.low
            enrichment.rent_estimate_high = rent.high
            enrichment.rent_estimate_source = rent.source
            self.stats.rent_estimates += 1

            if listing.price:
                ratio = listing.price / rent.rent
                enrichment.price_to_estimate_ratio = round(ratio, 3)
                enrichment.verdict = self._verdict(ratio)

        value = self._value_estimate(listing)
        if value:
            enrichment.value_estimate = value
            enrichment.value_estimate_source = (
                enrichment.rent_estimate_source or "rentcast"
            )
            if listing.price:
                enrichment.gross_yield_pct = round((listing.price * 12) / value * 100, 2)

    # -------------------------------------------------------------- internals
    def _apply_county(self, listing: Listing) -> None:
        try:
            record = self.county.lookup(listing)
        except Exception as exc:  # county services are flaky; never fatal
            listing.enrichment.errors.append(f"county: {exc}")
            self.stats.errors += 1
            log.debug("county lookup failed for %s: %s", listing.display_address, exc)
            return

        if record is None or not record.has_specs:
            return

        self.stats.county_hits += 1
        enrichment = listing.enrichment
        enrichment.county_folio = record.folio
        enrichment.county_sqft = record.sqft
        enrichment.county_year_built = record.year_built
        enrichment.county_source = record.source

        if record.sqft:
            if listing.sqft is None:
                listing.sqft = record.sqft
                self.stats.sqft_filled += 1
            elif _differs(listing.sqft, record.sqft, SQFT_CONFLICT_RATIO):
                enrichment.county_conflict = True
                self.stats.county_conflicts += 1
                # The county record is the assessed truth; trust it over the ad.
                log.info(
                    "sqft conflict for %s: portal %s vs county %s -- using county",
                    listing.display_address, listing.sqft, record.sqft,
                )
                listing.sqft = record.sqft

        if record.year_built:
            if listing.year_built is None:
                listing.year_built = record.year_built
                self.stats.year_filled += 1
            elif abs(listing.year_built - record.year_built) > YEAR_CONFLICT_TOLERANCE:
                enrichment.county_conflict = True
                listing.year_built = record.year_built

        if listing.beds is None and record.beds:
            listing.beds = record.beds
        if listing.baths is None and record.baths:
            listing.baths = record.baths

    def _rent_estimate(self, listing: Listing) -> RentEstimate | None:
        if self.criteria.rentcast and self.rentcast.enabled:
            try:
                estimate = self.rentcast.rent_estimate(listing)
                if estimate and estimate.rent:
                    return estimate
            except HttpError as exc:
                listing.enrichment.errors.append(f"rentcast: {exc}")
                self.stats.errors += 1

        if self.criteria.housecanary and self.housecanary.enabled:
            try:
                estimate, _ = self.housecanary.estimates(listing)
                if estimate and estimate.rent:
                    return estimate
            except HttpError as exc:
                listing.enrichment.errors.append(f"housecanary: {exc}")
                self.stats.errors += 1
        return None

    def _value_estimate(self, listing: Listing) -> int | None:
        if self.criteria.rentcast and self.rentcast.enabled:
            try:
                value = self.rentcast.value_estimate(listing)
                if value:
                    return value
            except HttpError as exc:
                listing.enrichment.errors.append(f"rentcast value: {exc}")

        if self.criteria.housecanary and self.housecanary.enabled:
            try:
                _, value = self.housecanary.estimates(listing)
                return value
            except HttpError as exc:
                listing.enrichment.errors.append(f"housecanary value: {exc}")
        return None

    def _verdict(self, ratio: float) -> str:
        if ratio >= self.criteria.overpriced_threshold:
            return "above market"
        if ratio <= self.criteria.underpriced_threshold:
            return "below market"
        return "at market"


def _differs(portal_value: int, county_value: int, tolerance: float) -> bool:
    if not county_value:
        return False
    return abs(portal_value - county_value) / county_value > tolerance
