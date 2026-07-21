"""
14_migrate_google_change_history.py  (Google change-history, Phase 1)

Pipeline step 14 (11-13 = the Microsoft change-history block).
CADENCE: ONE-TIME setup. Idempotent (safe to re-run) but only needed once.

Sequence:
  Initial backfill:  14 (migrate) -> 15 (load YTD CSV) -> 16 (name-history) -> 17 (change-events)
  Weekly ongoing:    15 (load) -> 16 -> 17     (14 never re-runs)

Additive migration. Creates the Google raw ledger and ensures the shared derived
tables exist with the expected shape. Does NOT drop or alter anything -- the
shared tables are already populated by the Microsoft build, so this is safe to
run repeatedly.

Usage:
    python migrate_google_change_history.py --db PATH                 # dry-run (default)
    python migrate_google_change_history.py --db PATH --apply
    python migrate_google_change_history.py --db PATH --cid 888-650-2147 --apply
"""

import argparse
import sys
import campaign_db_scripts.change_history.gads_change_common as C

# Google raw ledger (Model-A: accumulates forward via row_hash upsert).
DDL_RAW = """
CREATE TABLE IF NOT EXISTS google_change_csv_raw (
  id              INTEGER PRIMARY KEY,
  account_id      INTEGER NOT NULL REFERENCES accounts(id),
  source_file     TEXT    NOT NULL,
  row_hash        TEXT    NOT NULL UNIQUE,
  change_datetime TEXT    NOT NULL,   -- normalized ISO 'YYYY-MM-DD HH:MM:SS'
  date_time_raw   TEXT    NOT NULL,   -- verbatim, incl U+202F
  user_email      TEXT,
  campaign_col    TEXT,
  ad_group_col    TEXT,
  changes_raw     TEXT    NOT NULL,
  loaded_at       TEXT    NOT NULL
);
"""

# Google API change_event ledger (Model-A: id-native, idempotent on the event's
# own unique resource_name, so overlapping weekly re-pulls no-op).
DDL_API_RAW = """
CREATE TABLE IF NOT EXISTS google_change_event_raw (
  id                   INTEGER PRIMARY KEY,
  account_id           INTEGER NOT NULL REFERENCES accounts(id),
  resource_name        TEXT    NOT NULL UNIQUE,   -- change_event.resource_name (unique per event)
  change_datetime      TEXT    NOT NULL,          -- ISO 'YYYY-MM-DD HH:MM:SS'
  change_resource_type TEXT    NOT NULL,          -- CAMPAIGN | CAMPAIGN_BUDGET | BIDDING_STRATEGY | ...
  resource_operation   TEXT    NOT NULL,          -- CREATE | UPDATE | REMOVE
  change_resource_name TEXT    NOT NULL,          -- the affected resource (campaign/budget/strategy)
  user_email           TEXT,
  client_type          TEXT,
  changed_fields       TEXT,                       -- JSON list of changed paths
  changes_json         TEXT    NOT NULL,           -- JSON {path: [old, new]}
  loaded_at            TEXT    NOT NULL
);
"""

# Shared derived tables -- created only if absent (Microsoft build already made them).
DDL_NAME_HISTORY = """
CREATE TABLE IF NOT EXISTS campaign_name_history (
  id                 INTEGER PRIMARY KEY,
  platform           TEXT NOT NULL,
  account_id         INTEGER NOT NULL REFERENCES accounts(id),
  campaign_id        TEXT NOT NULL REFERENCES campaigns(campaign_id),
  campaign_name      TEXT NOT NULL,
  valid_from         TEXT,
  valid_to           TEXT,
  source             TEXT NOT NULL,
  convention_version TEXT,
  UNIQUE(platform, campaign_id, valid_from)
);
"""

DDL_CHANGE_EVENTS = """
CREATE TABLE IF NOT EXISTS campaign_change_events (
  id                 INTEGER PRIMARY KEY,
  platform           TEXT NOT NULL,
  account_id         INTEGER NOT NULL REFERENCES accounts(id),
  campaign_id        TEXT NOT NULL REFERENCES campaigns(campaign_id),
  change_date        TEXT NOT NULL,
  change_type        TEXT NOT NULL,
  old_value          REAL,
  new_value          REAL,
  raw_event_count    INTEGER NOT NULL,
  resolved_via       TEXT NOT NULL,
  source             TEXT NOT NULL,
  convention_version TEXT,
  derived_at         TEXT NOT NULL
);
"""

DDL_OVERRIDES = """
CREATE TABLE IF NOT EXISTS campaign_rename_overrides (
  id              INTEGER PRIMARY KEY,
  platform        TEXT NOT NULL,
  account_id      INTEGER NOT NULL REFERENCES accounts(id),
  campaign_id     TEXT NOT NULL REFERENCES campaigns(campaign_id),
  historical_name TEXT NOT NULL,
  valid_from      TEXT,
  valid_to        TEXT NOT NULL,
  note            TEXT,
  source          TEXT NOT NULL DEFAULT 'manual'
);
"""

EXPECTED = {
    "campaign_name_history": {"platform", "account_id", "campaign_id", "campaign_name",
                              "valid_from", "valid_to", "source", "convention_version"},
    "campaign_change_events": {"platform", "account_id", "campaign_id", "change_date",
                               "change_type", "old_value", "new_value", "raw_event_count",
                               "resolved_via", "source", "convention_version", "derived_at"},
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--cid", default=C.DEFAULT_CID)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = C.connect(args.db, write=args.apply)
    try:
        acct = C.resolve_account_id(conn, args.cid)
        print("[ok] resolved %s account '%s' -> accounts.id=%d" % (C.PLATFORM, args.cid, acct))

        # Guard: if a shared table already exists, its columns must match expectation
        # AND every live NOT NULL column must be one the derives actually populate
        # (EXPECTED == the full insert column set per table). Catches both a missing
        # column and a NOT NULL column we don't write -- the latter is what bit the
        # first apply (convention_version is NOT NULL on the live MS-built tables).
        for t, cols in EXPECTED.items():
            if C.table_exists(conn, t):
                have = set(C.table_columns(conn, t))
                missing = cols - have
                if missing:
                    print("[ABORT] existing %s is missing columns %s" % (t, sorted(missing)))
                    sys.exit(2)
                live_nn = C.notnull_columns(conn, t)          # NOT NULL, non-pk
                unwritable = live_nn - cols                    # NOT NULL but never inserted
                if unwritable:
                    print("[ABORT] %s has NOT NULL columns the derives don't populate: %s "
                          "(MS schema diverged -- update the derives before applying)" % (t, sorted(unwritable)))
                    sys.exit(2)
                print("[ok] shared table %s present, schema compatible (NOT NULL: %s)" % (
                    t, sorted(live_nn)))
            else:
                print("[new] shared table %s will be created" % t)

        plan = [
            ("google_change_csv_raw", DDL_RAW),
            ("google_change_event_raw", DDL_API_RAW),
            ("campaign_name_history", DDL_NAME_HISTORY),
            ("campaign_change_events", DDL_CHANGE_EVENTS),
            ("campaign_rename_overrides", DDL_OVERRIDES),
        ]
        for name, ddl in plan:
            existed = C.table_exists(conn, name)
            if args.apply:
                conn.execute(ddl)
                print("[apply] %s %s" % ("verified" if existed else "CREATED", name))
            else:
                print("[dry-run] would ensure %s (%s)" % (name, "exists" if existed else "CREATE"))

        if args.apply:
            conn.execute("COMMIT")
            print("[apply] migration committed (additive; no data altered)")
        else:
            print("\nDry run only. Re-run with --apply to create tables.")
    except Exception:
        if args.apply:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
