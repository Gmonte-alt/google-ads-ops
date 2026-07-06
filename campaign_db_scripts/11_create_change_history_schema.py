"""
11_create_change_history_schema.py
==================================
Creates the Microsoft campaign name-history schema (SCD2) and its raw
staging table. Dry-run by default; pass --apply to actually create.

This is the FIRST version of the change-history data model. Scope:
campaign name history only, Microsoft only. Column shape matches the
shared change_history_pipeline_design.md spec (§8) so it slots into the
broader unified design later without migration.

TABLES CREATED

  microsoft_change_grid_raw
    Direct, near-verbatim dump of the Microsoft UI grid ChangeHistory
    export. Source of truth for re-derivation. INSERT OR REPLACE on a
    natural key so re-loading the same export is idempotent.

  campaign_name_history
    The queryable SCD2 dimension. One row per (campaign_id, name interval).
    Reports resolve names via:
        report_date BETWEEN valid_from AND COALESCE(valid_to, '9999-12-31')
    Performance fact tables are NEVER modified. This is a pure dimension
    layered on top, joined at report time on the immutable campaign_id.

  campaign_rename_overrides
    Manual seed table for renames older than any available export window,
    or for disambiguating edge cases (e.g. two campaigns that briefly
    shared a name during a swap). Never hand-edited into output rows;
    it's an INPUT to the derive step, surviving a full re-derive.

DESIGN NOTES

  - SCD2 keyed on immutable campaign_id (+ account_id scoping). Names
    change; ids don't. Joins always go through id.
  - valid_to is NULL for the current (open) interval. Resolve with
    COALESCE(valid_to, '9999-12-31') at query time, or test IS NULL.
  - valid_from on the earliest interval = floor, derived from
    MIN(date) in campaign_performance_daily per campaign_id (data-derived,
    not arbitrary). Falls back to a configured epoch if the campaign has
    no performance data.
  - source column tags provenance: 'grid_csv', 'snapshot_seed',
    'manual_override', 'perf_floor'.
  - convention_version tracks the derivation logic version so a future
    re-derive under changed rules is traceable.

CONVENTIONS (matching the broader codebase)
  - Single DB: C:\\Projects\\google-ads-ops\\db\\paid_search.db
  - account_id scoping on every row (Microsoft Home = 2)
  - PRAGMA foreign_keys = ON asserted per connection
  - WAL journal mode
  - Dry-run default; --apply to execute; backs up any existing
    same-named table before recreating

Usage:
    python 11_create_change_history_schema.py            # dry-run, shows DDL
    python 11_create_change_history_schema.py --apply     # creates tables
"""

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")


# ── DDL ────────────────────────────────────────────────────────────────────────

DDL_RAW_STAGING = """
CREATE TABLE IF NOT EXISTS microsoft_change_grid_raw (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id          INTEGER NOT NULL,
    change_datetime     TEXT,           -- raw timestamp string from the grid
    changed_by          TEXT,           -- user/email if present
    item_changed        TEXT,           -- e.g. campaign name string (grid is name-only)
    attribute_changed   TEXT,           -- raw attribute code, e.g. 'CampaignName'
    old_value           TEXT,
    new_value           TEXT,
    change_date         TEXT,           -- normalized YYYY-MM-DD derived from change_datetime
    loaded_at           TEXT NOT NULL,
    FOREIGN KEY (account_id) REFERENCES accounts(id),
    -- Natural-key uniqueness: same change event shouldn't duplicate on reload.
    UNIQUE (account_id, change_datetime, attribute_changed, old_value, new_value)
);
CREATE INDEX IF NOT EXISTS ix_msgrid_attr
    ON microsoft_change_grid_raw (account_id, attribute_changed);
CREATE INDEX IF NOT EXISTS ix_msgrid_date
    ON microsoft_change_grid_raw (account_id, change_date);
"""

DDL_NAME_HISTORY = """
CREATE TABLE IF NOT EXISTS campaign_name_history (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    platform            TEXT NOT NULL,           -- 'microsoft' (spec §8 column)
    account_id          INTEGER NOT NULL,        -- scoping (our addition)
    campaign_id         TEXT NOT NULL,           -- immutable id, stripped (no brackets)
    campaign_name       TEXT NOT NULL,           -- name during this interval
    valid_from          TEXT NOT NULL,           -- YYYY-MM-DD inclusive
    valid_to            TEXT,                    -- YYYY-MM-DD inclusive; NULL = open/current
    source              TEXT NOT NULL,           -- provenance tag
    convention_version  TEXT NOT NULL,           -- derivation logic version
    FOREIGN KEY (account_id) REFERENCES accounts(id),
    -- An id can have at most one interval starting on a given date.
    UNIQUE (platform, account_id, campaign_id, valid_from)
);
CREATE INDEX IF NOT EXISTS ix_cnh_lookup
    ON campaign_name_history (campaign_id, valid_from, valid_to);
CREATE INDEX IF NOT EXISTS ix_cnh_account
    ON campaign_name_history (account_id, platform);
"""

DDL_OVERRIDES = """
CREATE TABLE IF NOT EXISTS campaign_rename_overrides (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    platform            TEXT NOT NULL,
    account_id          INTEGER NOT NULL,
    campaign_id         TEXT,                    -- may be NULL if only known by name
    old_name            TEXT NOT NULL,
    new_name            TEXT NOT NULL,
    rename_date         TEXT NOT NULL,           -- YYYY-MM-DD the rename took effect
    note                TEXT,                    -- why this override exists
    FOREIGN KEY (account_id) REFERENCES accounts(id),
    UNIQUE (platform, account_id, old_name, new_name, rename_date)
);
"""

ALL_DDL = {
    "microsoft_change_grid_raw": DDL_RAW_STAGING,
    "campaign_name_history":     DDL_NAME_HISTORY,
    "campaign_rename_overrides": DDL_OVERRIDES,
}


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn, name):
    return conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def main():
    ap = argparse.ArgumentParser(description="Create change-history schema (dry-run default).")
    ap.add_argument("--apply", action="store_true", help="Actually create tables (default is dry-run).")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"ERROR: DB not found at {DB_PATH}")
        return 1

    print("=" * 70)
    print(f"  Change-history schema creation  ({'APPLY' if args.apply else 'DRY RUN'})")
    print("=" * 70)

    conn = get_connection()

    # Report current state
    print("\nCurrent state:")
    for name in ALL_DDL:
        exists = table_exists(conn, name)
        print(f"  {name:<34} {'EXISTS' if exists else 'missing'}")

    if not args.apply:
        print("\n--- DDL that WOULD be executed (dry-run) ---")
        for name, ddl in ALL_DDL.items():
            print(ddl)
        print("Re-run with --apply to create these tables.")
        conn.close()
        return 0

    # Apply: back up any existing same-named table first
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    for name, ddl in ALL_DDL.items():
        if table_exists(conn, name):
            backup = f"{name}_backup_{ts}"
            print(f"\n  Backing up existing {name} -> {backup}")
            conn.execute(f"CREATE TABLE {backup} AS SELECT * FROM {name}")
        print(f"  Creating {name} ...")
        conn.executescript(ddl)
    conn.commit()

    print("\nVerification:")
    for name in ALL_DDL:
        n = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        print(f"  {name:<34} {n:>6} rows")

    conn.close()
    print("\nDone. Schema created. Next: load the grid CSV + derive the history.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
