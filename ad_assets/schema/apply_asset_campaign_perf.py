#!/usr/bin/env python
"""
apply_asset_campaign_perf.py -- ad_assets/schema/ -- migration runner for
05_add_asset_campaign_perf.sql. Standard discipline: dry-run default, required --db,
backup-first, small-DB tripwire, post-apply verification.

  python apply_asset_campaign_perf.py --db "C:\\Projects\\google-ads-ops\\db\\paid_search.db"            # dry run
  python apply_asset_campaign_perf.py --db "C:\\Projects\\google-ads-ops\\db\\paid_search.db" --apply
"""
import argparse
import datetime
import os
import sqlite3
import sys

MIN_DB_BYTES = 5 * 1024 ** 3
DDL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "05_add_asset_campaign_perf.sql")
EXPECTED_OBJECTS = {"table": ["asset_campaign_performance_daily"],
                    "index": ["idx_asset_camp_perf_date", "idx_asset_camp_perf_campaign"], "view": []}


def fail(msg):
    print("[ABORT] %s" % msg)
    sys.exit(1)


def inventory(conn):
    have = {(t, n) for t, n in conn.execute(
        "SELECT type, name FROM sqlite_master WHERE type IN ('table','index','view')")}
    return {(ot, n) for ot, names in EXPECTED_OBJECTS.items() for n in names if (ot, n) in have}


def main():
    p = argparse.ArgumentParser(description="Apply the asset_campaign_performance_daily addendum.")
    p.add_argument("--db", required=True)
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
        fail("small-DB tripwire: %.2f GB < %.0f GB." % (size / 1024 ** 3, MIN_DB_BYTES / 1024 ** 3))
    with open(DDL_FILE, "r", encoding="utf-8") as f:
        ddl = f.read()

    conn = sqlite3.connect("file:%s?mode=ro" % args.db.replace("\\", "/"), uri=True)
    existing = inventory(conn)
    conn.close()
    to_create = 0
    print("\n[PLAN]")
    for ot, names in EXPECTED_OBJECTS.items():
        for n in names:
            hit = (ot, n) in existing
            to_create += 0 if hit else 1
            print("  %-5s %-28s %s" % (ot, n, "exists (skipped)" if hit else "WILL CREATE"))

    if not args.apply:
        print("\n[DRY RUN] no changes made. %d object(s) would be created." % to_create)
        return
    if to_create == 0:
        print("\n[APPLY] nothing to do.")
        return

    if args.skip_backup:
        print("[WARNING] --skip-backup: no pre-apply backup.")
    else:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        bpath = "%s.pre_asset_camp_perf_%s.bak" % (args.db, ts)
        print("[BACKUP] writing %s ..." % bpath)
        src = sqlite3.connect(args.db)
        dst = sqlite3.connect(bpath)
        with dst:
            src.backup(dst)
        dst.close(); src.close()
        print("[BACKUP] done (%.2f GB)." % (os.path.getsize(bpath) / 1024 ** 3))

    conn = sqlite3.connect(args.db)
    try:
        conn.executescript(ddl)
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback(); conn.close()
        fail("DDL execution failed, rolled back: %s" % e)
    missing = [(ot, n) for ot, names in EXPECTED_OBJECTS.items() for n in names
               if (ot, n) not in inventory(conn)]
    conn.close()
    if missing:
        fail("post-apply verification FAILED: %s" % missing)
    print("[APPLY] done. Addendum verified present.")
    print("[NEXT] run 01b_sync_asset_campaign_split.py --from 2026-01-01 to backfill the campaign split.")


if __name__ == "__main__":
    main()
