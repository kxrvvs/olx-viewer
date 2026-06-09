from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .scraper import Listing, numeric_price

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    price TEXT NOT NULL,
    city TEXT NOT NULL,
    district TEXT NOT NULL,
    date TEXT NOT NULL,
    url TEXT NOT NULL,
    image_url TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
)
"""

IGNORED_SCHEMA = """
CREATE TABLE IF NOT EXISTS ignored_ads (
    id TEXT PRIMARY KEY,
    title TEXT,
    price TEXT,
    city TEXT,
    district TEXT,
    date TEXT,
    url TEXT,
    image_url TEXT,
    ignored_at TEXT NOT NULL
)
"""

SAVED_SEARCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS saved_searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    pages INTEGER NOT NULL,
    category_path TEXT,
    category_label TEXT,
    region_id INTEGER,
    region_label TEXT,
    city_id INTEGER,
    city_label TEXT,
    min_price REAL,
    max_price REAL,
    sort TEXT,
    created_at TEXT NOT NULL
)
"""

ITEMS_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

ITEM_SEARCHES_SCHEMA = """
CREATE TABLE IF NOT EXISTS item_searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    query TEXT NOT NULL,
    pages INTEGER NOT NULL DEFAULT 1,
    category_path TEXT,
    category_label TEXT,
    region_id INTEGER,
    region_label TEXT,
    city_id INTEGER,
    city_label TEXT,
    sort TEXT,
    source TEXT NOT NULL DEFAULT 'olx',
    created_at TEXT NOT NULL
)
"""

PRICE_SNAPSHOTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS price_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL,
    listing_id TEXT NOT NULL,
    title TEXT,
    price_raw TEXT,
    price_numeric REAL,
    url TEXT
)
"""

# Detail columns added after `ignored_ads` first shipped (it originally only
# tracked `id` + `ignored_at`) — backfilled via ALTER TABLE for existing
# databases so older ignored entries keep working (just with blank details)
# instead of raising "no such column" errors.
IGNORED_DETAIL_COLUMNS = ("title", "price", "city", "district", "date", "url", "image_url")

# Location columns added to `saved_searches` after it first shipped — same
# ALTER-TABLE backfill approach as `IGNORED_DETAIL_COLUMNS`/`_add_missing_ignored_columns`,
# so existing saved searches keep working (with no district/city filter) instead
# of raising "no such column".
SAVED_SEARCH_LOCATION_COLUMNS = (
    ("region_id", "INTEGER"),
    ("region_label", "TEXT"),
    ("city_id", "INTEGER"),
    ("city_label", "TEXT"),
)

# Columns compared to detect changes between a re-scraped listing and what's
# stored — everything that could plausibly be edited or refreshed by the
# seller, but not `id` (the lookup key) or the `first_seen`/`last_seen` tracking
# columns themselves.
TRACKED_FIELDS = ("title", "price", "city", "district", "date", "url", "image_url")


@dataclass
class SaveResult:
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    changes: list[str] = field(default_factory=list)


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(SCHEMA)
    conn.execute(IGNORED_SCHEMA)
    conn.execute(SAVED_SEARCH_SCHEMA)
    conn.execute(ITEMS_SCHEMA)
    conn.execute(ITEM_SEARCHES_SCHEMA)
    conn.execute(PRICE_SNAPSHOTS_SCHEMA)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS price_snapshots_item_run "
        "ON price_snapshots(item_id, run_id)"
    )
    _add_missing_ignored_columns(conn)
    _add_missing_saved_search_columns(conn)
    return conn


def _add_missing_ignored_columns(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(ignored_ads)")}
    for column in IGNORED_DETAIL_COLUMNS:
        if column not in existing:
            conn.execute(f"ALTER TABLE ignored_ads ADD COLUMN {column} TEXT")


def _add_missing_saved_search_columns(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(saved_searches)")}
    for column, sql_type in SAVED_SEARCH_LOCATION_COLUMNS:
        if column not in existing:
            conn.execute(f"ALTER TABLE saved_searches ADD COLUMN {column} {sql_type}")


def fetch_all(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every saved listing, most recently seen first."""
    return conn.execute(
        "SELECT id, title, price, city, district, date, url, image_url, first_seen, last_seen "
        "FROM listings ORDER BY last_seen DESC"
    ).fetchall()


def ignore_listing(conn: sqlite3.Connection, listing: Listing) -> None:
    """Save a snapshot of `listing` into `ignored_ads` so it's filtered out of all
    future searches (`fetch_ignored_ids`) and stays identifiable in the GUI's
    hidden-items view (`fetch_ignored`) — re-ignoring the same id just refreshes
    its details and `ignored_at` rather than erroring or duplicating.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO ignored_ads (id, title, price, city, district, date, url, image_url, ignored_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET title = excluded.title, price = excluded.price, "
        "city = excluded.city, district = excluded.district, date = excluded.date, "
        "url = excluded.url, image_url = excluded.image_url, ignored_at = excluded.ignored_at",
        (listing.id, listing.title, listing.price, listing.city, listing.district,
         listing.date, listing.url, listing.image_url, now),
    )
    conn.commit()


def fetch_ignored_ids(conn: sqlite3.Connection) -> set[str]:
    """Every listing id that's been marked as ignored."""
    return {row["id"] for row in conn.execute("SELECT id FROM ignored_ads")}


def fetch_ignored(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every ignored ad, most recently hidden first."""
    return conn.execute(
        "SELECT id, title, price, city, district, date, url, image_url, ignored_at "
        "FROM ignored_ads ORDER BY ignored_at DESC"
    ).fetchall()


def delete_listings(conn: sqlite3.Connection, ids: list[str]) -> None:
    """Permanently remove the given ids from the saved-listings table."""
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    conn.execute(f"DELETE FROM listings WHERE id IN ({placeholders})", ids)
    conn.commit()


def unignore_listings(conn: sqlite3.Connection, ids: list[str]) -> None:
    """Remove the given ids from `ignored_ads`, letting them reappear in future searches."""
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    conn.execute(f"DELETE FROM ignored_ads WHERE id IN ({placeholders})", ids)
    conn.commit()


def save_search(
    conn: sqlite3.Connection,
    *,
    query: str,
    pages: int,
    category_path: str | None,
    category_label: str | None,
    region_id: int | None,
    region_label: str | None,
    city_id: int | None,
    city_label: str | None,
    min_price: float | None,
    max_price: float | None,
    sort: str | None,
) -> None:
    """Remember a search's query and filters so it can be found again on the GUI's Home tab."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO saved_searches "
        "(query, pages, category_path, category_label, region_id, region_label, "
        "city_id, city_label, min_price, max_price, sort, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (query, pages, category_path, category_label, region_id, region_label,
         city_id, city_label, min_price, max_price, sort, now),
    )
    conn.commit()


def fetch_saved_searches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every saved search, most recently saved first."""
    return conn.execute(
        "SELECT id, query, pages, category_path, category_label, region_id, region_label, "
        "city_id, city_label, min_price, max_price, sort, created_at "
        "FROM saved_searches ORDER BY created_at DESC"
    ).fetchall()


def delete_saved_search(conn: sqlite3.Connection, search_id: int) -> None:
    """Permanently remove one saved search by its row id."""
    conn.execute("DELETE FROM saved_searches WHERE id = ?", (search_id,))
    conn.commit()


# -- Items ----------------------------------------------------------------
#
# An "item" is a named profile that groups one or more search configurations.
# Running an item fires all its searches and shows combined, deduplicated results.
# `item_searches` rows are deleted automatically when their item is deleted
# (ON DELETE CASCADE, enforced via PRAGMA foreign_keys = ON in connect()).

def create_item(conn: sqlite3.Connection, name: str) -> int:
    """Create a new empty item and return its id."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cursor = conn.execute("INSERT INTO items (name, created_at) VALUES (?, ?)", (name, now))
    conn.commit()
    return cursor.lastrowid


def fetch_items(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every item, most recently created first."""
    return conn.execute(
        "SELECT id, name, created_at FROM items ORDER BY created_at DESC"
    ).fetchall()


def rename_item(conn: sqlite3.Connection, item_id: int, name: str) -> None:
    conn.execute("UPDATE items SET name = ? WHERE id = ?", (name, item_id))
    conn.commit()


def delete_item(conn: sqlite3.Connection, item_id: int) -> None:
    """Delete an item and all its searches (cascade)."""
    conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
    conn.commit()


def add_item_search(
    conn: sqlite3.Connection,
    item_id: int,
    *,
    query: str,
    pages: int,
    category_path: str | None,
    category_label: str | None,
    region_id: int | None,
    region_label: str | None,
    city_id: int | None,
    city_label: str | None,
    sort: str | None,
    source: str = "olx",
) -> int:
    """Add one search configuration to an item and return the new row id."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cursor = conn.execute(
        "INSERT INTO item_searches "
        "(item_id, query, pages, category_path, category_label, region_id, region_label, "
        "city_id, city_label, sort, source, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (item_id, query, pages, category_path, category_label, region_id, region_label,
         city_id, city_label, sort, source, now),
    )
    conn.commit()
    return cursor.lastrowid


def fetch_item_searches(conn: sqlite3.Connection, item_id: int) -> list[sqlite3.Row]:
    """All searches for one item, oldest first (the order they were added)."""
    return conn.execute(
        "SELECT id, item_id, query, pages, category_path, category_label, "
        "region_id, region_label, city_id, city_label, sort, source, created_at "
        "FROM item_searches WHERE item_id = ? ORDER BY created_at ASC",
        (item_id,),
    ).fetchall()


def delete_item_search(conn: sqlite3.Connection, search_id: int) -> None:
    """Remove one search from an item."""
    conn.execute("DELETE FROM item_searches WHERE id = ?", (search_id,))
    conn.commit()


def save_price_snapshot_batch(
    conn: sqlite3.Connection, item_id: int, listings: list[Listing]
) -> None:
    """Record a price snapshot for every listing produced by one item run.

    All rows share the same `run_id` (the current UTC timestamp) so analytics
    queries can group by session without tracking a separate run table.
    """
    if not listings:
        return
    run_id = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.executemany(
        "INSERT INTO price_snapshots (item_id, run_id, listing_id, title, price_raw, price_numeric, url) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (item_id, run_id, listing.id, listing.title, listing.price,
             numeric_price(listing.price), listing.url)
            for listing in listings
        ],
    )
    conn.commit()


def fetch_price_runs(conn: sqlite3.Connection, item_id: int) -> list[sqlite3.Row]:
    """Per-session aggregate stats for one item, oldest session first.

    Ignored listings are excluded so hidden ads don't distort the stats.
    """
    return conn.execute(
        "SELECT run_id, COUNT(*) AS listing_count, "
        "MIN(price_numeric) AS min_price, MAX(price_numeric) AS max_price, "
        "AVG(price_numeric) AS avg_price "
        "FROM price_snapshots "
        "WHERE item_id = ? AND listing_id NOT IN (SELECT id FROM ignored_ads) "
        "GROUP BY run_id ORDER BY run_id ASC",
        (item_id,),
    ).fetchall()


def fetch_listing_history(conn: sqlite3.Connection, item_id: int) -> list[sqlite3.Row]:
    """All snapshots for one item, for per-listing price tracking.

    Ignored listings are excluded — same filter as fetch_price_runs.
    """
    return conn.execute(
        "SELECT listing_id, title, price_raw, price_numeric, url, run_id "
        "FROM price_snapshots "
        "WHERE item_id = ? AND listing_id NOT IN (SELECT id FROM ignored_ads) "
        "ORDER BY listing_id, run_id ASC",
        (item_id,),
    ).fetchall()


def update_item_search(
    conn: sqlite3.Connection,
    search_id: int,
    *,
    query: str,
    pages: int,
    category_path: str | None,
    category_label: str | None,
    region_id: int | None,
    region_label: str | None,
    city_id: int | None,
    city_label: str | None,
    sort: str | None,
) -> None:
    """Update an existing item search's parameters in-place."""
    conn.execute(
        "UPDATE item_searches SET query = ?, pages = ?, category_path = ?, category_label = ?, "
        "region_id = ?, region_label = ?, city_id = ?, city_label = ?, sort = ? WHERE id = ?",
        (query, pages, category_path, category_label, region_id, region_label,
         city_id, city_label, sort, search_id),
    )
    conn.commit()


def save_listings(conn: sqlite3.Connection, listings: list[Listing]) -> SaveResult:
    """Insert new listings, update changed ones, and refresh `last_seen` for the rest.

    Matching is by `Listing.id` — OLX's own ad id, stable across re-scrapes —
    so the same ad encountered again updates its existing row instead of
    creating a duplicate. Existing rows are compared field by field first, so
    a price drop (or any other edit) is reported in `SaveResult.changes`
    rather than silently overwritten.
    """
    result = SaveResult()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for listing in listings:
        row = conn.execute(
            "SELECT title, price, city, district, date, url, image_url FROM listings WHERE id = ?",
            (listing.id,),
        ).fetchone()

        if row is None:
            conn.execute(
                "INSERT INTO listings "
                "(id, title, price, city, district, date, url, image_url, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (listing.id, listing.title, listing.price, listing.city, listing.district,
                 listing.date, listing.url, listing.image_url, now, now),
            )
            result.added += 1
            continue

        stored = dict(zip(TRACKED_FIELDS, row))
        diffs = [
            f'"{listing.title}" — {field_name}: "{stored[field_name] or ""}" → "{getattr(listing, field_name) or ""}"'
            for field_name in TRACKED_FIELDS
            if (stored[field_name] or "") != (getattr(listing, field_name) or "")
        ]

        if diffs:
            conn.execute(
                "UPDATE listings SET title = ?, price = ?, city = ?, district = ?, date = ?, "
                "url = ?, image_url = ?, last_seen = ? WHERE id = ?",
                (listing.title, listing.price, listing.city, listing.district, listing.date,
                 listing.url, listing.image_url, now, listing.id),
            )
            result.updated += 1
            result.changes.extend(diffs)
        else:
            conn.execute("UPDATE listings SET last_seen = ? WHERE id = ?", (now, listing.id))
            result.unchanged += 1

    conn.commit()
    return result
