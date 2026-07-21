#!/usr/bin/env python
"""
apply_asset_schema.py -- ad_assets/schema/ -- migration runner for
01_create_asset_tables.sql against paid_search.db.

House migration discipline (non-negotiable, per charter):
  * DRY-RUN BY DEFAULT -- nothing is written unless --apply is passed
  * --db is REQUIRED with no default -- prevents applying to the wrong file
  * BACKUP-FIRST -- a sqlite3 backup-API copy is taken before any write
  * SMALL-DB TRIPWIRE -- aborts if the target file is suspiciously small
    (catches an empty/new/wrong database; paid_search.db is ~9.8 GB)

Usage:
  python apply_asset_schema.py --db "C:\\path\\to\\paid_search.db"            # dry run
  python apply_asset_schema.py --db "C:\\path\\to\\paid_search.db" --apply    # execute

All statements in the DDL are CREATE ... IF NOT EXISTS, so re-running is safe;
existing asset tables are never dropped or altered by this script.
"""
import argparse
import datetime
import os
import sqlite3
import sys

MIN_DB_BYTES = 5 * 1024 ** 3  # 5 GB tripwire; paid_search.db is ~9.8 GB

DDL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "01_create_asset_tables.sql")

EXPECTED_OBJECTS = {
    "table": ["assets",
              "asset_sitelink_data",
              "asset_associations",
              "asset_performance_daily"],
    "index": ["idx_asset_assoc_container",
              "idx_asset_perf_date",
              "idx_asset_perf_container"],
    "view":  ["asset_associations_current"],
}


def fail(msg):
    print("[ABORT] %s" % msg)
    sys.exit(1)


def inventory(conn):
    """Return {(type, name)} for expected objects that already exist."""
    existing = set()
    cur = conn.execute(
        "SELECT type, name FROM sqlite_master WHERE type IN ('table','index','view')")
    have = {(t, n) for t, n in cur.fetchall()}
    for obj_type, names in EXPECTED_OBJECTS.items():
        for name in names:
            if (obj_type, name) in have:
                existing.add((obj_type, name))
    return existing


def take_backup(db_path):
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = "%s.pre_asset_schema_%s.bak" % (db_path, ts)
    print("[BACKUP] writing %s (sqlite3 backup API, WAL-safe)..." % backup_path)
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(backup_path)
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    size_gb = os.path.getsize(backup_path) / 1024 ** 3
    print("[BACKUP] done (%.2f GB)." % size_gb)
    return backup_path


def main():
    p = argparse.ArgumentParser(description="Apply the asset DB schema (dry-run default).")
    p.add_argument("--db", required=True, help="Path to paid_search.db (REQUIRED, no default)")
    p.add_argument("--apply", action="store_true",
                   help="Actually execute the DDL. Without this flag: dry run only.")
    p.add_argument("--allow-small-db", action="store_true",
                   help="Override the small-DB tripwire (test databases only).")
    p.add_argument("--skip-backup", action="store_true",
                   help="Skip the pre-apply backup. VIOLATES HOUSE DISCIPLINE -- only "
                        "acceptable immediately after a verified external backup.")
    args = p.parse_args()

    # --- target checks -------------------------------------------------------
    if not os.path.isfile(args.db):
        fail("database not found: %s" % args.db)

    size = os.path.getsize(args.db)
    print("[TARGET] %s (%.2f GB)" % (args.db, size / 1024 ** 3))
    if size < MIN_DB_BYTES and not args.allow_small_db:
        fail("small-DB tripwire: file is %.2f GB (< %.0f GB). This does not look like "
             "production paid_search.db. Pass --allow-small-db only for test DBs."
             % (size / 1024 ** 3, MIN_DB_BYTES / 1024 ** 3))

    if not os.path.isfile(DDL_FILE):
        fail("DDL file not found next to this script: %s" % DDL_FILE)
    with open(DDL_FILE, "r", encoding="utf-8") as f:
        ddl = f.read()

    # --- inventory (read-only) ----------------------------------------------
    conn = sqlite3.connect("file:%s?mode=ro" % args.db.replace("\\", "/"), uri=True)
    existing = inventory(conn)
    conn.close()

    print("\n[PLAN] objects defined in 01_create_asset_tables.sql:")
    to_create = 0
    for obj_type, names in EXPECTED_OBJECTS.items():
        for name in names:
            status = "exists (skipped -- IF NOT EXISTS)" if (obj_type, name) in existing \
                     else "WILL CREATE"
            if (obj_type, name) not in existing:
                to_create += 1
            print("  %-5s %-32s %s" % (obj_type, name, status))

    if not args.apply:
        print("\n[DRY RUN] no changes made. %d object(s) would be created. "
              "Re-run with --apply to execute." % to_create)
        return

    if to_create == 0:
        print("\n[APPLY] all objects already exist -- nothing to do.")
        return

    # --- backup-first --------------------------------------------------------
    if args.skip_backup:
        print("[WARNING] --skip-backup passed: proceeding WITHOUT a pre-apply backup.")
    else:
        take_backup(args.db)

    # --- execute -------------------------------------------------------------
    print("[APPLY] executing DDL...")
    conn = sqlite3.connect(args.db)
    try:
        conn.executescript(ddl)
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        conn.close()
        fail("DDL execution failed, rolled back: %s" % e)

    # --- verify --------------------------------------------------------------
    missing = []
    now_existing = inventory(conn)
    for obj_type, names in EXPECTED_OBJECTS.items():
        for name in names:
            if (obj_type, name) not in now_existing:
                missing.append("%s %s" % (obj_type, name))
    conn.close()

    if missing:
        fail("post-apply verification FAILED, missing: %s" % ", ".join(missing))
    print("[APPLY] done. All %d expected objects verified present."
          % sum(len(v) for v in EXPECTED_OBJECTS.values()))
    print("[NEXT] step 3: sync script in ad_assets\\sync\\ -- and the routine line "
          "ships the same day the sync does.")


if __name__ == "__main__":
    main()
