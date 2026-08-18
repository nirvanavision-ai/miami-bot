"""Persistence, cross-portal dedupe and change detection."""

from __future__ import annotations

from datetime import timedelta

from miami_bot.models import utcnow
from tests.conftest import make_listing

ZILLOW = {"source": "rapidapi_zillow", "source_id": "z1", "url": "https://zillow/1"}
REDFIN = {"source": "rapidapi_redfin", "source_id": "r9", "url": "https://redfin/9"}


def test_a_first_sighting_is_new(db):
    change = db.upsert(make_listing(**ZILLOW), matched=True, score=60)
    assert change.is_new and change.new_price == 9500


def test_the_same_unit_from_another_portal_is_not_new(db):
    db.upsert(make_listing(**ZILLOW))
    change = db.upsert(
        make_listing(
            address="9705 Collins Avenue APT 1502-N, Bal Harbour, FL 33154-2932", **REDFIN
        )
    )
    assert not change.is_new
    assert db.stats()["listings_total"] == 1
    assert db.stats()["multi_portal_units"] == 1


def test_a_thin_sighting_does_not_erase_richer_data(db):
    key = db.upsert(make_listing(**ZILLOW)).dedupe_key      # sqft 1450, built 2018
    db.upsert(make_listing(sqft=None, year_built=None, **REDFIN))
    stored = db.get_listing(key)
    assert stored.sqft == 1450
    assert stored.year_built == 2018


def test_a_second_portal_fills_gaps_the_first_left(db):
    key = db.upsert(make_listing(sqft=None, year_built=None, **ZILLOW)).dedupe_key
    db.upsert(make_listing(sqft=1450, year_built=2019, **REDFIN))
    stored = db.get_listing(key)
    assert stored.sqft == 1450
    assert stored.year_built == 2019


def test_a_genuinely_different_unit_is_new(db):
    db.upsert(make_listing(**ZILLOW))
    change = db.upsert(
        make_listing(address="9705 Collins Ave #1802N, Bal Harbour, FL 33154",
                     source="rapidapi_zillow", source_id="z2")
    )
    assert change.is_new
    assert db.stats()["listings_total"] == 2


def test_price_drops_are_detected_and_logged(db):
    first = db.upsert(make_listing(price=9500, **ZILLOW))
    change = db.upsert(make_listing(price=8800, **ZILLOW))
    assert change.price_drop and not change.price_increase
    assert change.old_price == 9500 and change.new_price == 8800
    assert change.delta == -700
    assert round(change.drop_pct, 3) == 0.074
    history = db.price_history(first.dedupe_key)
    assert [(h["old_price"], h["new_price"]) for h in history] == [(None, 9500), (9500, 8800)]


def test_price_increases_are_recorded_but_flagged_separately(db):
    db.upsert(make_listing(price=9500, **ZILLOW))
    change = db.upsert(make_listing(price=10200, **ZILLOW))
    assert change.price_increase and not change.price_drop


def test_an_unchanged_listing_produces_no_change(db):
    db.upsert(make_listing(**ZILLOW))
    change = db.upsert(make_listing(**ZILLOW))
    assert not (change.is_new or change.price_drop or change.price_increase)


def test_matching_by_provider_id_survives_an_address_correction(db):
    db.upsert(make_listing(**ZILLOW))
    change = db.upsert(
        make_listing(address="9705 Collins Ave #1502-North, Bal Harbour, FL 33154", **ZILLOW)
    )
    assert not change.is_new


def test_alert_cooldown(db):
    listing = make_listing(**ZILLOW)
    key = db.upsert(listing).dedupe_key
    assert not db.was_alerted_recently(key, "new", 24)
    db.record_alert(key, "new", "discord", 9500, True)
    assert db.was_alerted_recently(key, "new", 24)
    assert not db.was_alerted_recently(key, "price_drop", 24)


def test_failed_alerts_do_not_start_the_cooldown(db):
    key = db.upsert(make_listing(**ZILLOW)).dedupe_key
    db.record_alert(key, "new", "discord", 9500, False, "webhook 500")
    assert not db.was_alerted_recently(key, "new", 24)


def test_stale_listings_are_marked_inactive(db):
    db.upsert(make_listing(**ZILLOW), matched=True)
    assert db.stats()["listings_active"] == 1
    assert db.mark_inactive_before(utcnow() + timedelta(hours=1)) == 1
    assert db.stats()["listings_active"] == 0


def test_matched_listings_come_back_ranked(db):
    db.upsert(make_listing(price=9500, **ZILLOW), matched=True, score=40)
    db.upsert(
        make_listing(address="9705 Collins Ave #1802N, Bal Harbour, FL 33154",
                     source="rapidapi_zillow", source_id="z2"),
        matched=True, score=90,
    )
    ranked = db.matched_listings()
    assert len(ranked) == 2
    assert ranked[0].unit == "1802n"


def test_runs_are_recorded(db):
    run_id = db.start_run()
    db.finish_run(run_id, fetched=10, matched=3, alerts_sent=2)
    assert db.stats()["runs"] == 1


def test_a_later_run_without_photos_does_not_wipe_them(db):
    """An empty list serializes to "[]", which SQL COALESCE would happily store."""
    key = db.upsert(make_listing(photos=["https://a.jpg"], **ZILLOW)).dedupe_key
    db.upsert(make_listing(photos=[], **REDFIN))
    assert db.get_listing(key).photos == ["https://a.jpg"]


def test_a_later_run_without_a_description_does_not_wipe_it(db):
    key = db.upsert(make_listing(**ZILLOW)).dedupe_key
    original = db.get_listing(key).description
    db.upsert(make_listing(description="", broker="", **REDFIN))
    stored = db.get_listing(key)
    assert stored.description == original


def test_an_unenriched_later_sighting_keeps_stored_avm_data(db):
    listing = make_listing(**ZILLOW)
    listing.enrichment.rent_estimate = 8900
    listing.enrichment.verdict = "at market"
    listing.enrichment.county_sqft = 1450
    key = db.upsert(listing, matched=True).dedupe_key

    db.upsert(make_listing(**REDFIN))          # no enrichment on this pass
    stored = db.get_listing(key)
    assert stored.enrichment.rent_estimate == 8900
    assert stored.enrichment.county_sqft == 1450


def test_fresh_enrichment_replaces_stale_enrichment(db):
    listing = make_listing(**ZILLOW)
    listing.enrichment.rent_estimate = 8900
    key = db.upsert(listing, matched=True).dedupe_key

    updated = make_listing(**ZILLOW)
    updated.enrichment.rent_estimate = 9300
    db.upsert(updated, matched=True)
    assert db.get_listing(key).enrichment.rent_estimate == 9300
