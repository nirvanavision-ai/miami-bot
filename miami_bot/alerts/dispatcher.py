"""Decide what to alert on, then fan out to every configured channel.

The gating rules live here, not in the channels:

* only net-new units and genuine price drops produce an alert,
* a price drop must clear both a dollar and a percentage floor, so a $25 nudge
  on a $10,000 rental stays quiet,
* a unit already alerted within the cooldown window is suppressed,
* a run is capped, and when the cap bites the highest-scoring listings win.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Settings
from ..db import ChangeSet, Database
from ..models import Listing, MatchResult
from ..util.http import HttpClient
from ..util.logging import get_logger
from .base import AlertChannel, SendResult
from .discord import DiscordChannel
from .email_smtp import EmailChannel
from .format import AlertContent, build_content
from .slack import SlackChannel

log = get_logger(__name__)


@dataclass
class AlertEvent:
    """One thing worth telling the user about."""

    listing: Listing
    kind: str                      # "new" | "price_drop"
    dedupe_key: str
    old_price: int | None = None
    score: float = 0.0
    warnings: list[str] = field(default_factory=list)
    extra_sources: list[str] = field(default_factory=list)

    def to_content(self) -> AlertContent:
        return build_content(
            self.listing,
            kind=self.kind,
            old_price=self.old_price,
            warnings=self.warnings,
            score=self.score,
            extra_sources=self.extra_sources,
        )


class AlertDispatcher:
    def __init__(self, http: HttpClient, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.criteria = settings.pipeline.alerts
        self.db = db
        self.channels: list[AlertChannel] = [
            DiscordChannel(http, settings.alerts.discord_webhook_url),
            SlackChannel(http, settings.alerts.slack_webhook_url),
            EmailChannel(settings.alerts),
        ]

    @property
    def active_channels(self) -> list[AlertChannel]:
        return [channel for channel in self.channels if channel.enabled]

    # ------------------------------------------------------------- decisions
    def build_event(self, result: MatchResult, change: ChangeSet) -> AlertEvent | None:
        """Turn a match + change into an alert, or None if it isn't newsworthy."""
        if not result.passed:
            return None
        if result.warnings and not self.criteria.include_warned:
            return None

        listing = result.listing
        if change.is_new:
            kind = "new"
        elif change.price_drop and self._is_material_drop(change):
            kind = "price_drop"
        else:
            return None

        if self.db.was_alerted_recently(change.dedupe_key, kind, self.criteria.cooldown_hours):
            log.debug("suppressed %s alert for %s (cooldown)", kind, change.dedupe_key)
            return None

        extra_sources = [
            row["source"] for row in self.db.sightings_for(change.dedupe_key)
            if row["source"] != listing.source
        ]

        return AlertEvent(
            listing=listing,
            kind=kind,
            dedupe_key=change.dedupe_key,
            old_price=change.old_price,
            score=result.score,
            warnings=result.warnings,
            extra_sources=extra_sources,
        )

    def _is_material_drop(self, change: ChangeSet) -> bool:
        """A drop must clear both the dollar and the percentage floor."""
        if change.delta is None or change.delta >= 0:
            return False
        drop = abs(change.delta)
        pct = change.drop_pct or 0.0
        return (
            drop >= self.criteria.price_drop_min_dollars
            and pct >= self.criteria.price_drop_min_pct
        )

    # -------------------------------------------------------------- dispatch
    def dispatch(self, events: list[AlertEvent]) -> list[SendResult]:
        """Send a run's alerts and record what happened."""
        if not events:
            log.info("no alerts to send")
            return []

        # Price drops first, then by score -- if the cap bites, the most
        # actionable items are the ones that survive.
        ordered = sorted(
            events, key=lambda e: (e.kind != "price_drop", -e.score)
        )
        capped = ordered[: self.criteria.max_alerts_per_run]
        if len(ordered) > len(capped):
            log.warning(
                "alert cap reached: sending %d of %d (raise pipeline.alerts.max_alerts_per_run)",
                len(capped), len(ordered),
            )

        channels = self.active_channels
        if not channels:
            log.warning("no alert channel configured; %d match(es) recorded only in SQLite",
                        len(capped))
            return []

        if self.settings.dry_run:
            log.info("DRY RUN: would send %d alert(s) to %s",
                     len(capped), ", ".join(c.name for c in channels))
            for event in capped:
                content = event.to_content()
                log.info("  [%s] %s — %s", event.kind, content.headline, content.subheadline)
            return []

        contents = [event.to_content() for event in capped]
        results: list[SendResult] = []
        for channel in channels:
            result = channel.send(contents)
            results.append(result)
            log.info("alerts via %s: %s (%s)", channel.name,
                     "ok" if result.ok else "FAILED", result.detail)
            for event in capped:
                self.db.record_alert(
                    event.dedupe_key, event.kind, channel.name,
                    event.listing.price, result.ok, result.detail,
                )
        return results

    def send_test(self) -> list[SendResult]:
        """Fire a synthetic alert through every configured channel."""
        listing = Listing(
            source="selftest",
            source_id="test-1",
            url="https://www.zillow.com/homedetails/example_zpid/",
            address="9705 Collins Ave #1502N, Bal Harbour, FL 33154",
            beds=2, baths=2.5, sqft=1450, year_built=2018, price=9500,
            latitude=25.8890, longitude=-80.1233,
            property_type="Condo",
            broker="Example Realty",
            title="Oceanfront 2BR at Bal Harbour",
            description=(
                "Test alert from the Miami condo monitor. Annual lease, 6-12 months. "
                "Valet parking, concierge, oceanfront pool, spa, fitness center, beach service."
            ),
            photos=["https://images.unsplash.com/photo-1512917774080-9991f1c4c750?w=1200"],
        )
        from ..filters import evaluate  # local import avoids a cycle at module load
        result = evaluate(listing, self.settings.search)
        event = AlertEvent(
            listing=listing, kind="new", dedupe_key=listing.dedupe_key,
            score=result.score, warnings=result.warnings,
        )
        channels = self.active_channels
        if not channels:
            log.error("no alert channel configured -- set DISCORD_WEBHOOK_URL, "
                      "SLACK_WEBHOOK_URL or the SMTP_* variables")
            return []
        return [channel.send([event.to_content()]) for channel in channels]
