"""End-to-end run: ingest -> dedupe -> filter -> enrich -> persist -> alert.

The sequencing is deliberate:

1. **Module 1** primary wrappers run first, concurrently (different hosts, so
   the per-host rate limiter is not the bottleneck).
2. **Staleness assessment** decides whether Module 2 is needed. A run where
   every primary errored, or which returned implausibly few candidates, trips
   the ScrapingBee fallback. Scraper credits are expensive, so this is a
   deliberate decision point rather than an always-on second pass.
3. **Cross-source dedupe** collapses the same unit found on multiple portals
   before any metered enrichment call is made -- deduplicating afterwards would
   mean paying for the same AVM two or three times.
4. **Filter, enrich, then filter again.** The county lookup can change square
   footage and year built, which are hard constraints, so the verdict is not
   final until after enrichment.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import timedelta

from .alerts.dispatcher import AlertDispatcher, AlertEvent
from .config import Settings
from .db import ChangeSet, Database
from .enrich.enricher import Enricher
from .filters import FilterStats, evaluate
from .models import Listing, MatchResult, utcnow
from .sources.base import ListingSource, SourceResult
from .sources.craigslist import CraigslistSource
from .sources.rapidapi import build_rapidapi_sources
from .sources.realtyapi import RealtyApiSource
from .sources.rentcast_listings import RentCastListingsSource
from .sources.scrapingbee import ScrapingBeeSource
from .util.http import HttpClient
from .util.logging import get_logger

log = get_logger(__name__)

#: Listings not seen for this long are marked inactive (off market).
INACTIVE_AFTER_HOURS = 72


@dataclass
class RunSummary:
    run_id: int = 0
    fetched: int = 0
    deduped: int = 0
    matched: int = 0
    new_listings: int = 0
    price_drops: int = 0
    alerts_sent: int = 0
    scraper_used: bool = False
    scraper_reason: str = ""
    marked_inactive: int = 0
    source_results: list[SourceResult] = field(default_factory=list)
    filter_stats: FilterStats = field(default_factory=FilterStats)

    def render(self) -> str:
        lines = [
            "",
            "=" * 72,
            f"RUN {self.run_id} SUMMARY",
            "=" * 72,
            f"  fetched (raw)        : {self.fetched}",
            f"  unique units         : {self.deduped}",
            f"  matched criteria     : {self.matched}",
            f"  net-new              : {self.new_listings}",
            f"  price drops          : {self.price_drops}",
            f"  alerts sent          : {self.alerts_sent}",
            f"  marked off-market    : {self.marked_inactive}",
        ]
        if self.scraper_used:
            lines.append(f"  scraper fallback     : engaged ({self.scraper_reason})")
        lines.append("")
        lines.append("  sources:")
        for result in self.source_results:
            lines.append(f"    - {result.summary()}")
            for note in result.notes:
                lines.append(f"        note: {note}")
            for error in result.errors[:3]:
                lines.append(f"        error: {error}")
        if self.filter_stats.by_reason:
            lines.append("")
            lines.append("  top rejection reasons:")
            for reason, count in self.filter_stats.top_reasons():
                lines.append(f"    - {reason}: {count}")
        lines.append("=" * 72)
        return "\n".join(lines)


class Pipeline:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.http = HttpClient(
            timeout=settings.http.timeout,
            max_retries=settings.http.max_retries,
            rate_limit_seconds=settings.http.rate_limit_seconds,
            cache_enabled=settings.http.cache_enabled,
            cache_dir=settings.http.cache_dir,
            cache_ttl_seconds=settings.http.cache_ttl_seconds,
        )
        self.db = Database(settings.database_path)
        self.enricher = Enricher(self.http, settings)
        self.dispatcher = AlertDispatcher(self.http, settings, self.db)

        self.primary_sources: list[ListingSource] = [
            *build_rapidapi_sources(self.http, settings.rapidapi),
            RealtyApiSource(self.http, settings.realtyapi),
            RentCastListingsSource(
                self.http, settings.rentcast,
                enabled=settings.rentcast.use_as_listing_source,
            ),
            CraigslistSource(
                self.http,
                enabled=settings.craigslist.enabled,
                sites=settings.craigslist.sites,
            ),
        ]
        self.fallback_source = ScrapingBeeSource(self.http, settings.scrapingbee)

    # ------------------------------------------------------------------ main
    def run(self) -> RunSummary:
        summary = RunSummary(run_id=self.db.start_run())
        criteria = self.settings.search

        # --- Module 1 --------------------------------------------------
        results = self._fetch_primary(criteria)
        summary.source_results.extend(results)
        listings = [listing for result in results for listing in result.listings]

        # --- Module 2 --------------------------------------------------
        should_scrape, reason = self._should_use_fallback(results, listings)
        if should_scrape and self.fallback_source.enabled:
            log.warning("engaging ScrapingBee fallback: %s", reason)
            summary.scraper_used = True
            summary.scraper_reason = reason
            fallback_result = self.fallback_source.fetch(criteria)
            summary.source_results.append(fallback_result)
            listings.extend(fallback_result.listings)
        elif should_scrape:
            log.warning("fallback wanted (%s) but ScrapingBee is not configured", reason)

        summary.fetched = len(listings)

        # --- Dedupe across sources -------------------------------------
        unique = self._merge_across_sources(listings)
        summary.deduped = len(unique)
        log.info("fetched %d records -> %d unique units", summary.fetched, summary.deduped)

        # --- Filter ----------------------------------------------------
        matches = [evaluate(listing, criteria) for listing in unique]
        passing = [m for m in matches if m.passed]
        log.info("%d of %d units pass the hard constraints", len(passing), len(matches))

        # --- Module 3 --------------------------------------------------
        to_enrich = (
            [m.listing for m in passing]
            if self.settings.pipeline.enrichment.only_matched
            else unique
        )
        if self.settings.pipeline.enrichment.enabled and to_enrich:
            self.enricher.enrich_all(to_enrich)
            # County data can change sqft/year built, which are hard constraints.
            matches = [evaluate(listing, criteria) for listing in unique]
            passing = [m for m in matches if m.passed]
            log.info("%d units pass after enrichment", len(passing))

        for match in matches:
            summary.filter_stats.record(match)
        summary.matched = len(passing)

        # --- Persist + decide what is newsworthy -----------------------
        events = self._persist(matches, summary)

        # --- Alert -----------------------------------------------------
        send_results = self.dispatcher.dispatch(events)
        summary.alerts_sent = len(events) if any(r.ok for r in send_results) else 0

        summary.marked_inactive = self.db.mark_inactive_before(
            utcnow() - timedelta(hours=INACTIVE_AFTER_HOURS)
        )

        self.db.finish_run(
            summary.run_id,
            fetched=summary.fetched,
            deduped=summary.deduped,
            matched=summary.matched,
            new_listings=summary.new_listings,
            price_drops=summary.price_drops,
            alerts_sent=summary.alerts_sent,
            source_health=json.dumps(
                {r.source: {"count": r.count, "ok": r.ok, "errors": r.errors[:3]}
                 for r in summary.source_results}
            ),
        )
        return summary

    # ------------------------------------------------------------- stages
    def _fetch_primary(self, criteria) -> list[SourceResult]:
        """Run every configured primary source, concurrently."""
        active = [source for source in self.primary_sources if source.enabled]
        if not active:
            log.warning("no primary listing source is configured")
            return [source.fetch(criteria) for source in self.primary_sources]

        results: list[SourceResult] = []
        with ThreadPoolExecutor(max_workers=min(4, len(active))) as pool:
            futures = {pool.submit(source.fetch, criteria): source for source in active}
            for future in as_completed(futures):
                source = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:  # fetch() already traps, but be certain
                    log.exception("%s crashed", source.name)
                    results.append(
                        SourceResult(source=source.name, errors=[f"crashed: {exc}"])
                    )

        # Report skipped sources too, so the summary shows what was not tried.
        for source in self.primary_sources:
            if not source.enabled:
                results.append(
                    SourceResult(source=source.name, skipped=True, skip_reason="not configured")
                )
        return results

    def _should_use_fallback(
        self, results: list[SourceResult], listings: list[Listing]
    ) -> tuple[bool, str]:
        """Decide whether the primary APIs are healthy enough to trust."""
        pipeline = self.settings.pipeline
        if pipeline.always_run_scraper:
            return True, "always_run_scraper is enabled"

        attempted = [r for r in results if not r.skipped]
        if not attempted:
            return True, "no primary source was configured"

        failed = [r for r in attempted if not r.ok]
        error_ratio = len(failed) / len(attempted)
        if error_ratio >= pipeline.staleness_error_ratio:
            names = ", ".join(r.source for r in failed)
            return True, f"{len(failed)}/{len(attempted)} primary sources failed ({names})"

        if len(listings) < pipeline.staleness_min_results:
            return True, (
                f"only {len(listings)} candidates from primary APIs "
                f"(expected at least {pipeline.staleness_min_results})"
            )
        return False, ""

    @staticmethod
    def _merge_across_sources(listings: list[Listing]) -> list[Listing]:
        """Collapse the same unit seen on multiple portals into one record."""
        merged: dict[str, Listing] = {}
        order: list[str] = []
        for listing in listings:
            key = listing.dedupe_key
            if key in merged:
                merged[key].merge_from(listing)
            else:
                merged[key] = listing
                order.append(key)
        return [merged[key] for key in order]

    def _persist(self, matches: list[MatchResult], summary: RunSummary) -> list[AlertEvent]:
        """Write every unit, and collect the alert-worthy changes."""
        events: list[AlertEvent] = []
        for match in matches:
            change: ChangeSet = self.db.upsert(
                match.listing,
                matched=match.passed,
                score=match.score,
                warnings=match.warnings,
            )
            if match.passed and change.is_new:
                summary.new_listings += 1
            if match.passed and change.price_drop:
                summary.price_drops += 1

            event = self.dispatcher.build_event(match, change)
            if event is not None:
                events.append(event)
        return events

    # ------------------------------------------------------------- lifecycle
    def close(self) -> None:
        self.http.close()
        self.db.close()

    def __enter__(self) -> Pipeline:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
