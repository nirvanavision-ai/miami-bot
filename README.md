# Miami Coastal Condo Rental Monitor

A rental-listing pipeline for oceanfront and near-ocean luxury condos in
**Miami Beach, Surfside, Bal Harbour, Bay Harbor Islands and Sunny Isles Beach**.

It reaches active inventory **without MLS or IDX broker credentials** by
combining three independent ingestion strategies, deduplicating the same unit
across portals, validating the physical specs against county records, and
alerting only on genuinely new listings and real price drops.

---

## What it enforces

| Constraint | Rule |
|---|---|
| Location | Miami Beach · Surfside · Bal Harbour · Bay Harbor Islands · Sunny Isles Beach |
| Ocean proximity | ≤ 0.6 mi from the Atlantic shoreline (measured, not inferred) |
| Property type | Condo / condominium / co-op only — houses and townhouses rejected |
| Layout | ≥ 2 beds, ≥ 2 baths, ≥ 1,200 sq ft |
| Budget | $5,000 – $12,000 per month |
| Lease term | **6–12 months strictly** |
| Excluded | short term, vacation, seasonal, daily, weekly, monthly, Airbnb, VRBO, month-to-month … |
| Building | Built 2005+ **or** described as renovated, **and** ≥ 3 luxury amenities |

All of it lives in [`config.yaml`](config.yaml). Tuning the search never means
editing Python.

---

## Architecture

```
                    ┌──────────────── MODULE 1: primary ingestion ────────────────┐
                    │  RapidAPI/Zillow    RapidAPI/Realtor    RapidAPI/Redfin     │
                    │  RealtyAPI (generic, endpoint-configurable)                 │
                    └───────────────────────────┬─────────────────────────────────┘
                                                │
                              health + staleness assessment
                                                │
                    ┌───────────────────────────▼─────────────────────────────────┐
                    │  MODULE 2: ScrapingBee fallback (headless + stealth proxy)   │
                    │  embedded state JSON → JSON-LD → server-side CSS extraction  │
                    └───────────────────────────┬─────────────────────────────────┘
                                                │
                         normalization → single internal schema
                                                │
                       cross-portal dedupe (address / geo / unit hash)
                                                │
                              hard-constraint filtering
                                                │
                    ┌───────────────────────────▼─────────────────────────────────┐
                    │  MODULE 3: enrichment & validation                          │
                    │  Miami-Dade PA / GIS  →  authoritative sqft + year built     │
                    │  RentCast / HouseCanary → rent AVM, value AVM, gross yield   │
                    └───────────────────────────┬─────────────────────────────────┘
                                                │
                          re-filter (county data can reverse a match)
                                                │
                     SQLite: listings · sightings · price_history · alerts
                                                │
                        Discord  ·  Slack  ·  SMTP email digest
```

### Module 1 — primary active-listing ingestion

Portal wrappers on RapidAPI re-publish consumer-portal search results, which is
what makes MLS-free ingestion possible. Two properties of this layer matter:

* **Two-phase fetching.** The search endpoint is cheap and returns numeric
  specs; the detail endpoint is expensive and is the only place the description
  and lease term live. The pipeline searches, discards anything that already
  fails a numeric constraint, then spends detail calls only on survivors — capped
  by `max_detail_lookups`.
* **Declarative field maps.** Every field is a *list* of candidate JSON paths,
  plus a structural fallback that finds the results array by shape when every
  declared path misses. A wrapper renaming a field degrades one field instead of
  returning zero listings.

`RealtyApiSource` is fully endpoint-configurable (base URL, path, and three auth
styles), so pointing it at a different aggregator is an `.env` change.

### Module 2 — anti-bot scraper fallback

ScrapingBee runs only when the primaries are actually in trouble, because
premium/stealth proxy calls cost real credits. It engages when:

* a configured share of primary sources errored (`staleness_error_ratio`), or
* the primaries returned fewer than `staleness_min_results` candidates, or
* `always_run_scraper` is set.

Extraction prefers the portal's own embedded state JSON (`__NEXT_DATA__`,
`__INITIAL_STATE__`, `__reactServerState`) — the same shape the portal's API
returns, so the Module 1 field maps are reused verbatim. It falls back to JSON-LD,
then to ScrapingBee's server-side `extract_rules`. Every request counts against
`SCRAPINGBEE_MAX_REQUESTS_PER_RUN`.

### Module 3 — enrichment & validation

* **Miami-Dade County** (public, no key): the Property Appraiser service proxy,
  with the ArcGIS parcel layer as a fallback. This is the authoritative answer to
  *"is it really 1,200+ sq ft, and when was the building actually built?"* —
  listings inflate square footage routinely. **When the county disagrees with the
  listing, the county wins, and the listing is re-filtered.** A unit advertised
  at 1,650 sq ft that the county records as 1,180 gets dropped, not alerted.
* **RentCast / HouseCanary**: long-term rent AVM and sale-value AVM, producing a
  price-to-estimate ratio, a below/at/above-market verdict, and a gross-yield
  figure. Unit specs are passed with the request — otherwise the AVM prices the
  *building*, not the unit.

### Deduplication

The same Bal Harbour unit appears as `9705 Collins Avenue #1502N`,
`9705 Collins Ave APT 1502-N, 33154-2932`, and `9705 COLLINS AVE UNIT 1502 N`.
Addresses are decomposed into canonical components (USPS suffixes, directionals,
unit designators, ZIP+4 collapse) and hashed. Matching is tried in confidence
order:

1. exact unit hash (normalized street + unit + ZIP),
2. geo hash (coordinates rounded to ~11 m + unit),
3. building hash + unit + bed count,
4. same provider + provider id.

`PH-3` stays distinct from `#3`; `TH2` and `TH-2` collapse. Merging is
non-destructive: a thin sighting never erases specs a richer portal supplied, and
the lower advertised rent wins.

### Alerting

Only net-new units and material price drops alert. A drop must clear **both** a
dollar floor and a percentage floor, so a $25 nudge on a $10,000 rental stays
quiet. Per-unit cooldowns survive restarts, runs are capped, and when the cap
bites, price drops and the highest-scoring listings survive.

Each alert carries the primary photo, price, specs, **parsed lease terms**, ocean
distance, amenity list, AVM comparison, county-record cross-check and the source
URL.

---

## Quick start

```bash
git clone https://github.com/nirvanavision-ai/miami-bot.git
cd miami-bot

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
$EDITOR .env                    # add the keys you have; everything is optional

python main.py check            # validate config, see what will run
python main.py run --dry-run    # full pipeline, no alerts sent
python main.py run              # for real
python main.py watch            # daemon on POLL_INTERVAL_MINUTES
```

Nothing is mandatory. Each provider switches on when its key is present, and
`check` tells you exactly what is off and what that costs you.

### Commands

| Command | Purpose |
|---|---|
| `check` | Validate configuration, print criteria and provider status |
| `run` | One pipeline pass (`--dry-run` to skip alerts) |
| `watch` | Run forever on `POLL_INTERVAL_MINUTES`, handling SIGINT/SIGTERM cleanly |
| `stats` | Database summary, including cross-portal duplicate count |
| `list --limit N` | Current matches, best first |
| `test-alert` | Send a synthetic alert through every configured channel |
| `export out.csv` / `out.json` | Dump matches for a spreadsheet |

---

## Configuration

Two files, with a strict split:

* **`.env`** — secrets and endpoints. See [`.env.example`](.env.example) for
  every supported key, grouped by module.
* **`config.yaml`** — search criteria, staleness thresholds, enrichment and
  alerting behaviour.

### Required keys by module

| Module | Keys | Without them |
|---|---|---|
| 1 — RapidAPI | `RAPIDAPI_KEY` (+ optional host overrides) | wrapper sources skip |
| 1 — RealtyAPI | `REALTYAPI_KEY`, `REALTYAPI_BASE_URL` | aggregator skips |
| 2 — ScrapingBee | `SCRAPINGBEE_API_KEY` | no fallback when primaries fail |
| 3 — RentCast | `RENTCAST_API_KEY` | no rent AVM |
| 3 — HouseCanary | `HOUSECANARY_API_KEY`, `HOUSECANARY_API_SECRET` | no second AVM |
| 3 — Miami-Dade | *(none — public data)* | — |
| Alerts | `DISCORD_WEBHOOK_URL` / `SLACK_WEBHOOK_URL` / `SMTP_*` | matches stay in SQLite |

### Cost control

* `SCRAPINGBEE_MAX_REQUESTS_PER_RUN` — hard ceiling on scraper credits per run.
* `HTTP_CACHE_ENABLED=true` — cache raw responses on disk while developing so you
  are not re-billed for the same query.
* `pipeline.enrichment.only_matched: true` — never spend an AVM call on a listing
  that already failed the filters.
* `HTTP_RATE_LIMIT_SECONDS` — polite per-host floor between calls.

---

## Data model

```
listings       one row per unit (canonical), keyed by dedupe_key
sightings      one row per (unit, source) — proves a unit is on N portals
price_history  append-only log of every price change
alerts         what was sent, when, on which channel — powers the cooldown
runs           per-run telemetry and source health
```

---

## Development

```bash
pip install -r requirements-dev.txt
pytest                                     # 266 tests
pytest -q --cov=miami_bot --cov-report=term-missing
ruff check .
```

A single-listing tuning aid is included, for when a run returns nothing and you
need to know which constraint is doing the rejecting:

```bash
python scripts/explain_listing.py \
    --address "9705 Collins Ave #1502N, Bal Harbour, FL 33154" \
    --price 9500 --beds 2 --baths 2.5 --sqft 1450 --year 2018 \
    --lat 25.8890 --lon -80.1233 \
    --description "Annual lease. Valet, concierge, pool, spa."
```

It prints the parsed lease term with its evidence, the measured ocean distance,
the detected amenities, and every failure and warning by name.

The suite covers address normalization and dedupe collapse, lease-term parsing
(digits *and* spelled-out numbers), negation-aware keyword screening, ocean
distance against real oceanfront towers, every hard constraint, adapter field
mapping against realistic payloads, provider outages, the staleness decision
matrix, alert gating, and end-to-end pipeline runs with mocked HTTP.

### Extending

Adding a source means subclassing `ListingSource`, declaring a `FieldMap`, and
registering it — everything downstream already speaks the internal schema.

```python
class MySource(ListingSource):
    name = "mysource"
    field_map = FieldMap(address=("addr.line",), price=("rent",), ...)

    @property
    def enabled(self) -> bool:
        return bool(self.settings.api_key)

    def _fetch(self, criteria, result):
        ...
```

Adding an alert channel means subclassing `AlertChannel` and implementing
`send(contents)`; `AlertContent` is already assembled for you.

---

## Operational notes and limitations

* **Third-party wrapper APIs are unstable by nature.** The tolerant field maps
  and structural fallback are designed for that, but if a wrapper changes
  fundamentally you will see it in the run summary as a source returning zero
  with no errors. `check` and the per-run source health are the place to look.
* **Ocean distance is an approximation.** The shoreline is a ~100 m-accurate
  polyline (`miami_bot/geo.py`) — ample to separate an oceanfront tower from a
  bayside one, not a survey instrument.
* **Lease terms are parsed from prose.** The parser reports its evidence and the
  alert shows the parsed window, but a term inferred from marketing copy should
  still be confirmed with the agent before signing.
* **Keyword exclusion is deliberately strict.** A listing that says "vacation" is
  dropped even if it also says "annual lease", per the brief. Negated mentions
  ("NO short-term rentals") are correctly kept. Tune the list in `config.yaml`.
* **Respect each provider's terms of service and robots policy.** ScrapingBee is
  configured as a fallback, rate-limited and credit-capped, rather than a
  continuous crawler.
