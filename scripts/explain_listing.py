#!/usr/bin/env python3
"""Evaluate a single listing against the criteria and explain the verdict.

A tuning aid: when the pipeline returns nothing, this shows exactly which
constraint is doing the rejecting, on a listing you construct by hand.

    python scripts/explain_listing.py \
        --address "9705 Collins Ave #1502N, Bal Harbour, FL 33154" \
        --price 9500 --beds 2 --baths 2.5 --sqft 1450 --year 2018 \
        --lat 25.8890 --lon -80.1233 \
        --description "Annual lease. Valet, concierge, pool, spa."

Or pipe a provider payload straight in:

    cat payload.json | python scripts/explain_listing.py --json -
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from miami_bot.config import Settings  # noqa: E402
from miami_bot.filters import evaluate  # noqa: E402
from miami_bot.models import Listing  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--json", help="path to a JSON listing, or '-' for stdin")
    parser.add_argument("--address", default="")
    parser.add_argument("--unit", default="")
    parser.add_argument("--price", type=int)
    parser.add_argument("--beds", type=float)
    parser.add_argument("--baths", type=float)
    parser.add_argument("--sqft", type=int)
    parser.add_argument("--year", type=int, dest="year_built")
    parser.add_argument("--lat", type=float, dest="latitude")
    parser.add_argument("--lon", type=float, dest="longitude")
    parser.add_argument("--type", default="Condo", dest="property_type")
    parser.add_argument("--description", default="")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    settings = Settings.load(args.config, env_file=None)

    if args.json:
        raw = sys.stdin.read() if args.json == "-" else Path(args.json).read_text()
        payload = json.loads(raw)
        known = set(Listing.__dataclass_fields__)
        listing = Listing(**{"source": "manual",
                             **{k: v for k, v in payload.items() if k in known}})
    else:
        listing = Listing(
            source="manual",
            address=args.address,
            unit=args.unit,
            price=args.price,
            beds=args.beds,
            baths=args.baths,
            sqft=args.sqft,
            year_built=args.year_built,
            latitude=args.latitude,
            longitude=args.longitude,
            property_type=args.property_type,
            description=args.description,
        )

    result = evaluate(listing, settings.search)

    print(f"\n{listing.summary()}")
    print(f"  dedupe key : {listing.dedupe_key}")
    print(f"  ocean      : {listing.miles_from_ocean if listing.miles_from_ocean is None else f'{listing.miles_from_ocean:.3f} mi'}")
    term = listing.lease_term
    print(f"  lease term : {term.describe()}  evidence={term.evidence}")
    print(f"  amenities  : {sorted(listing.amenity_matches) or 'none detected'}")
    print(f"\n  VERDICT    : {'PASS' if result.passed else 'FAIL'}  (score {result.score}/100)")
    for failure in result.failures:
        print(f"    ✗ {failure}")
    for warning in result.warnings:
        print(f"    ! {warning}")
    print()
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
