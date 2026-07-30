#!/usr/bin/env python
"""
apply_creative_lineage.py -- ad_assets/schema/ -- migration runner for
04_add_creative_lineage.sql (gate-approved 2026-07-23). Standard discipline:
dry-run default, required --db, backup-first, small-DB tripwire, post-apply
verification -- plus the architect-required ORPHAN HYGIENE query on every
apply: lineage fingerprints absent from rsa_ads, with authored_date, so
abandoned authoring is visible rather than silently accumulating.
(A pre-registered creative like BCC-01/e1fd7d1595 is EXPECTED here until it
loads -- orphan means "authored, not yet live", which is information.)

  python apply_creative_lineage.py --db "C:\\Projects\\google-ads-ops\\db\\paid_search.db"
  python apply_creative_lineage.py --db "C:\\Projects\\google-ads-ops\\db\\paid_search.db" --apply
"""
import argparse
import datetime
import os
import sqlite3
import sys

MIN_DB_BYTES = 5 * 1024 ** 3
DDL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "04_add_creative_lineage.sql")
EXPECTED_OBJECTS = {"table": ["creative_lineage"],
                    "index": ["idx_lineage_parent"],
                    "view": ["creative_repository_current", "creative_lineage_view"]}
EXPECTED_TAXONOMY = ["brand_compliant_core", "renewal_repricing"]


def fail(msg):
    print("[ABORT] %s" % msg)
    sys.exit(1)


def inventory(conn):
    have = {(t, n) for t, n in conn.execute(
        "SELECT type, name FROM sqlite_master WHERE type IN ('table','index','view')")}
    return {(ot, n) for ot, names in EXPECTED_OBJECTS.items() for n in names if (ot, n) in have}


def hygiene(conn):
    try:
        rows = conn.execute(
            """SELECT l.fingerprint, MIN(l.authored_date), COUNT(*)
               FROM creative_lineage l
               WHERE l.account_id = 1
                 AND l.fingerprint NOT IN
                     (SELECT DISTINCT fingerprint FROM rsa_ads WHERE account_id = 1)
               GROUP BY l.fingerprint""").fetchall()
    except sqlite3.Error:
        return
    if rows:
        print("[HYGIENE] authored-but-not-live fingerprints (expected until load):")
        for fp, dt, n in rows:
            print("   %s  authored %s  (%d edge(s))" % (fp, dt, n))
    else:
        print("[HYGIENE] no orphaned lineage -- every authored fingerprint is live")


def main():
    p = argparse.ArgumentParser(description="Apply the creative lineage system (dry-run default).")
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
    print("  + 2 taxonomy rows (INSERT OR REPLACE, idempotent): %s"
          % ", ".join(EXPECTED_TAXONOMY))

    if not args.apply:
        print("\n[DRY RUN] no changes made. %d object(s) would be created." % to_create)
        return

    if args.skip_backup:
        print("[WARNING] --skip-backup: no pre-apply backup.")
    else:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        bpath = "%s.pre_lineage_%s.bak" % (args.db, ts)
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
    tax = [r[0] for r in conn.execute(
        "SELECT angle_code FROM creative_angle_taxonomy WHERE angle_code IN (?,?)",
        EXPECTED_TAXONOMY)]
    if missing or set(tax) != set(EXPECTED_TAXONOMY):
        conn.close()
        fail("post-apply verification FAILED: missing %s / taxonomy %s" % (missing, tax))
    print("[APPLY] done. %d objects + %d taxonomy rows verified."
          % (sum(len(v) for v in EXPECTED_OBJECTS.values()), len(tax)))
    hygiene(conn)
    conn.close()
    print("[NEXT] seed_creative_lineage_initial.py (BCC-01 edges + June family), "
          "then load BCC-01, then reconcile_fingerprint.py (THE acceptance test).")


if __name__ == "__main__":
    main()
