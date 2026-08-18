"""The internal schema: derived fields, persistence round-trip and merging."""

from __future__ import annotations

from miami_bot.models import Enrichment, Listing
from tests.conftest import make_listing


def test_derived_fields_are_computed_on_construction(listing):
    assert listing.unit_hash and listing.building_hash
    assert listing.city == "Bal Harbour" and listing.zip_code == "33154"
    assert listing.unit == "1502n"
    assert listing.lease_term.min_months == 6
    assert listing.miles_from_ocean is not None
    assert listing.price_per_sqft == 6.55


def test_dedupe_key_falls_back_through_the_identity_strategies():
    by_address = make_listing()
    assert by_address.dedupe_key.startswith("u:")

    by_geo = make_listing(address="Address withheld", latitude=25.889, longitude=-80.1233)
    assert by_geo.dedupe_key.startswith("g:")

    by_source = make_listing(address="", latitude=None, longitude=None,
                             source="zillow", source_id="z1")
    assert by_source.dedupe_key == "s:zillow:z1"


def test_row_round_trip_preserves_identity_and_specs(listing):
    restored = Listing.from_row(listing.to_row())
    assert restored.dedupe_key == listing.dedupe_key
    assert restored.sqft == listing.sqft
    assert restored.photos == listing.photos
    assert restored.lease_term.describe() == listing.lease_term.describe()


def test_enrichment_survives_the_round_trip(listing):
    listing.enrichment.rent_estimate = 8900
    listing.enrichment.verdict = "at market"
    restored = Listing.from_row(listing.to_row())
    assert restored.enrichment.rent_estimate == 8900
    assert restored.enrichment.verdict == "at market"


def test_enrichment_from_corrupt_json_degrades_quietly():
    assert Enrichment.from_json("{not json").rent_estimate is None
    assert Enrichment.from_json(None).rent_estimate is None


def test_merging_takes_the_lower_rent_and_unions_media():
    primary = make_listing(price=9500, year_built=None, photos=["a.jpg"])
    secondary = make_listing(price=9200, year_built=2018, photos=["b.jpg"],
                             amenities=["Rooftop deck"])
    primary.merge_from(secondary)
    assert primary.price == 9200
    assert primary.year_built == 2018
    assert primary.photos == ["a.jpg", "b.jpg"]
    assert "Rooftop deck" in primary.amenities
    assert primary.raw["merged_sources"]


def test_merging_never_overwrites_a_populated_field_with_a_blank():
    primary = make_listing(sqft=1450, broker="Elliman")
    secondary = make_listing(sqft=None, broker="")
    primary.merge_from(secondary)
    assert primary.sqft == 1450 and primary.broker == "Elliman"


def test_searchable_terms_covers_every_text_field():
    listing = make_listing(title="Penthouse", description="Annual lease",
                           amenities=["Valet"], status="Active")
    blob = listing.searchable_terms()
    for fragment in ("Penthouse", "Annual lease", "Valet", "Active"):
        assert fragment in blob
