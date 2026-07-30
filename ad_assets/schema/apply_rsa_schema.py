#!/usr/bin/env python
"""
apply_rsa_schema.py -- ad_assets/schema/ -- migration runner for
02_create_rsa_tables.sql (Phase 2 RSA layer).

Identical discipline to apply_asset_schema.py: dry-run default, required
--db, backup-first (sqlite backup API), small-DB tripwire, post-apply
verification. All DDL is CREATE ... IF NOT EXISTS -- re-running is safe.

  python apply_rsa_schema.py --db "C:\\Projects\\google-ads-ops\\db\\paid_search.db"            # dry run
  python apply_rsa_schema.py --db "C:\\Projects\\google-ads-ops\\db\\paid_search.db" --apply
"""
import argparse
import datetime
import os
import sqlite3
import sys

MIN_DB_BYTES = 5 * 1024 ** 3

DDL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "02_create_rsa_tables.sql")

EXPECTED_OBJECTS = {
    "table": ["creative_angle_taxonomy",
              "rsa_ads",
              "asset_text_data",
              "rsa_ad_assets",
              "rsa_asset_performance_daily"],
    "index": ["idx_rsa_ads_fingerprint",
              "idx_rsa_ads_campaign",
              "idx_rsa_ad_assets_asset",
              "idx_rsa_perf_date",
              "idx_rsa_perf_ad"],
    "view": [],
}


def fail(msg):
    print("[ABORT] %s" % msg)
    sys.exit(1)


def inventory(conn):
    existing = set()
    have = {(t, n) for t, n in conn.execute(
        "SELECT type, name FROM sqlite_master WHERE type IN ('table','index','view')")}
    for obj_type, names in EXPECTED_OBJECTS.items():
        for name in names:
            if (obj_type, name) in have:
                existing.add((obj_type, name))
    return existing


def take_backup(db_path):
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = "%s.pre_rsa_schema_%s.bak" % (db_path, ts)
    print("[BACKUP] writing %s (sqlite3 backup API, WAL-safe)..." % backup_path)
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(backup_path)
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    print("[BACKUP] done (%.2f GB)." % (os.path.getsize(backup_path) / 1024 ** 3))


def main():
    p = argparse.ArgumentParser(description="Apply the Phase 2 RSA schema (dry-run default).")
    p.add_argument("--db", required=True, help="Path to paid_search.db (REQUIRED)")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--allow-small-db", action="store_true")
    p.add_argument("--skip-backup", action="store_true",
                   help="VIOLATES HOUSE DISCIPLINE -- only after a verified external backup.")
    args = p.parse_args()

    if not os.path.isfile(args.db):
        fail("database not found: %s" % args.db)
    size = os.path.getsize(args.db)
    print("[TARGET] %s (%.2f GB)" % (args.db, size / 1024 ** 3))
    if size < MIN_DB_BYTES and not args.allow_small_db:
        fail("small-DB tripwire: %.2f GB < %.0f GB. Pass --allow-small-db only for test DBs."
             % (size / 1024 ** 3, MIN_DB_BYTES / 1024 ** 3))
    if not os.path.isfile(DDL_FILE):
        fail("DDL file not found next to this script: %s" % DDL_FILE)
    with open(DDL_FILE, "r", encoding="utf-8") as f:
        ddl = f.read()

    conn = sqlite3.connect("file:%s?mode=ro" % args.db.replace("\\", "/"), uri=True)
    existing = inventory(conn)
    conn.close()

    print("\n[PLAN] objects defined in 02_create_rsa_tables.sql:")
    to_create = 0
    for obj_type, names in EXPECTED_OBJECTS.items():
        for name in names:
            hit = (obj_type, name) in existing
            if not hit:
                to_create += 1
            print("  %-5s %-32s %s" % (obj_type, name,
                  "exists (skipped -- IF NOT EXISTS)" if hit else "WILL CREATE"))

    if not args.apply:
        print("\n[DRY RUN] no changes made. %d object(s) would be created. "
              "Re-run with --apply to execute." % to_create)
        return
    if to_create == 0:
        print("\n[APPLY] all objects already exist -- nothing to do.")
        return

    if args.skip_backup:
        print("[WARNING] --skip-backup: proceeding WITHOUT a pre-apply backup.")
    else:
        take_backup(args.db)

    print("[APPLY] executing DDL...")
    conn = sqlite3.connect(args.db)
    try:
        conn.executescript(ddl)
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        conn.close()
        fail("DDL execution failed, rolled back: %s" % e)

    missing = []
    now = inventory(conn)
    conn.close()
    for obj_type, names in EXPECTED_OBJECTS.items():
        for name in names:
            if (obj_type, name) not in now:
                missing.append("%s %s" % (obj_type, name))
    if missing:
        fail("post-apply verification FAILED, missing: %s" % ", ".join(missing))
    print("[APPLY] done. All %d expected objects verified present."
          % sum(len(v) for v in EXPECTED_OBJECTS.values()))
    print("[NEXT] seed creative_angle_taxonomy from V11, then 02_sync_rsa.py.")


if __name__ == "__main__":
    main()
