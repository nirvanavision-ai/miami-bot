"""Address normalization and unit hashing -- the basis for cross-portal dedupe.

The same oceanfront unit shows up on Zillow as::

    "9705 Collins Avenue #1502N, Bal Harbour, FL 33154"

on Redfin as::

    "9705 Collins Ave APT 1502-N, Bal Harbour, FL 33154-2932"

and from a scrape as::

    "9705 COLLINS AVE UNIT 1502 N, Bal Harbour FL"

All three must collapse to one identity. We do that by decomposing the address
into canonical components, then hashing (street + unit + zip5). A secondary
``building_key`` (street + zip, no unit) lets us group units in one tower, and a
geo key handles listings whose street text is unusable but which carry lat/lon.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .util.text import clean_text

# --- USPS Publication 28 style abbreviations -------------------------------
_STREET_SUFFIXES = {
    "avenue": "ave", "av": "ave", "ave": "ave", "aven": "ave", "avenu": "ave",
    "boulevard": "blvd", "blvd": "blvd", "boul": "blvd", "boulv": "blvd",
    "causeway": "cswy", "cswy": "cswy",
    "circle": "cir", "cir": "cir", "circl": "cir",
    "court": "ct", "ct": "ct",
    "drive": "dr", "dr": "dr", "driv": "dr", "drv": "dr",
    "expressway": "expy", "expy": "expy",
    "highway": "hwy", "hwy": "hwy", "hiway": "hwy",
    "island": "is", "is": "is",
    "lane": "ln", "ln": "ln",
    "parkway": "pkwy", "pkwy": "pkwy", "pky": "pkwy",
    "place": "pl", "pl": "pl",
    "plaza": "plz", "plz": "plz",
    "point": "pt", "pt": "pt",
    "road": "rd", "rd": "rd",
    "square": "sq", "sq": "sq",
    "street": "st", "st": "st", "str": "st", "strt": "st",
    "terrace": "ter", "ter": "ter", "terr": "ter",
    "trail": "trl", "trl": "trl",
    "walk": "walk",
    "way": "way", "wy": "way",
}

_DIRECTIONALS = {
    "north": "n", "n": "n", "south": "s", "s": "s",
    "east": "e", "e": "e", "west": "w", "w": "w",
    "northeast": "ne", "ne": "ne", "northwest": "nw", "nw": "nw",
    "southeast": "se", "se": "se", "southwest": "sw", "sw": "sw",
}

# Tokens that introduce a unit designator inside a street line.
_UNIT_MARKERS = (
    "apt", "apartment", "unit", "ste", "suite", "#", "no", "num", "number",
    "ph", "penthouse", "th", "townhouse", "lot", "res", "residence", "villa",
)

# Markers that are pure noise and get stripped from the unit value. "ph"/"th"
# are deliberately absent: "PH-3" and "TH2" are unit labels, not prefixes, and
# stripping them would collapse penthouse 3 into unit 3.
_UNIT_NOISE_MARKERS = (
    "apartment", "apt", "unit", "suite", "ste", "number", "num", "no", "residence", "res",
)

_ORDINAL_WORDS = {
    "first": "1st", "second": "2nd", "third": "3rd", "fourth": "4th",
    "fifth": "5th", "sixth": "6th", "seventh": "7th", "eighth": "8th",
    "ninth": "9th", "tenth": "10th",
}

# Municipalities we monitor, plus common aliases seen in portal payloads.
CITY_ALIASES = {
    "miami beach": "Miami Beach",
    "miamibeach": "Miami Beach",
    "south beach": "Miami Beach",
    "mid beach": "Miami Beach",
    "north beach": "Miami Beach",
    "surfside": "Surfside",
    "bal harbour": "Bal Harbour",
    "bal harbor": "Bal Harbour",
    "balharbour": "Bal Harbour",
    "bay harbor islands": "Bay Harbor Islands",
    "bay harbour islands": "Bay Harbor Islands",
    "sunny isles beach": "Sunny Isles Beach",
    "sunny isles": "Sunny Isles Beach",
    "sunnyisles": "Sunny Isles Beach",
}

_STATE_ALIASES = {"florida": "FL", "fl": "FL", "fla": "FL"}


@dataclass(frozen=True)
class AddressParts:
    """Canonical address components. Every field is lowercase except state/city."""

    number: str = ""
    directional: str = ""
    street: str = ""
    suffix: str = ""
    post_directional: str = ""
    unit: str = ""
    city: str = ""
    state: str = ""
    zip5: str = ""

    @property
    def street_line(self) -> str:
        parts = [self.number, self.directional, self.street, self.suffix, self.post_directional]
        return " ".join(p for p in parts if p).strip()

    @property
    def is_usable(self) -> bool:
        """Enough signal to hash on: a house number plus a street name."""
        return bool(self.number and self.street)

    def display(self) -> str:
        line = self.street_line.title()
        if self.unit:
            line = f"{line} #{self.unit.upper()}"
        tail = ", ".join(p for p in [self.city, f"{self.state} {self.zip5}".strip()] if p)
        return f"{line}, {tail}" if tail else line


def normalize_city(value: object) -> str:
    text = clean_text(value).lower().strip(" ,.")
    if not text:
        return ""
    return CITY_ALIASES.get(text, clean_text(value).strip(" ,.").title())


def normalize_state(value: object) -> str:
    text = clean_text(value).lower().strip(" ,.")
    return _STATE_ALIASES.get(text, text.upper()[:2] if text else "")


def normalize_zip(value: object) -> str:
    """'33154-2932' -> '33154'. ZIP+4 differs between portals for one building."""
    match = re.search(r"(\d{5})", str(value or ""))
    return match.group(1) if match else ""


def normalize_unit(value: object) -> str:
    """'#1502-N' / 'Apt 1502 N' / 'PH-3' -> '1502n' / 'ph3'.

    Separators are dropped because portals disagree on them, but the token order
    is preserved so 1502N and N1502 stay distinct.
    """
    text = clean_text(value).lower()
    if not text:
        return ""
    text = re.sub(
        r"^\s*(?:" + "|".join(re.escape(m) for m in _UNIT_NOISE_MARKERS) + r")\b\.?[\s#-]*",
        "",
        text,
    )
    text = text.replace("#", "")
    text = re.sub(r"[^a-z0-9]+", "", text)
    if text in {"", "na", "none", "null", "n/a"}:
        return ""
    return text


def _normalize_street_token(token: str) -> str:
    token = token.strip(".,")
    return _ORDINAL_WORDS.get(token, token)


def parse_address(
    raw: object,
    *,
    city: object = None,
    state: object = None,
    zip_code: object = None,
    unit: object = None,
) -> AddressParts:
    """Decompose a free-text address (optionally with structured overrides).

    Structured fields supplied by the caller always win over anything parsed out
    of the string -- portal APIs that give a separate ``unitNumber`` are more
    trustworthy than the concatenated display address.
    """
    text = clean_text(raw)
    parsed_unit = normalize_unit(unit)
    parsed_city = normalize_city(city)
    parsed_state = normalize_state(state)
    parsed_zip = normalize_zip(zip_code)

    if not text:
        return AddressParts(
            unit=parsed_unit, city=parsed_city, state=parsed_state, zip5=parsed_zip
        )

    segments = [s.strip() for s in text.split(",") if s.strip()]
    street_segment = segments[0] if segments else ""

    # --- locality: everything after the first comma ------------------------
    for index, segment in enumerate(segments[1:]):
        body, seg_state, seg_zip = _peel_state_zip(segment)
        if not parsed_zip and seg_zip:
            parsed_zip = seg_zip
        if not parsed_state and seg_state:
            parsed_state = normalize_state(seg_state)
        if not body:
            continue
        if index == 0 and not parsed_city:
            parsed_city = normalize_city(body)
        elif not parsed_state and body.lower() in _STATE_ALIASES:
            parsed_state = normalize_state(body)

    # --- comma-less addresses ("9705 Collins Ave Bal Harbour FL 33154") ----
    if len(segments) == 1:
        street_segment, tail_state, tail_zip, tail_city = _peel_locality(street_segment)
        if not parsed_zip:
            parsed_zip = tail_zip
        if not parsed_state and tail_state:
            parsed_state = normalize_state(tail_state)
        if not parsed_city and tail_city:
            parsed_city = normalize_city(tail_city)

    street_segment, inline_unit = _split_unit(street_segment)
    if inline_unit and not parsed_unit:
        parsed_unit = normalize_unit(inline_unit)

    tokens = [_normalize_street_token(t) for t in street_segment.lower().split() if t.strip(".,")]
    tokens = [t for t in tokens if t]

    number = ""
    if tokens and re.match(r"^\d+[a-z]?(?:-\d+)?$", tokens[0]):
        number = tokens.pop(0)

    directional = ""
    if tokens and tokens[0] in _DIRECTIONALS and len(tokens) > 1:
        directional = _DIRECTIONALS[tokens.pop(0)]

    post_directional = ""
    if len(tokens) > 1 and tokens[-1] in _DIRECTIONALS:
        post_directional = _DIRECTIONALS[tokens.pop()]

    suffix = ""
    if len(tokens) > 1 and tokens[-1] in _STREET_SUFFIXES:
        suffix = _STREET_SUFFIXES[tokens.pop()]

    street = " ".join(tokens).strip()

    return AddressParts(
        number=number,
        directional=directional,
        street=street,
        suffix=suffix,
        post_directional=post_directional,
        unit=parsed_unit,
        city=parsed_city,
        state=parsed_state,
        zip5=parsed_zip,
    )


def _peel_state_zip(segment: str) -> tuple[str, str, str]:
    """Strip a trailing ZIP and state token: 'Bal Harbour FL 33154-2932'
    -> ('Bal Harbour', 'FL', '33154')."""
    text = segment.strip(" ,")
    zip5 = ""
    zip_match = re.search(r"\b(\d{5})(?:-\d{4})?\s*$", text)
    if zip_match:
        zip5 = zip_match.group(1)
        text = text[: zip_match.start()].strip(" ,")

    state = ""
    if text.lower() in _STATE_ALIASES:
        return "", text, zip5
    state_match = re.search(r"[\s,]+(florida|fla|[A-Za-z]{2})\s*$", text, flags=re.IGNORECASE)
    if state_match:
        state = state_match.group(1)
        text = text[: state_match.start()].strip(" ,")
    return text, state, zip5


def _peel_locality(text: str) -> tuple[str, str, str, str]:
    """Peel ZIP, state and a known city off the end of a comma-less address.

    Returns ``(street_remainder, state, zip5, city)``. The ZIP is only taken
    from the end of the string so a five-digit street number such as
    '18201 Collins Ave' is never mistaken for a postal code.
    """
    remainder, state, zip5 = _peel_state_zip(text)

    city = ""
    lowered = remainder.lower()
    # Longest alias first so "sunny isles beach" wins over "sunny isles".
    for alias in sorted(CITY_ALIASES, key=len, reverse=True):
        if lowered.endswith(alias):
            boundary = len(remainder) - len(alias)
            if boundary == 0 or not remainder[boundary - 1].isalnum():
                city = alias
                remainder = remainder[:boundary].strip(" ,-")
                break
    return remainder, state, zip5, city


def _split_unit(street_segment: str) -> tuple[str, str]:
    """Separate '9705 Collins Ave Apt 1502 N' -> ('9705 Collins Ave', '1502 N').

    A trailing one-to-three character token is absorbed into the unit because
    portals split stack letters off the number inconsistently ('1502N',
    '1502-N', '1502 N' are the same unit).
    """
    if not street_segment:
        return "", ""

    value_pattern = r"([A-Za-z0-9][A-Za-z0-9\-]*(?:\s+[A-Za-z0-9]{1,3})?)"

    hash_match = re.search(r"#\s*" + value_pattern + r"\s*$", street_segment)
    if hash_match:
        return street_segment[: hash_match.start()].strip(" ,-"), hash_match.group(1)

    marker_pattern = (
        r"\b(" + "|".join(re.escape(m) for m in _UNIT_MARKERS if m != "#") + r")\b"
        r"[\s.#-]*" + value_pattern + r"\s*$"
    )
    marker_match = re.search(marker_pattern, street_segment, flags=re.IGNORECASE)
    if marker_match:
        marker = marker_match.group(1).lower()
        value = marker_match.group(2)
        # "PH"/"TH" label the unit itself rather than merely introducing it.
        if marker in {"ph", "penthouse", "th", "townhouse"}:
            value = f"{marker}{value}"
        return street_segment[: marker_match.start()].strip(" ,-"), value

    # Bare trailing unit with no marker at all: "Collins Ave TH2", "Collins
    # Ave N-501", "South Pointe Dr 1502". Only accepted when the preceding
    # token is a street suffix or directional, which keeps numbered streets
    # ("1000 5th St") and plain addresses from losing their last token.
    tokens = street_segment.split()
    if len(tokens) >= 3:
        candidate = tokens[-1].strip(",")
        previous = tokens[-2].strip(",.").lower()
        anchored = previous in _STREET_SUFFIXES or previous in _DIRECTIONALS
        looks_like_unit = bool(
            re.fullmatch(r"(?:ph|th)?-?\d{1,5}[a-z]?|[a-z]-?\d{1,5}[a-z]?", candidate, flags=re.IGNORECASE)
        ) and any(ch.isdigit() for ch in candidate)
        if anchored and looks_like_unit:
            return " ".join(tokens[:-1]).strip(" ,-"), candidate

    return street_segment.strip(), ""


def _hash(*parts: str) -> str:
    payload = "|".join(p.strip().lower() for p in parts)
    return hashlib.blake2b(payload.encode("utf-8"), digest_size=16).hexdigest()


def building_key(parts: AddressParts) -> str:
    """Identity of the tower, ignoring the unit. Used for grouping and for
    county-assessor lookups where a folio covers the building."""
    if not parts.is_usable:
        return ""
    return _hash(
        parts.number, parts.directional, parts.street, parts.suffix,
        parts.post_directional, parts.zip5 or parts.city,
    )


def unit_key(parts: AddressParts) -> str:
    """Identity of a specific unit. Empty when the address is unusable."""
    if not parts.is_usable:
        return ""
    return _hash(building_key(parts), parts.unit)


def geo_key(latitude: float | None, longitude: float | None, unit: str = "") -> str:
    """Fallback identity for listings with coordinates but a broken address.

    Rounded to 4 decimal places (~11 m), which resolves separate towers without
    splitting one building across float noise from different portals.
    """
    if latitude is None or longitude is None:
        return ""
    return _hash(f"{float(latitude):.4f}", f"{float(longitude):.4f}", normalize_unit(unit))


def fingerprint(
    raw_address: object,
    *,
    city: object = None,
    state: object = None,
    zip_code: object = None,
    unit: object = None,
    latitude: float | None = None,
    longitude: float | None = None,
) -> tuple[AddressParts, str, str, str]:
    """One-shot helper returning (parts, unit_key, building_key, geo_key)."""
    parts = parse_address(raw_address, city=city, state=state, zip_code=zip_code, unit=unit)
    return parts, unit_key(parts), building_key(parts), geo_key(latitude, longitude, parts.unit)
