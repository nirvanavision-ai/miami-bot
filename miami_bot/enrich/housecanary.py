"""HouseCanary: rental and sale AVMs via the v2 analytics API.

HouseCanary authenticates with HTTP Basic (key as user, secret as password) and
answers with a list of per-address envelopes, each carrying an ``api_code`` that
must be checked -- a 200 response with ``api_code`` 204 means "no data for this
address", not success.
"""

from __future__ import annotations

from typing import Any

from ..config import HouseCanarySettings
from ..models import Listing
from ..util.http import HttpClient, NotFound
from ..util.logging import get_logger
from .rentcast import RentEstimate

log = get_logger(__name__)


class HouseCanaryClient:
    def __init__(self, http: HttpClient, settings: HouseCanarySettings) -> None:
        self.http = http
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return self.settings.enabled

    @property
    def _auth(self) -> tuple[str, str]:
        return (self.settings.api_key, self.settings.api_secret)

    def _address_params(self, listing: Listing) -> dict[str, str] | None:
        parts = listing.address_parts
        if parts is None or not parts.is_usable or not parts.zip5:
            return None
        line = parts.street_line.title()
        if parts.unit:
            line = f"{line} Unit {parts.unit.upper()}"
        return {"address": line, "zipcode": parts.zip5}

    def estimates(self, listing: Listing) -> tuple[RentEstimate | None, int | None]:
        """Return (rent estimate, sale value estimate).

        Both come from one ``component_mget`` call, which is a single billed
        request instead of two.
        """
        params = self._address_params(listing)
        if not params:
            return None, None

        params = dict(params)
        params["components"] = "property/value,property/value_rental"
        try:
            payload = self.http.get_json(
                f"{self.settings.base_url}/property/component_mget",
                params=params,
                auth=self._auth,
            )
        except NotFound:
            return None, None

        envelope = _first_envelope(payload)
        if envelope is None:
            return None, None

        rent = _rent_from(envelope.get("property/value_rental"))
        value = _value_from(envelope.get("property/value"))
        return rent, value


def _first_envelope(payload: Any) -> dict[str, Any] | None:
    if isinstance(payload, list) and payload:
        candidate = payload[0]
        return candidate if isinstance(candidate, dict) else None
    if isinstance(payload, dict):
        return payload
    return None


def _rent_from(block: Any) -> RentEstimate | None:
    result = _unwrap(block)
    if not result:
        return None
    value = result.get("value") if isinstance(result, dict) else None
    if not isinstance(value, dict):
        return None
    return RentEstimate(
        rent=_as_int(value.get("price") or value.get("value")),
        low=_as_int(value.get("price_lower") or value.get("value_lower")),
        high=_as_int(value.get("price_upper") or value.get("value_upper")),
        source="housecanary",
    )


def _value_from(block: Any) -> int | None:
    result = _unwrap(block)
    if not result:
        return None
    value = result.get("value") if isinstance(result, dict) else None
    if not isinstance(value, dict):
        return None
    return _as_int(value.get("price") or value.get("value"))


def _unwrap(block: Any) -> dict[str, Any] | None:
    """Validate the api_code and return the ``result`` payload."""
    if not isinstance(block, dict):
        return None
    code = block.get("api_code")
    if code not in (None, 0):
        log.info("housecanary: api_code %s -- %s", code, block.get("api_code_description"))
        return None
    result = block.get("result")
    return result if isinstance(result, dict) else None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(round(number)) if number > 0 else None
