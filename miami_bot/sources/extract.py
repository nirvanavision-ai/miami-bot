"""Tolerant JSON traversal for third-party listing payloads.

Portal wrapper APIs on RapidAPI are re-publishers: their response shapes track
whatever the upstream consumer site emits and change without notice. Hard-coding
``data["home_search"]["results"][0]["description"]["beds"]`` produces an adapter
that silently returns zero listings the day the wrapper adds a envelope.

So each field is declared as an ordered list of candidate paths, and there is a
structural fallback (:func:`find_result_array`) that locates the results array by
shape rather than by name when every declared path misses.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from typing import Any

_INDEX = re.compile(r"^(.*?)\[(\d+|\*)\]$")


def dig(data: Any, path: str) -> Any:
    """Follow a dotted path with optional indexing.

    ``"location.address.coordinate.lat"``, ``"photos[0].href"`` and
    ``"tags[*]"`` are all valid. Returns ``None`` on any miss rather than
    raising -- a missing field is normal, not exceptional.
    """
    current = data
    for segment in path.split("."):
        if current is None:
            return None
        match = _INDEX.match(segment)
        index: str | None = None
        if match:
            segment, index = match.group(1), match.group(2)

        if segment:
            if isinstance(current, dict):
                current = current.get(segment)
            else:
                return None

        if index is not None:
            if not isinstance(current, list | tuple):
                return None
            if index == "*":
                return list(current)
            position = int(index)
            current = current[position] if position < len(current) else None
    return current


def first(
    data: Any,
    paths: Sequence[str],
    cast: Callable[[Any], Any] | None = None,
    *,
    default: Any = None,
) -> Any:
    """Return the first path that yields a usable value."""
    for path in paths:
        value = dig(data, path)
        if value in (None, "", [], {}):
            continue
        if cast is None:
            return value
        converted = cast(value)
        if converted not in (None, "", [], {}):
            return converted
    return default


def collect_strings(data: Any, paths: Sequence[str], limit: int = 40) -> list[str]:
    """Union of string values found at any of ``paths`` (flattening lists)."""
    out: list[str] = []
    seen: set[str] = set()
    for path in paths:
        value = dig(data, path)
        for item in _iter_strings(value):
            cleaned = item.strip()
            if cleaned and cleaned.lower() not in seen:
                seen.add(cleaned.lower())
                out.append(cleaned)
                if len(out) >= limit:
                    return out
    return out


def _iter_strings(value: Any) -> Iterable[str]:
    if value is None:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        # Amenity/photo objects: prefer the obvious label or href field.
        for key in ("href", "url", "name", "label", "text", "value", "description"):
            if isinstance(value.get(key), str):
                yield value[key]
                return
        for nested in value.values():
            yield from _iter_strings(nested)
    elif isinstance(value, list | tuple | set):
        for item in value:
            yield from _iter_strings(item)
    elif isinstance(value, int | float):
        yield str(value)


# Keys that mark a dict as "probably a listing" for the structural fallback.
_LISTING_SIGNALS = (
    "price", "list_price", "rent", "rentzestimate", "zpid", "property_id",
    "propertyid", "listingid", "listing_id", "mlsid", "address", "streetaddress",
    "bedrooms", "beds", "baths", "bathrooms", "livingarea", "sqft",
)


def find_result_array(payload: Any, min_signals: int = 2) -> list[dict[str, Any]]:
    """Locate the listings array in an unknown response shape.

    Walks the whole document and returns the largest list whose dict members
    carry at least ``min_signals`` listing-ish keys. This is the safety net that
    keeps an adapter working through a wrapper's envelope change.
    """
    best: list[dict[str, Any]] = []

    def visit(node: Any, depth: int = 0) -> None:
        nonlocal best
        if depth > 8:
            return
        if isinstance(node, list):
            dicts = [item for item in node if isinstance(item, dict)]
            if dicts:
                scored = sum(1 for item in dicts[:5] if _signal_count(item) >= min_signals)
                if scored >= max(1, min(len(dicts[:5]), 1)) and len(dicts) > len(best):
                    best = dicts
            for item in node[:200]:
                visit(item, depth + 1)
        elif isinstance(node, dict):
            for value in node.values():
                visit(value, depth + 1)

    visit(payload)
    return best


def _signal_count(item: dict[str, Any]) -> int:
    lowered = {str(key).lower() for key in item}
    return sum(1 for signal in _LISTING_SIGNALS if signal in lowered)


def deep_get_text(payload: Any, keys: Sequence[str], limit: int = 4000) -> str:
    """Concatenate every string found under any of ``keys``, anywhere in the doc.

    Used for description/lease-term text, which wrappers scatter across
    ``description``, ``remarks``, ``publicRemarks``, ``resoFacts.leaseTerm`` and
    a dozen other spellings depending on the upstream feed.
    """
    wanted = {k.lower() for k in keys}
    chunks: list[str] = []

    def visit(node: Any, depth: int = 0) -> None:
        if depth > 8 or sum(len(c) for c in chunks) > limit:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key).lower() in wanted:
                    for text in _iter_strings(value):
                        if text and text not in chunks:
                            chunks.append(text)
                else:
                    visit(value, depth + 1)
        elif isinstance(node, list):
            for item in node[:100]:
                visit(item, depth + 1)

    visit(payload)
    return " . ".join(chunks)[:limit]
