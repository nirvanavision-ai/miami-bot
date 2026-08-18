"""SQLite persistence, cross-portal deduplication and change detection.

Three tables carry the state:

``listings``
    One row per *unit* (not per portal sighting), keyed by ``dedupe_key``. This
    is the canonical record and the price shown in alerts.
``sightings``
    One row per (unit, source). Records that Zillow and Redfin both carry the
    unit, with each portal's own id, URL and price.
``price_history``
    Append-only log of every price change, which is what makes "price drop"
    alerts possible without re-alerting on noise.
``alerts``
    What we have already told the user, so a restart never re-spams them.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Listing, utcnow
from .util.logging import get_logger

log = get_logger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS listings (
    dedupe_key       TEXT PRIMARY KEY,
    unit_hash        TEXT,
    building_hash    TEXT,
    geo_hash         TEXT,
    source           TEXT NOT NULL,
    source_id        TEXT,
    url              TEXT,
    address          TEXT,
    raw_address      TEXT,
    unit             TEXT,
    city             TEXT,
    state            TEXT,
    zip_code         TEXT,
    latitude         REAL,
    longitude        REAL,
    property_type    TEXT,
    beds             REAL,
    baths            REAL,
    sqft             INTEGER,
    year_built       INTEGER,
    price            INTEGER,
    available_date   TEXT,
    days_on_market   INTEGER,
    status           TEXT,
    title            TEXT,
    description      TEXT,
    amenities        TEXT,
    photos           TEXT,
    broker           TEXT,
    lease_min_months INTEGER,
    lease_max_months INTEGER,
    lease_term_text  TEXT,
    miles_from_ocean REAL,
    is_oceanfront    INTEGER DEFAULT 0,
    amenity_matches  TEXT,
    enrichment       TEXT,
    raw              TEXT,
    matched          INTEGER DEFAULT 0,
    match_score      REAL DEFAULT 0,
    match_warnings   TEXT,
    first_seen_at    TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    fetched_at       TEXT,
    times_seen       INTEGER DEFAULT 1,
    is_active        INTEGER DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_listings_building ON listings(building_hash);
CREATE INDEX IF NOT EXISTS idx_listings_geo      ON listings(geo_hash);
CREATE INDEX IF NOT EXISTS idx_listings_active   ON listings(is_active, matched);
CREATE INDEX IF NOT EXISTS idx_listings_lastseen ON listings(last_seen_at);

CREATE TABLE IF NOT EXISTS sightings (
    source_key    TEXT PRIMARY KEY,
    dedupe_key    TEXT NOT NULL,
    source        TEXT NOT NULL,
    source_id     TEXT,
    url           TEXT,
    price         INTEGER,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    FOREIGN KEY (dedupe_key) REFERENCES listings(dedupe_key) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sightings_dedupe ON sightings(dedupe_key);

CREATE TABLE IF NOT EXISTS price_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key  TEXT NOT NULL,
    old_price   INTEGER,
    new_price   INTEGER NOT NULL,
    delta       INTEGER,
    changed_at  TEXT NOT NULL,
    source      TEXT,
    FOREIGN KEY (dedupe_key) REFERENCES listings(dedupe_key) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_price_history_key ON price_history(dedupe_key, changed_at);

CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key  TEXT NOT NULL,
    kind        TEXT NOT NULL,
    channel     TEXT NOT NULL,
    price       INTEGER,
    sent_at     TEXT NOT NULL,
    ok          INTEGER DEFAULT 1,
    detail      TEXT
);

CREATE INDEX IF NOT EXISTS idx_alerts_key ON alerts(dedupe_key, sent_at);

CREATE TABLE IF NOT EXISTS runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    fetched        INTEGER DEFAULT 0,
    deduped        INTEGER DEFAULT 0,
    matched        INTEGER DEFAULT 0,
    new_listings   INTEGER DEFAULT 0,
    price_drops    INTEGER DEFAULT 0,
    alerts_sent    INTEGER DEFAULT 0,
    source_health  TEXT,
    notes          TEXT
);
"""


@dataclass
class ChangeSet:
    """What upserting a listing actually changed."""

    dedupe_key: str
    is_new: bool = False
    price_drop: bool = False
    price_increase: bool = False
    old_price: int | None = None
    new_price: int | None = None
    merged_with_existing: bool = False

    @property
    def delta(self) -> int | None:
        if self.old_price is None or self.new_price is None:
            return None
        return self.new_price - self.old_price

    @property
    def drop_pct(self) -> float | None:
        if not self.old_price or self.delta is None:
            return None
        return abs(self.delta) / self.old_price


class Database:
    """Thin, explicit SQLite wrapper. No ORM, no magic."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.path), timeout=30.0)
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(_SCHEMA)
        self._connection.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._connection.commit()
        # Listing.to_row() carries a few fields that belong to other tables
        # (source_key -> sightings). Bind only real columns.
        self._listing_columns = {
            row[1] for row in self._connection.execute("PRAGMA table_info(listings)")
        }

    # ------------------------------------------------------------------ infra
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._connection
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------- dedupe
    def find_existing_key(self, listing: Listing) -> str | None:
        """Locate an existing record for this unit across identity strategies.

        Tried in order of confidence:

        1. exact ``dedupe_key`` (same normalized address + unit),
        2. ``geo_hash`` (same coordinates + unit) -- catches a portal whose
           street text is mangled but whose pin is right,
        3. same ``building_hash`` + same unit + same bed/bath -- catches a
           portal that reports the unit only in the title.
        """
        cursor = self._connection.execute(
            "SELECT dedupe_key FROM listings WHERE dedupe_key = ?", (listing.dedupe_key,)
        )
        row = cursor.fetchone()
        if row:
            return row["dedupe_key"]

        if listing.geo_hash:
            row = self._connection.execute(
                "SELECT dedupe_key FROM listings WHERE geo_hash = ? AND geo_hash != ''",
                (listing.geo_hash,),
            ).fetchone()
            if row:
                return row["dedupe_key"]

        if listing.building_hash and listing.unit:
            row = self._connection.execute(
                """
                SELECT dedupe_key FROM listings
                 WHERE building_hash = ? AND unit = ?
                   AND (beds IS NULL OR ? IS NULL OR beds = ?)
                 LIMIT 1
                """,
                (listing.building_hash, listing.unit, listing.beds, listing.beds),
            ).fetchone()
            if row:
                return row["dedupe_key"]

        # Same source, same provider id -- the address changed but it is the
        # same advertisement (portals do correct their own typos).
        if listing.source_id:
            row = self._connection.execute(
                "SELECT dedupe_key FROM sightings WHERE source = ? AND source_id = ?",
                (listing.source, listing.source_id),
            ).fetchone()
            if row:
                return row["dedupe_key"]

        return None

    def get_listing(self, dedupe_key: str) -> Listing | None:
        row = self._connection.execute(
            "SELECT * FROM listings WHERE dedupe_key = ?", (dedupe_key,)
        ).fetchone()
        return Listing.from_row(dict(row)) if row else None

    # ------------------------------------------------------------- mutation
    def upsert(
        self,
        listing: Listing,
        *,
        matched: bool = False,
        score: float = 0.0,
        warnings: Iterable[str] = (),
    ) -> ChangeSet:
        """Insert or update a unit, returning what changed.

        Alerting decisions are made downstream from the returned ChangeSet, so
        this method never sends anything.
        """
        now = utcnow().isoformat()
        existing_key = self.find_existing_key(listing)
        row = {k: v for k, v in listing.to_row().items() if k in self._listing_columns}
        row["matched"] = int(matched)
        row["match_score"] = float(score)
        row["match_warnings"] = json.dumps(list(warnings))

        if existing_key is None:
            change = ChangeSet(
                dedupe_key=listing.dedupe_key, is_new=True, new_price=listing.price
            )
            row["first_seen_at"] = now
            row["last_seen_at"] = now
            row["times_seen"] = 1
            row["is_active"] = 1
            columns = ", ".join(row)
            placeholders = ", ".join(f":{c}" for c in row)
            with self.transaction() as connection:
                connection.execute(
                    f"INSERT INTO listings ({columns}) VALUES ({placeholders})", row
                )
                if listing.price is not None:
                    connection.execute(
                        """INSERT INTO price_history
                               (dedupe_key, old_price, new_price, delta, changed_at, source)
                           VALUES (?, NULL, ?, NULL, ?, ?)""",
                        (listing.dedupe_key, listing.price, now, listing.source),
                    )
            self._record_sighting(listing, listing.dedupe_key, now)
            return change

        previous = self._connection.execute(
            "SELECT price, matched FROM listings WHERE dedupe_key = ?", (existing_key,)
        ).fetchone()
        old_price = previous["price"] if previous else None

        change = ChangeSet(
            dedupe_key=existing_key,
            is_new=False,
            old_price=old_price,
            new_price=listing.price,
            merged_with_existing=existing_key != listing.dedupe_key,
        )
        if listing.price is not None and old_price is not None and listing.price != old_price:
            change.price_drop = listing.price < old_price
            change.price_increase = listing.price > old_price

        # Never overwrite a populated column with an empty one: a thin sighting
        # from one portal must not erase specs a richer portal already supplied.
        # SQL COALESCE only protects against NULL, so "", "[]" and "{}" -- which
        # are what an absent list or string serializes to -- are nulled first.
        updatable = {
            key: value for key, value in row.items()
            if key not in {"dedupe_key", "first_seen_at"}
        }
        for column in _NULLABLE_MERGE_COLUMNS:
            if updatable.get(column) in ("", "[]", "{}", "null"):
                updatable[column] = None
        if listing.enrichment.is_empty:
            updatable["enrichment"] = None
        assignments = ", ".join(
            f"{key} = COALESCE(:{key}, {key})" if key in _NULLABLE_MERGE_COLUMNS
            else f"{key} = :{key}"
            for key in updatable
        )
        params = dict(updatable)
        params["dedupe_key"] = existing_key
        params["last_seen_at"] = now

        with self.transaction() as connection:
            connection.execute(
                f"""UPDATE listings
                       SET {assignments},
                           last_seen_at = :last_seen_at,
                           times_seen = times_seen + 1,
                           is_active = 1
                     WHERE dedupe_key = :dedupe_key""",
                params,
            )
            if change.price_drop or change.price_increase:
                connection.execute(
                    """INSERT INTO price_history
                           (dedupe_key, old_price, new_price, delta, changed_at, source)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        existing_key, old_price, listing.price,
                        (listing.price or 0) - (old_price or 0), now, listing.source,
                    ),
                )
        self._record_sighting(listing, existing_key, now)
        return change

    def _record_sighting(self, listing: Listing, dedupe_key: str, now: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO sightings
                       (source_key, dedupe_key, source, source_id, url, price,
                        first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_key) DO UPDATE SET
                       last_seen_at = excluded.last_seen_at,
                       price        = excluded.price,
                       url          = COALESCE(excluded.url, sightings.url)
                """,
                (
                    listing.source_key, dedupe_key, listing.source, listing.source_id,
                    listing.url, listing.price, now, now,
                ),
            )

    def sightings_for(self, dedupe_key: str) -> list[sqlite3.Row]:
        return list(
            self._connection.execute(
                "SELECT * FROM sightings WHERE dedupe_key = ? ORDER BY first_seen_at",
                (dedupe_key,),
            )
        )

    def mark_inactive_before(self, cutoff: datetime) -> int:
        """Flag listings not seen since ``cutoff`` as gone from the market."""
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE listings SET is_active = 0 WHERE last_seen_at < ? AND is_active = 1",
                (cutoff.isoformat(),),
            )
        return cursor.rowcount

    # --------------------------------------------------------------- alerts
    def was_alerted_recently(self, dedupe_key: str, kind: str, hours: int) -> bool:
        cutoff = (utcnow() - timedelta(hours=hours)).isoformat()
        row = self._connection.execute(
            """SELECT 1 FROM alerts
                WHERE dedupe_key = ? AND kind = ? AND ok = 1 AND sent_at >= ?
                LIMIT 1""",
            (dedupe_key, kind, cutoff),
        ).fetchone()
        return row is not None

    def record_alert(
        self, dedupe_key: str, kind: str, channel: str,
        price: int | None, ok: bool, detail: str = "",
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO alerts (dedupe_key, kind, channel, price, sent_at, ok, detail)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (dedupe_key, kind, channel, price, utcnow().isoformat(), int(ok), detail[:1000]),
            )

    # ----------------------------------------------------------------- runs
    def start_run(self) -> int:
        with self.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO runs (started_at) VALUES (?)", (utcnow().isoformat(),)
            )
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, **fields: object) -> None:
        allowed = {
            "fetched", "deduped", "matched", "new_listings",
            "price_drops", "alerts_sent", "source_health", "notes",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        assignments = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [utcnow().isoformat(), run_id]
        with self.transaction() as connection:
            connection.execute(
                f"UPDATE runs SET {assignments + ', ' if assignments else ''}"
                "finished_at = ? WHERE id = ?",
                values,
            )

    # ----------------------------------------------------------------- reads
    def stats(self) -> dict[str, object]:
        one = lambda sql, *args: self._connection.execute(sql, args).fetchone()[0]  # noqa: E731
        return {
            "listings_total": one("SELECT COUNT(*) FROM listings"),
            "listings_active": one("SELECT COUNT(*) FROM listings WHERE is_active = 1"),
            "listings_matched": one(
                "SELECT COUNT(*) FROM listings WHERE matched = 1 AND is_active = 1"
            ),
            "sightings": one("SELECT COUNT(*) FROM sightings"),
            "multi_portal_units": one(
                """SELECT COUNT(*) FROM (
                       SELECT dedupe_key FROM sightings
                        GROUP BY dedupe_key HAVING COUNT(DISTINCT source) > 1)"""
            ),
            "price_changes": one("SELECT COUNT(*) FROM price_history WHERE old_price IS NOT NULL"),
            "alerts_sent": one("SELECT COUNT(*) FROM alerts WHERE ok = 1"),
            "runs": one("SELECT COUNT(*) FROM runs"),
        }

    def matched_listings(self, limit: int = 100) -> list[Listing]:
        rows = self._connection.execute(
            """SELECT * FROM listings
                WHERE matched = 1 AND is_active = 1
                ORDER BY match_score DESC, last_seen_at DESC
                LIMIT ?""",
            (limit,),
        ).fetchall()
        return [Listing.from_row(dict(row)) for row in rows]

    def price_history(self, dedupe_key: str) -> list[sqlite3.Row]:
        return list(
            self._connection.execute(
                "SELECT * FROM price_history WHERE dedupe_key = ? ORDER BY changed_at",
                (dedupe_key,),
            )
        )


# Columns where an incoming NULL means "this source didn't say", not "empty".
_NULLABLE_MERGE_COLUMNS = frozenset({
    "beds", "baths", "sqft", "year_built", "latitude", "longitude",
    "available_date", "days_on_market", "broker", "photos", "amenities",
    "description", "title", "lease_min_months", "lease_max_months",
    "property_type", "unit", "city", "zip_code", "url", "amenity_matches",
    "enrichment", "lease_term_text", "miles_from_ocean",
})


def utc_cutoff(hours: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=hours)
