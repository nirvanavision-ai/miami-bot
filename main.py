#!/usr/bin/env python3
"""Miami coastal luxury condo rental monitor -- command line entry point.

    python main.py check                # validate config, show what will run
    python main.py run                  # one pipeline pass
    python main.py run --dry-run        # everything except sending alerts
    python main.py watch                # run forever on POLL_INTERVAL_MINUTES
    python main.py stats                # what is in the database
    python main.py list --limit 20      # current matches, best first
    python main.py test-alert           # prove the alert channels work
    python main.py export matches.json  # dump matches for spreadsheeting
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from miami_bot import __version__
from miami_bot.config import ConfigError, Settings
from miami_bot.db import Database
from miami_bot.pipeline import Pipeline
from miami_bot.util.logging import get_logger, setup_logging

log = get_logger("miami_bot.cli")

_STOPPING = False


def _handle_signal(signum: int, _frame: object) -> None:
    global _STOPPING
    _STOPPING = True
    log.warning("received signal %s; finishing the current run then stopping", signum)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def command_check(settings: Settings) -> int:
    print(f"\nMiami condo monitor v{__version__}\n")
    print("Search criteria")
    search = settings.search
    print(f"  cities            : {', '.join(search.cities)}")
    print(f"  ZIP codes         : {', '.join(search.zip_codes)}")
    print(f"  max ocean distance: {search.max_miles_from_ocean} mi")
    print(f"  budget            : ${search.min_price:,} - ${search.max_price:,} / month")
    print(f"  minimum layout    : {search.min_beds:g} bd / {search.min_baths:g} ba / "
          f"{search.min_sqft:,} sqft")
    print(f"  lease term        : {search.min_lease_months}-{search.max_lease_months} months")
    print(f"  excluded keywords : {len(search.excluded_keywords)} configured")
    print(f"  property types    : {', '.join(search.property_types)}")
    print(f"  building          : built {search.building.min_year_built}+ "
          f"(or renovated), {search.building.min_amenity_matches}+ luxury amenities")
    if search.building.required_amenities:
        required = ", ".join(a.replace("_", " ") for a in search.building.required_amenities)
        print(f"  required amenities: {required}")

    print("\nProviders")
    for name, enabled in settings.provider_status().items():
        print(f"  {'ON ' if enabled else 'off'}  {name}")

    warnings = settings.warnings()
    if warnings:
        print("\nWarnings")
        for warning in warnings:
            print(f"  ! {warning}")

    print(f"\nDatabase: {settings.database_path}")
    print(f"Dry run : {settings.dry_run}\n")
    return 0


def command_run(settings: Settings) -> int:
    with Pipeline(settings) as pipeline:
        summary = pipeline.run()
        print(summary.render())
    return 0


def command_watch(settings: Settings) -> int:
    interval = max(1, settings.poll_interval_minutes) * 60
    log.info("watching every %d minutes; Ctrl-C to stop", settings.poll_interval_minutes)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    while not _STOPPING:
        started = time.monotonic()
        try:
            with Pipeline(settings) as pipeline:
                summary = pipeline.run()
                print(summary.render())
        except Exception:
            log.exception("run failed; continuing to the next interval")

        if _STOPPING:
            break
        elapsed = time.monotonic() - started
        sleep_for = max(0.0, interval - elapsed)
        log.info("next run in %.0f minutes", sleep_for / 60)
        # Sleep in slices so a signal is noticed promptly.
        while sleep_for > 0 and not _STOPPING:
            nap = min(5.0, sleep_for)
            time.sleep(nap)
            sleep_for -= nap

    log.info("stopped")
    return 0


def command_stats(settings: Settings) -> int:
    with Database(settings.database_path) as db:
        stats = db.stats()
    print(f"\nDatabase: {settings.database_path}")
    width = max(len(k) for k in stats)
    for key, value in stats.items():
        print(f"  {key.replace('_', ' '):<{width}} : {value}")
    print()
    return 0


def command_list(settings: Settings, limit: int) -> int:
    with Database(settings.database_path) as db:
        listings = db.matched_listings(limit=limit)
    if not listings:
        print("\nNo current matches. Run `python main.py run` first.\n")
        return 0

    print(f"\n{len(listings)} current match(es), best first:\n")
    for index, listing in enumerate(listings, 1):
        print(f"{index:3}. {listing.summary()}")
        extras = []
        if listing.miles_from_ocean is not None:
            extras.append(f"{listing.miles_from_ocean:.2f} mi to ocean")
        if listing.year_built:
            extras.append(f"built {listing.year_built}")
        if listing.enrichment.rent_estimate:
            extras.append(
                f"AVM ${listing.enrichment.rent_estimate:,}"
                f"{' (' + listing.enrichment.verdict + ')' if listing.enrichment.verdict else ''}"
            )
        if extras:
            print(f"     {' · '.join(extras)}")
        if listing.url:
            print(f"     {listing.url}")
    print()
    return 0


def command_test_alert(settings: Settings) -> int:
    with Pipeline(settings) as pipeline:
        results = pipeline.dispatcher.send_test()
    if not results:
        print("\nNo alert channel is configured. Set DISCORD_WEBHOOK_URL, "
              "SLACK_WEBHOOK_URL or the SMTP_* variables in .env\n")
        return 1
    failed = 0
    for result in results:
        status = "ok" if result.ok else "FAILED"
        print(f"  {result.channel:<8} {status}  {result.detail}")
        failed += 0 if result.ok else 1
    return 1 if failed else 0


def command_export(settings: Settings, destination: str, limit: int) -> int:
    with Database(settings.database_path) as db:
        listings = db.matched_listings(limit=limit)

    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for listing in listings:
        rows.append({
            "address": listing.display_address,
            "price": listing.price,
            "beds": listing.beds,
            "baths": listing.baths,
            "sqft": listing.sqft,
            "year_built": listing.year_built,
            "lease_term": listing.lease_term.describe() if listing.lease_term else "",
            "miles_from_ocean": listing.miles_from_ocean,
            "amenities": "; ".join(sorted(listing.amenity_matches)),
            "rent_estimate": listing.enrichment.rent_estimate,
            "avm_verdict": listing.enrichment.verdict,
            "county_sqft": listing.enrichment.county_sqft,
            "source": listing.source,
            "url": listing.url,
        })

    if path.suffix.lower() == ".csv":
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else ["address"])
            writer.writeheader()
            writer.writerows(rows)
    else:
        path.write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")

    print(f"\nWrote {len(rows)} listing(s) to {path}\n")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="miami-bot",
        description="Monitor coastal Miami luxury condo rentals (6-12 month leases).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", default=None, help="path to config.yaml")
    parser.add_argument("--env-file", default=".env", help="path to the .env file")
    parser.add_argument("--log-level", default=None,
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("check", help="validate configuration and show what will run")

    run_parser = sub.add_parser("run", help="execute one pipeline pass")
    run_parser.add_argument("--dry-run", action="store_true",
                            help="do everything except sending alerts")

    sub.add_parser("watch", help="run continuously on POLL_INTERVAL_MINUTES")
    sub.add_parser("stats", help="summarise the database")
    sub.add_parser("test-alert", help="send a synthetic alert to every channel")

    list_parser = sub.add_parser("list", help="show current matches")
    list_parser.add_argument("--limit", type=int, default=25)

    export_parser = sub.add_parser("export", help="write matches to .json or .csv")
    export_parser.add_argument("destination")
    export_parser.add_argument("--limit", type=int, default=500)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "run"

    if getattr(args, "dry_run", False):
        os.environ["DRY_RUN"] = "true"
    if args.log_level:
        os.environ["LOG_LEVEL"] = args.log_level

    try:
        settings = Settings.load(args.config, env_file=args.env_file)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    setup_logging(settings.log_level, settings.log_file)

    if command in {"run", "watch"}:
        for warning in settings.warnings():
            log.warning("%s", warning)

    handlers = {
        "check": lambda: command_check(settings),
        "run": lambda: command_run(settings),
        "watch": lambda: command_watch(settings),
        "stats": lambda: command_stats(settings),
        "list": lambda: command_list(settings, args.limit),
        "test-alert": lambda: command_test_alert(settings),
        "export": lambda: command_export(settings, args.destination, args.limit),
    }
    handler = handlers.get(command)
    if handler is None:
        parser.print_help()
        return 2

    try:
        return handler()
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
