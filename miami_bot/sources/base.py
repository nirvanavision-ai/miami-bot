"""Source adapter contract.

A source is anything that can produce :class:`~miami_bot.models.Listing`
objects for the configured criteria: a RapidAPI portal wrapper, RealtyAPI, or
the ScrapingBee fallback. The pipeline only ever sees this interface, which is
what lets Module 2 substitute for Module 1 without special-casing.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..config import SearchCriteria
from ..models import Listing
from ..util.http import AuthError, HttpClient, HttpError, NotFound, RateLimited
from ..util.logging import get_logger

log = get_logger(__name__)


@dataclass
class SourceResult:
    """What one source returned in one run, including how it failed."""

    source: str
    listings: list[Listing] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: Non-fatal diagnostics (quota caps hit, fields missing). These are shown
    #: in the run summary but never count toward source-health/staleness.
    notes: list[str] = field(default_factory=list)
    requests_made: int = 0
    duration_seconds: float = 0.0
    skipped: bool = False
    skip_reason: str = ""

    @property
    def ok(self) -> bool:
        """Healthy = ran, and either produced listings or failed cleanly with
        an empty-but-valid response."""
        return not self.skipped and not self.errors

    @property
    def count(self) -> int:
        return len(self.listings)

    def summary(self) -> str:
        if self.skipped:
            return f"{self.source}: skipped ({self.skip_reason})"
        state = "ok" if self.ok else f"{len(self.errors)} error(s)"
        return (
            f"{self.source}: {self.count} listings, {self.requests_made} requests, "
            f"{self.duration_seconds:.1f}s, {state}"
        )


class ListingSource(ABC):
    """Base class for every ingestion adapter."""

    #: Stable identifier persisted on every listing row.
    name: str = "source"
    #: Human label used in logs and alerts.
    label: str = "Source"
    #: True for Module 2 adapters that only run when the primaries go stale.
    is_fallback: bool = False

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    # ------------------------------------------------------------- interface
    @property
    @abstractmethod
    def enabled(self) -> bool:
        """False when credentials are absent -- the pipeline skips it quietly."""

    @abstractmethod
    def _fetch(self, criteria: SearchCriteria, result: SourceResult) -> list[Listing]:
        """Do the provider-specific work. Exceptions are caught by ``fetch``."""

    # ---------------------------------------------------------------- driver
    def fetch(self, criteria: SearchCriteria) -> SourceResult:
        """Run the adapter, converting every failure into a recorded error.

        One provider outage must never abort a run: the pipeline needs the other
        sources' results and needs to know this one is down so it can decide
        whether to trip the scraper fallback.
        """
        result = SourceResult(source=self.name)
        if not self.enabled:
            result.skipped = True
            result.skip_reason = "not configured"
            return result

        started = time.monotonic()
        try:
            result.listings = self._fetch(criteria, result) or []
        except AuthError as exc:
            result.errors.append(f"auth rejected -- check the API key ({exc})")
            log.error("%s: %s", self.name, exc)
        except RateLimited as exc:
            result.errors.append(f"rate limited / quota exhausted ({exc})")
            log.warning("%s: %s", self.name, exc)
        except NotFound as exc:
            result.errors.append(f"endpoint not found -- host or path may have changed ({exc})")
            log.warning("%s: %s", self.name, exc)
        except HttpError as exc:
            result.errors.append(f"transport failure ({exc})")
            log.warning("%s: %s", self.name, exc)
        except Exception as exc:  # adapter bug or unexpected payload shape
            result.errors.append(f"unexpected {type(exc).__name__}: {exc}")
            log.exception("%s: unhandled error", self.name)
        finally:
            result.duration_seconds = time.monotonic() - started

        log.info("%s", result.summary())
        return result

    # -------------------------------------------------------------- helpers
    def make_listing(self, **fields: Any) -> Listing:
        """Construct a Listing already stamped with this source's name."""
        fields.setdefault("source", self.name)
        return Listing(**fields)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} name={self.name!r} enabled={self.enabled}>"
