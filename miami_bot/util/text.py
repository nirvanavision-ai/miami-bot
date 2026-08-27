"""Text parsing helpers: prices, specs and -- critically -- lease terms.

Portal wrappers report specs inconsistently ("2", "2 Beds", "2+den", "1,450 sf",
"$8,500/mo"). These helpers coerce that mess into numbers and pull the lease
term out of free-text descriptions, which is where the 6-12 month constraint is
actually decided.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# Spelled-out numbers appear constantly in listing copy ("six month minimum").
_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "eighteen": 18, "twentyfour": 24, "twenty four": 24,
}

_MONTHS_IN_YEAR = 12


def clean_text(value: object) -> str:
    """Normalize whitespace/unicode and lowercase for keyword scanning."""
    if value is None:
        return ""
    text = str(value)
    text = unicodedata.normalize("NFKD", text)
    text = text.replace("–", "-").replace("—", "-").replace("’", "'")
    text = re.sub(r"<[^>]+>", " ", text)          # strip stray HTML from scrapes
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_for_match(value: object) -> str:
    """Lowercased, punctuation-collapsed text used for keyword containment."""
    text = clean_text(value).lower()
    # Keep hyphens (short-term) and slashes; collapse everything else to space.
    text = re.sub(r"[^a-z0-9\-/+.,$ ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_price(value: object) -> int | None:
    """'$8,500/mo' -> 8500. Rejects obvious sale prices."""
    if value is None:
        return None
    if isinstance(value, int | float):
        number = float(value)
    else:
        text = clean_text(value)
        match = re.search(r"\$?\s*([\d][\d,]*(?:\.\d+)?)", text)
        if not match:
            return None
        try:
            number = float(match.group(1).replace(",", ""))
        except ValueError:
            return None
    if number <= 0:
        return None
    return int(round(number))


def parse_beds(value: object) -> float | None:
    """'2 Beds', '2+Den', 'Studio', 3 -> float bedrooms."""
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value) if value >= 0 else None
    text = clean_text(value).lower()
    if "studio" in text:
        return 0.0
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if not match:
        word = re.search(r"\b(one|two|three|four|five|six)\b", text)
        return float(_WORD_NUMBERS[word.group(1)]) if word else None
    return float(match.group(1))


def parse_baths(value: object) -> float | None:
    """'2.5 Baths', '2 full 1 half', 2 -> float bathrooms (half = 0.5)."""
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value) if value >= 0 else None
    text = clean_text(value).lower()
    full = re.search(r"(\d+(?:\.\d+)?)\s*(?:full)?", text)
    half = re.search(r"(\d+)\s*half", text)
    total = 0.0
    if full:
        try:
            total += float(full.group(1))
        except ValueError:
            return None
    if half:
        total += float(half.group(1)) * 0.5
    return total or None


def parse_sqft(value: object) -> int | None:
    """'1,450 sq ft' / '1450' / 1450.0 -> 1450. Filters implausible values."""
    if value is None:
        return None
    if isinstance(value, int | float):
        number = float(value)
    else:
        text = clean_text(value).lower().replace(",", "")
        match = re.search(r"(\d+(?:\.\d+)?)", text)
        if not match:
            return None
        try:
            number = float(match.group(1))
        except ValueError:
            return None
    # Anything under 200 or above 25,000 is a unit-of-measure error, not a condo.
    if number < 200 or number > 25000:
        return None
    return int(round(number))


def parse_year_built(value: object) -> int | None:
    if value is None:
        return None
    match = re.search(r"(1[89]\d{2}|20\d{2})", str(value))
    if not match:
        return None
    year = int(match.group(1))
    return year if 1880 <= year <= 2100 else None


# ---------------------------------------------------------------------------
# Lease-term extraction
# ---------------------------------------------------------------------------

# Ordered most-specific first: a range pattern must win over a bare "12 months".
_RANGE_PATTERNS = [
    # "6-12 months", "6 to 12 month", "6 - 12 mos"
    re.compile(r"\b(\d{1,2})\s*(?:-|to|thru|through|/)\s*(\d{1,2})\s*(?:\+)?\s*(?:mo|mos|month|months)\b"),
    # "minimum 6 months maximum 12 months"
    re.compile(r"\bmin(?:imum)?\.?\s*(\d{1,2})\s*(?:mo|mos|month|months)\b.{0,40}?"
               r"\bmax(?:imum)?\.?\s*(\d{1,2})\s*(?:mo|mos|month|months)\b"),
]

_MIN_PATTERNS = [
    re.compile(r"\bmin(?:imum)?\.?\s*(?:of\s*)?(\d{1,2})\s*(?:mo|mos|month|months)\b"),
    re.compile(r"\b(\d{1,2})\s*(?:mo|mos|month|months)\s*(?:min(?:imum)?|or (?:more|longer)|\+)\b"),
    re.compile(r"\bat least\s*(\d{1,2})\s*(?:mo|mos|month|months)\b"),
]

_MAX_PATTERNS = [
    re.compile(r"\bmax(?:imum)?\.?\s*(?:of\s*)?(\d{1,2})\s*(?:mo|mos|month|months)\b"),
    re.compile(r"\bup to\s*(\d{1,2})\s*(?:mo|mos|month|months)\b"),
    re.compile(r"\bno more than\s*(\d{1,2})\s*(?:mo|mos|month|months)\b"),
]

_EXACT_PATTERNS = [
    re.compile(r"\b(\d{1,2})\s*(?:mo|mos|month|months)\s*(?:lease|term|rental|only)\b"),
    re.compile(r"\blease\s*(?:term|length|of|:)?\s*(\d{1,2})\s*(?:mo|mos|month|months)\b"),
    re.compile(r"\b(\d{1,2})\s*(?:mo|mos|month|months)\b"),
]

_YEAR_PATTERNS = [
    re.compile(r"\b(\d{1,2})\s*(?:yr|yrs|year|years)\s*(?:lease|term|minimum|min)?\b"),
    re.compile(r"\b(one|two|three)\s*(?:yr|yrs|year|years)\b"),
]

_ANNUAL_PHRASES = (
    "annual lease", "annual rental", "annual only", "yearly lease",
    "one year lease", "1 year lease", "12 month lease", "12-month lease",
    "long term lease", "long-term lease", "unfurnished annual",
)


def _digitize(text: str) -> str:
    """Rewrite spelled-out numbers as digits before term matching.

    Listing copy says "six to twelve month lease" as often as "6-12 months",
    and a digits-only regex silently misses every one of them.
    """
    if not text:
        return text
    # Longest first so "twenty four" is consumed before "four".
    for word in sorted(_WORD_NUMBERS, key=len, reverse=True):
        text = re.sub(
            r"(?<![a-z0-9])" + word.replace(" ", r"\s+") + r"(?![a-z0-9])",
            str(_WORD_NUMBERS[word]),
            text,
        )
    return text


@dataclass
class LeaseTerm:
    """Parsed lease-term window, in months."""

    min_months: int | None = None
    max_months: int | None = None
    raw: str = ""
    # How the value was derived -- surfaced in alerts so a human can sanity-check.
    evidence: list[str] = field(default_factory=list)

    @property
    def known(self) -> bool:
        return self.min_months is not None or self.max_months is not None

    def describe(self) -> str:
        if self.min_months and self.max_months:
            if self.min_months == self.max_months:
                return f"{self.min_months} months"
            return f"{self.min_months}-{self.max_months} months"
        if self.min_months:
            return f"{self.min_months}+ months"
        if self.max_months:
            return f"up to {self.max_months} months"
        return "unspecified"


def parse_lease_term(*texts: object) -> LeaseTerm:
    """Extract a lease window from any number of free-text fields.

    Returns the *widest defensible* reading: a listing saying "6 month minimum"
    with no cap yields ``min=6, max=None``, which the filter then treats as
    satisfying a 6-12 requirement only if nothing else disqualifies it.
    """
    blob = _digitize(normalize_for_match(" . ".join(clean_text(t) for t in texts if t)))
    term = LeaseTerm(raw=blob[:500])
    if not blob:
        return term

    for pattern in _RANGE_PATTERNS:
        match = pattern.search(blob)
        if match:
            low, high = int(match.group(1)), int(match.group(2))
            if 0 < low <= high <= 60:
                term.min_months, term.max_months = low, high
                term.evidence.append(f"range:{match.group(0).strip()}")
                return term

    for pattern in _MIN_PATTERNS:
        match = pattern.search(blob)
        if match:
            value = int(match.group(1))
            if 0 < value <= 60:
                term.min_months = value
                term.evidence.append(f"min:{match.group(0).strip()}")
                break

    for pattern in _MAX_PATTERNS:
        match = pattern.search(blob)
        if match:
            value = int(match.group(1))
            if 0 < value <= 60:
                term.max_months = value
                term.evidence.append(f"max:{match.group(0).strip()}")
                break

    if term.known:
        return term

    for pattern in _YEAR_PATTERNS:
        match = pattern.search(blob)
        if match:
            token = match.group(1)
            years = _WORD_NUMBERS.get(token) if not token.isdigit() else int(token)
            if years and 0 < years <= 5:
                months = years * _MONTHS_IN_YEAR
                term.min_months = term.max_months = months
                term.evidence.append(f"years:{match.group(0).strip()}")
                return term

    for pattern in _EXACT_PATTERNS:
        match = pattern.search(blob)
        if match:
            value = int(match.group(1))
            if 0 < value <= 60:
                term.min_months = term.max_months = value
                term.evidence.append(f"exact:{match.group(0).strip()}")
                return term

    for phrase in _ANNUAL_PHRASES:
        if phrase in blob:
            term.min_months = term.max_months = _MONTHS_IN_YEAR
            term.evidence.append(f"phrase:{phrase}")
            return term

    return term


def find_keywords(text: str, keywords: list[str]) -> list[str]:
    """Return every keyword present in ``text`` (already normalized or not)."""
    blob = normalize_for_match(text)
    if not blob:
        return []
    hits = []
    for keyword in keywords:
        needle = normalize_for_match(keyword)
        if not needle:
            continue
        if _contains_phrase(blob, needle):
            hits.append(keyword)
    return hits


def _phrase_pattern(needle: str) -> str:
    """Build a whole-token regex that is indifferent to separators.

    One keyword therefore covers "short term", "short-term" and "shortterm",
    which is how the same disqualifier is written across different portals.
    """
    parts = [re.escape(part) for part in re.split(r"[\s\-_/]+", needle) if part]
    if not parts:
        return r"(?!x)x"  # never matches
    return r"(?<![a-z0-9])" + r"[\s\-_]*".join(parts) + r"(?![a-z0-9])"


def _contains_phrase(haystack: str, needle: str) -> bool:
    """Whole-token containment so 'daily' never matches inside 'dailymotion'."""
    return re.search(_phrase_pattern(needle), haystack) is not None


# Words that flip the meaning of a disqualifying keyword. Listing copy is full
# of "NO short term rentals" and "seasonal rentals not permitted" -- scanning for
# the bare keyword would reject exactly the annual leases we are hunting for.
_NEGATORS_BEFORE = (
    "no", "not", "non", "never", "without", "excluding", "excludes", "exclude",
    "prohibited", "prohibits", "prohibit", "unavailable", "sorry", "cannot",
    "cant", "wont", "declined", "denies", "deny", "absolutely", "strictly",
)
_NEGATORS_AFTER = (
    "not available", "not allowed", "not permitted", "not accepted", "prohibited",
    "unavailable", "excluded", "will not be considered", "need not apply",
    "not an option", "no", "not",
)

_NEGATION_WINDOW = 4


@dataclass
class KeywordScan:
    """Result of scanning text for disqualifying keywords."""

    hits: list[str] = field(default_factory=list)          # genuine disqualifiers
    negated: list[str] = field(default_factory=list)       # "no short term rentals"

    @property
    def disqualified(self) -> bool:
        return bool(self.hits)


def scan_keywords(text: str, keywords: list[str]) -> KeywordScan:
    """Find disqualifying keywords, ignoring negated occurrences."""
    blob = normalize_for_match(text)
    scan = KeywordScan()
    if not blob:
        return scan

    for keyword in keywords:
        needle = normalize_for_match(keyword)
        if not needle:
            continue
        occurrences = list(re.finditer(_phrase_pattern(needle), blob))
        if not occurrences:
            continue
        if all(_is_negated(blob, m.start(), m.end()) for m in occurrences):
            scan.negated.append(keyword)
        else:
            scan.hits.append(keyword)
    return scan


def _is_negated(blob: str, start: int, end: int) -> bool:
    """True when the match at [start:end) sits inside a negating phrase."""
    before_tokens = blob[:start].split()[-_NEGATION_WINDOW:]
    if any(token.strip(".,") in _NEGATORS_BEFORE for token in before_tokens):
        return True

    after = blob[end:].lstrip(" .,-:;")
    after_window = " ".join(after.split()[:_NEGATION_WINDOW])
    if not after_window:
        return False
    # "seasonal rentals not permitted" negates just as surely as "not seasonal".
    return any(_contains_phrase(after_window, phrase) for phrase in _NEGATORS_AFTER)


def match_amenities(text: str, amenity_map: dict[str, list[str]]) -> dict[str, list[str]]:
    """Map amenity category -> matched phrases found in the text."""
    blob = normalize_for_match(text)
    found: dict[str, list[str]] = {}
    for category, phrases in amenity_map.items():
        hits = [p for p in phrases if _contains_phrase(blob, normalize_for_match(p))]
        if hits:
            found[category] = hits
    return found


def truncate(text: str, limit: int = 280) -> str:
    text = clean_text(text)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"
