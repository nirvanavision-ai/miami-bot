"""Lease-term parsing and keyword screening -- the logic that enforces the
'6 to 12 months, no short-term' constraint."""

from __future__ import annotations

import pytest

from miami_bot.util.text import (
    match_amenities,
    parse_baths,
    parse_beds,
    parse_lease_term,
    parse_price,
    parse_sqft,
    parse_year_built,
    scan_keywords,
    truncate,
)


@pytest.mark.parametrize(
    "text,min_months,max_months",
    [
        ("6-12 month lease available", 6, 12),
        ("6 to 12 months", 6, 12),
        ("Minimum 6 months, maximum 12 months", 6, 12),
        ("Annual lease only, unfurnished", 12, 12),
        ("1 year lease", 12, 12),
        ("Lease term: 8 months", 8, 8),
        ("min 6 mos", 6, None),
        ("up to 12 months", None, 12),
        ("Seasonal rental, 3 month minimum", 3, None),
        ("24 month minimum", 24, None),
    ],
)
def test_lease_term_extraction(text, min_months, max_months):
    term = parse_lease_term(text)
    assert term.min_months == min_months
    assert term.max_months == max_months


@pytest.mark.parametrize(
    "text,min_months,max_months",
    [
        ("Six to twelve month lease", 6, 12),
        ("minimum six months maximum twelve months", 6, 12),
        ("six month minimum", 6, None),
        ("Twelve month lease", 12, 12),
        ("one year lease", 12, 12),
        ("three month minimum", 3, None),
    ],
)
def test_spelled_out_numbers_are_understood(text, min_months, max_months):
    """Listing copy writes terms in words at least as often as in digits."""
    term = parse_lease_term(text)
    assert (term.min_months, term.max_months) == (min_months, max_months)


def test_lease_term_unknown_when_nothing_is_stated():
    assert not parse_lease_term("Beautiful oceanfront condo with balcony").known


def test_lease_term_reads_across_multiple_fields():
    term = parse_lease_term("Oceanfront 2BR", None, "Owner requires a 12 month lease")
    assert term.min_months == 12


def test_lease_term_evidence_is_recorded():
    term = parse_lease_term("6-12 month lease")
    assert term.evidence and "6-12 month" in term.evidence[0]


def test_range_wins_over_a_bare_number():
    # "12 months" also appears; the range must take precedence.
    term = parse_lease_term("Flexible 6-12 months, most owners prefer 12 months")
    assert (term.min_months, term.max_months) == (6, 12)


# --- keyword screening -----------------------------------------------------
EXCLUDED = ["short term", "seasonal", "vacation", "airbnb", "daily", "weekly",
            "month to month"]


@pytest.mark.parametrize(
    "text",
    [
        "Beautiful oceanfront condo, short term rentals welcome",
        "Available for daily, weekly or monthly stays",
        "Great vacation getaway, book weekly",
        "Shortterm ok",                     # no separator
        "Short-term lease considered",      # hyphenated
    ],
)
def test_disqualifying_keywords_are_caught(text):
    assert scan_keywords(text, EXCLUDED).disqualified


@pytest.mark.parametrize(
    "text",
    [
        "NO short term rentals. Annual lease only.",
        "Seasonal rentals not permitted; 12 month minimum.",
        "Owner does not allow Airbnb or short-term",
        "Strictly no month to month",
        "Annual unfurnished lease, no seasonal or vacation use",
    ],
)
def test_negated_keywords_do_not_disqualify(text):
    scan = scan_keywords(text, EXCLUDED)
    assert not scan.disqualified
    assert scan.negated


def test_keywords_do_not_match_inside_longer_words():
    assert not scan_keywords("Visit dailymotion for a tour", ["daily"]).disqualified


# --- spec parsing ----------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected", [("2 Beds", 2.0), ("2+Den", 2.0), ("Studio", 0.0), (3, 3.0), (None, None)]
)
def test_parse_beds(raw, expected):
    assert parse_beds(raw) == expected


@pytest.mark.parametrize(
    "raw,expected", [("2.5 Baths", 2.5), ("2 full 1 half", 2.5), (2, 2.0), (None, None)]
)
def test_parse_baths(raw, expected):
    assert parse_baths(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [("1,450 sq ft", 1450), ("1450", 1450), (1450.0, 1450), ("12", None), ("99999", None)],
)
def test_parse_sqft_rejects_implausible_values(raw, expected):
    assert parse_sqft(raw) == expected


@pytest.mark.parametrize(
    "raw,expected", [("$8,500/mo", 8500), (9500, 9500), ("$12,000 per month", 12000), ("", None)]
)
def test_parse_price(raw, expected):
    assert parse_price(raw) == expected


@pytest.mark.parametrize("raw,expected", [("Built in 2018", 2018), (2018, 2018), ("n/a", None)])
def test_parse_year_built(raw, expected):
    assert parse_year_built(raw) == expected


def test_amenity_matching_groups_by_category():
    found = match_amenities(
        "Valet parking, 24-hour concierge, rooftop pool and a spa",
        {"valet": ["valet"], "concierge": ["concierge"], "pool": ["pool"],
         "spa": ["spa"], "tennis": ["tennis court"]},
    )
    assert set(found) == {"valet", "concierge", "pool", "spa"}


def test_truncate_adds_an_ellipsis():
    assert truncate("x" * 50, 20).endswith("…")
