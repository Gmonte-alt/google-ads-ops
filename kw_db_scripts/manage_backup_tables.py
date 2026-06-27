"""
manage_backup_tables.py — list (default) or drop backup/snapshot tables in
paid_search.db. Safe: a hardcoded LIVE-TABLE denylist means it can NEVER drop a
production table even if a pattern matched it. Drops require --drop and naming the
tables explicitly (or --all-listed to drop everything it lists).

  python manage_backup_tables.py                       # list candidates + row counts
  python manage_backup_tables.py --drop NAME1 NAME2    # drop specific tables
  python manage_backup_tables.py --drop --all-listed   # drop ALL listed candidates
"""

import argparse
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")

# patterns that identify backup/snapshot tables
PATTERNS = ["%backup%", "%predupe%", "%prephase%", "%prematchrisk%",
            "%snapshot%", "%_pre_%", "%_bak%", "%_old%"]

# LIVE tables that must NEVER be dropped, regardless of pattern match.
LIVE = {
    "brand_registry", "brand_registry_keywords", "keyword_performance_daily",
    "keyword_instances", "search_queries", "conversion_actions",
    "product_taxonomy", "product_taxonomy_terms", "campaigns", "accounts",
    "geo_performance_daily",
}


def list_candidates(conn):
    rows = []
    for pat in PATTERNS:
        for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ?", (pat,)):
            if r[0] not in LIVE and r[0] not in [x[0] for x in rows]:
                cnt = conn.execute(f"SELECT COUNT(*) FROM \"{r[0]}\"").fetchone()[0]
                rows.append((r[0], cnt))
    return sorted(rows)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--drop", nargs="*", default=None,
                   help="Table names to drop. With --all-listed, drops all listed.")
    p.add_argument("--all-listed", action="store_true")
    a = p.parse_args()
    if not DB.exists():
        sys.exit(f"DB not found: {DB}")
    conn = sqlite3.connect(DB)

    cands = list_candidates(conn)
    print("=" * 64)
    print(f"  Backup/snapshot tables in paid_search.db  ({len(cands)})")
    print("=" * 64)
    for name, cnt in cands:
        print(f"  {name:<46} {cnt:>10,} rows")
    if not cands:
        print("  (none found)")

    if a.drop is None:
        print("\n  LIST ONLY. To drop: --drop NAME [NAME...]  or  --drop --all-listed")
        conn.close()
        return

    targets = [n for n, _ in cands] if a.all_listed else a.drop
    # safety: never drop a live table
    blocked = [t for t in targets if t in LIVE]
    if blocked:
        sys.exit(f"\n  REFUSED: {blocked} are LIVE tables. Nothing dropped.")
    # only drop things that actually exist as backup candidates
    valid = [t for t in targets if t in [n for n, _ in cands]]
    skip = [t for t in targets if t not in valid]
    if skip:
        print(f"\n  Skipping (not a listed backup table): {skip}")

    conn.execute("PRAGMA busy_timeout = 15000")
    dropped = 0
    for t in valid:
        conn.execute(f'DROP TABLE IF EXISTS "{t}"')
        print(f"  dropped: {t}")
        dropped += 1
    conn.commit()
    print(f"\n  {dropped} table(s) dropped.")
    print("  Tip: run  VACUUM;  separately to reclaim file space on disk.")
    conn.close()


if __name__ == "__main__":
    main()
