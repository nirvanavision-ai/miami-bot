"""Address normalization is the foundation of cross-portal dedupe, so these
tests assert on identity collapse rather than on intermediate fields."""

from __future__ import annotations

import pytest

from miami_bot.normalize import (
    building_key,
    geo_key,
    normalize_city,
    normalize_unit,
    normalize_zip,
    parse_address,
    unit_key,
)

# Every spelling of one Bal Harbour unit seen across the portals.
SAME_UNIT = [
    "9705 Collins Avenue #1502N, Bal Harbour, FL 33154",
    "9705 Collins Ave APT 1502-N, Bal Harbour, FL 33154-2932",
    "9705 COLLINS AVE UNIT 1502 N, Bal Harbour FL 33154",
    "9705 Collins Ave Bal Harbour FL 33154",          # unit supplied separately
    "9705 collins avenue apartment 1502 n, bal harbor, florida 33154",
]


def test_all_spellings_of_one_unit_collapse_to_one_key():
    keys = set()
    for index, address in enumerate(SAME_UNIT):
        unit = "Apt 1502N" if index == 3 else None
        keys.add(unit_key(parse_address(address, unit=unit)))
    assert len(keys) == 1, f"expected one identity, got {keys}"


def test_different_units_in_one_tower_stay_distinct():
    a = parse_address("9705 Collins Ave #1502N, Bal Harbour, FL 33154")
    b = parse_address("9705 Collins Ave #1802N, Bal Harbour, FL 33154")
    assert unit_key(a) != unit_key(b)
    assert building_key(a) == building_key(b)


def test_penthouse_is_not_the_same_as_the_bare_number():
    penthouse = parse_address("9705 Collins Ave PH-3, Bal Harbour, FL 33154")
    unit_three = parse_address("9705 Collins Ave #3, Bal Harbour, FL 33154")
    assert penthouse.unit == "ph3"
    assert unit_key(penthouse) != unit_key(unit_three)


@pytest.mark.parametrize(
    "address,expected_unit",
    [
        ("9705 Collins Ave TH2, Bal Harbour, FL 33154", "th2"),
        ("9705 Collins Ave TH-2, Bal Harbour, FL 33154", "th2"),
        ("9111 Collins Ave N-501, Surfside, FL 33154", "n501"),
        ("1000 South Pointe Dr Unit 1502, Miami Beach, FL 33139", "1502"),
        ("100 Lincoln Rd #1001, Miami Beach, FL 33139", "1001"),
    ],
)
def test_unit_extraction(address, expected_unit):
    assert parse_address(address).unit == expected_unit


@pytest.mark.parametrize(
    "address",
    ["1000 5th St, Miami Beach, FL 33139", "801 71st St, Miami Beach, FL 33141"],
)
def test_numbered_streets_do_not_lose_their_last_token(address):
    parts = parse_address(address)
    assert parts.unit == ""
    assert parts.suffix == "st"


def test_five_digit_street_number_is_not_read_as_a_zip():
    parts = parse_address("18201 Collins Ave, Sunny Isles Beach, FL")
    assert parts.number == "18201"
    assert parts.zip5 == ""


def test_zip_plus_four_normalizes_to_five():
    assert normalize_zip("33154-2932") == "33154"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("bal harbor", "Bal Harbour"),
        ("Sunny Isles", "Sunny Isles Beach"),
        ("SOUTH BEACH", "Miami Beach"),
        ("Surfside", "Surfside"),
    ],
)
def test_city_aliases(raw, expected):
    assert normalize_city(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [("#1502N", "1502n"), ("Apt 1502-N", "1502n"), ("Unit 1502 N", "1502n"),
     ("PH3", "ph3"), ("", ""), ("N/A", "")],
)
def test_normalize_unit(raw, expected):
    assert normalize_unit(raw) == expected


def test_unusable_address_produces_no_key():
    parts = parse_address("Address withheld")
    assert not parts.is_usable
    assert unit_key(parts) == ""


def test_geo_key_tolerates_float_noise_between_portals():
    a = geo_key(25.889012, -80.123301, "1502N")
    b = geo_key(25.889034, -80.123289, "#1502-n")
    assert a == b and a != ""


def test_geo_key_separates_neighbouring_towers():
    assert geo_key(25.8890, -80.1233, "1502") != geo_key(25.8940, -80.1225, "1502")
