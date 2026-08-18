"""Build the human-readable payload once, render it per channel.

Every channel shows the same facts -- photo, price, specs, parsed lease terms,
ocean proximity, AVM comparison and the source URL -- so they are assembled here
and formatted downstream. That keeps Discord, Slack and email from drifting.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import Listing
from ..util.text import truncate


@dataclass
class AlertContent:
    """Channel-agnostic view of one alert."""

    headline: str
    subheadline: str
    url: str
    photo: str | None
    fields: list[tuple[str, str]] = field(default_factory=list)
    footer: str = ""
    description: str = ""
    accent: str = "new"          # "new" | "price_drop"
    score: float = 0.0

    @property
    def color(self) -> int:
        """Discord embed colour: teal for new, amber for a price drop."""
        return 0x0FB9A5 if self.accent == "new" else 0xF2A33C

    @property
    def emoji(self) -> str:
        return "🏝️" if self.accent == "new" else "📉"


def build_content(
    listing: Listing,
    *,
    kind: str = "new",
    old_price: int | None = None,
    warnings: list[str] | None = None,
    score: float = 0.0,
    extra_sources: list[str] | None = None,
) -> AlertContent:
    """Assemble everything a channel needs to render one listing."""
    price = f"${listing.price:,}/mo" if listing.price else "price on request"

    if kind == "price_drop" and old_price and listing.price:
        delta = old_price - listing.price
        pct = delta / old_price * 100
        headline = f"Price drop: {listing.display_address}"
        subheadline = f"${old_price:,} → ${listing.price:,} (−${delta:,}, −{pct:.1f}%)"
    else:
        headline = listing.display_address
        subheadline = f"{price} · {_specs(listing)}"

    fields: list[tuple[str, str]] = [
        ("Price", price),
        ("Layout", _specs(listing)),
        ("Lease term", listing.lease_term.describe() if listing.lease_term else "unspecified"),
        ("Ocean", _ocean(listing)),
    ]

    if listing.year_built:
        year = str(listing.year_built)
        if listing.enrichment.county_year_built == listing.year_built:
            year += " (county-verified)"
        fields.append(("Built", year))

    if listing.price_per_sqft:
        fields.append(("Price / sqft", f"${listing.price_per_sqft:,.2f}"))

    if listing.amenity_matches:
        amenities = ", ".join(sorted(listing.amenity_matches)[:8]).replace("_", " ")
        fields.append((f"Amenities ({len(listing.amenity_matches)})", amenities))

    enrichment = listing.enrichment
    if enrichment.rent_estimate:
        band = ""
        if enrichment.rent_estimate_low and enrichment.rent_estimate_high:
            band = f" (range ${enrichment.rent_estimate_low:,}–${enrichment.rent_estimate_high:,})"
        verdict = f" — {enrichment.verdict}" if enrichment.verdict else ""
        fields.append((
            f"Rent AVM · {enrichment.rent_estimate_source or 'avm'}",
            f"${enrichment.rent_estimate:,}{band}{verdict}",
        ))

    if enrichment.county_sqft:
        note = " ⚠️ conflicts with the listing" if enrichment.county_conflict else ""
        fields.append(("County record", f"{enrichment.county_sqft:,} sqft{note}"))

    if listing.broker:
        fields.append(("Listed by", listing.broker))
    if listing.available_date:
        fields.append(("Available", str(listing.available_date)))

    if extra_sources:
        fields.append(("Also listed on", ", ".join(sorted(set(extra_sources)))))

    if warnings:
        fields.append(("Needs confirming", "; ".join(warnings[:4])))

    footer_bits = [f"source: {listing.source}"]
    if score:
        footer_bits.append(f"score {score:.0f}/100")
    if listing.days_on_market is not None:
        footer_bits.append(f"{listing.days_on_market} days on market")

    return AlertContent(
        headline=headline,
        subheadline=subheadline,
        url=listing.url,
        photo=listing.primary_photo,
        fields=fields,
        footer=" · ".join(footer_bits),
        description=truncate(listing.description, 320),
        accent="price_drop" if kind == "price_drop" else "new",
        score=score,
    )


def _specs(listing: Listing) -> str:
    beds = _number(listing.beds)
    baths = _number(listing.baths)
    sqft = f"{listing.sqft:,} sqft" if listing.sqft else "sqft n/a"
    return f"{beds} bd · {baths} ba · {sqft}"


def _ocean(listing: Listing) -> str:
    if listing.miles_from_ocean is None:
        return "distance unverified"
    if listing.is_oceanfront:
        return "oceanfront (<0.1 mi)"
    return f"{listing.miles_from_ocean:.2f} mi to the beach"


def _number(value: float | None) -> str:
    if value is None:
        return "?"
    return str(int(value)) if float(value).is_integer() else f"{value:g}"
