"""
migrate_dedup_brand_keywords.py  — Phase 1: collapse exact-dup keyword rows and
fix the NULL-match_type UNIQUE defeat that let them accumulate.
==============================================================================
ROOT CAUSE
  brand_registry_keywords had UNIQUE(normalized, match_type) with match_type
  almost always NULL. SQLite treats every NULL as DISTINCT in a UNIQUE
  constraint, so ON CONFLICT never fired and every 07 re-run re-inserted the
  whole variant set. Probe found 73,119 rows collapsing to 10,371 distinct
  (85.8% duplicate), Liberty Mutual variants at x26.

PHASE 1 (this script) — the safe, no-judgment win:
  * Collapse exact dups: keep one row per (brand_id, normalized, COALESCE(
    match_type,'')). Deterministic — keeps the lowest id in each group.
  * Rebuild with match_type NOT NULL DEFAULT '' (so NULLs can't defeat dedupe
    again) and UNIQUE(brand_id, normalized, match_type) — per-brand uniqueness,
    which does NOT require resolving the 18 cross-brand collisions first.
  * Preserve the old table as a backup (rename, don't drop) for rollback.

PHASE 2 (later, deliberate): resolve the 18 cross-brand collisions (duplicate
  brand identities like CSAA/CSAA Insurance, the ACE mis-attribution), then
  optionally tighten to the global UNIQUE(normalized, match_type).

AFTER THIS RUNS: the 07 insert path MUST be updated (match_type '' not NULL,
ON CONFLICT(brand_id, normalized, match_type)) before 07 is run again, or it
will error on the NOT NULL column. Do not re-run 07 until that lands.

SAFETY: defaults to DRY-RUN. Pass --apply to write. Old table is renamed to a
backup, not dropped.

    python migrate_dedup_brand_keywords.py            # dry-run preview
    python migrate_dedup_brand_keywords.py --apply
"""

import argparse
import sqlite3
import sys
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
TABLE = "brand_registry_keywords"
BACKUP = f"brand_registry_keywords_predupe_{date.today():%Y%m%d}"


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true",
                   help="Perform the migration. Without this, dry-run preview only.")
    return p.parse_args()


def lock_preflight(conn):
    """Fail fast if the DB is locked, instead of hanging."""
    conn.execute("PRAGMA busy_timeout = 15000")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError as e:
        sys.exit(f"  [LOCKED] DB is held by another process ({e}). "
                 f"Close other writers / taskkill python, then retry.")


def counts(conn):
    total = conn.execute(f"SELECT COUNT(*) c FROM {TABLE}").fetchone()["c"]
    nulls = conn.execute(f"SELECT COUNT(*) c FROM {TABLE} WHERE match_type IS NULL").fetchone()["c"]
    distinct = conn.execute(f"""SELECT COUNT(*) c FROM (
        SELECT brand_id, normalized, COALESCE(match_type,'') mt
        FROM {TABLE} GROUP BY brand_id, normalized, mt)""").fetchone()["c"]
    return total, nulls, distinct


def main():
    a = parse_args()
    if not DB.exists():
        sys.exit(f"DB not found: {DB}")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    print("=" * 70)
    print(f"  Phase 1 dedup migration{'   (DRY RUN)' if not a.apply else '   [APPLY]'}")
    print("=" * 70)

    total, nulls, distinct = counts(conn)
    excess = total - distinct
    print(f"  current rows        : {total:,}")
    print(f"  NULL match_type     : {nulls:,}")
    print(f"  distinct keys       : {distinct:,}")
    print(f"  excess to remove    : {excess:,}  ({100*excess/total:.1f}% of table)")

    if not a.apply:
        print("\n  DRY RUN — nothing written. Re-run with --apply to perform:")
        print(f"    1. rename {TABLE} -> {BACKUP} (backup, not dropped)")
        print(f"    2. rebuild {TABLE} with match_type NOT NULL DEFAULT '' and")
        print(f"       UNIQUE(brand_id, normalized, match_type)")
        print(f"    3. insert {distinct:,} deduped rows (lowest id per group)")
        print(f"    4. recreate indexes on normalized and brand_id")
        print(f"    5. foreign_key_check + verify counts")
        conn.close()
        return

    lock_preflight(conn)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA foreign_keys = OFF")

    try:
        conn.execute("BEGIN")
        # guard against a prior aborted run
        conn.execute(f"DROP TABLE IF EXISTS {TABLE}_new")
        # 1. new table with the corrected schema
        conn.execute(f"""
            CREATE TABLE {TABLE}_new (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                brand_id     INTEGER NOT NULL REFERENCES brand_registry(id),
                keyword_text TEXT    NOT NULL,
                normalized   TEXT    NOT NULL,
                match_type   TEXT    NOT NULL DEFAULT '',
                source_sheet TEXT,
                UNIQUE (brand_id, normalized, match_type)
            )""")
        # 2. deduped insert — lowest id per (brand_id, normalized, COALESCE(mt,''))
        conn.execute(f"""
            INSERT INTO {TABLE}_new (brand_id, keyword_text, normalized, match_type, source_sheet)
            SELECT brand_id, keyword_text, normalized, COALESCE(match_type,''), source_sheet
            FROM {TABLE}
            WHERE id IN (
                SELECT MIN(id) FROM {TABLE}
                GROUP BY brand_id, normalized, COALESCE(match_type,''))""")
        new_count = conn.execute(f"SELECT COUNT(*) c FROM {TABLE}_new").fetchone()["c"]
        new_nulls = conn.execute(
            f"SELECT COUNT(*) c FROM {TABLE}_new WHERE match_type IS NULL").fetchone()["c"]

        # 3. verify BEFORE swap
        if new_count != distinct:
            conn.execute("ROLLBACK")
            sys.exit(f"  ABORT: new row count {new_count:,} != expected {distinct:,}. No changes made.")
        if new_nulls != 0:
            conn.execute("ROLLBACK")
            sys.exit(f"  ABORT: {new_nulls} NULL match_type rows in new table. No changes made.")

        # 4. swap (rename old to backup, not drop)
        conn.execute(f"ALTER TABLE {TABLE} RENAME TO {BACKUP}")
        conn.execute(f"ALTER TABLE {TABLE}_new RENAME TO {TABLE}")
        # 5. indexes for tagger lookups (normalized) and brand joins (brand_id)
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_brk_normalized ON {TABLE}(normalized)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_brk_brand ON {TABLE}(brand_id)")

        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk:
            conn.execute("ROLLBACK")
            sys.exit(f"  ABORT: foreign_key_check found {len(fk)} violations. No changes made.")

        conn.execute("COMMIT")
    except Exception as e:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")

    final = conn.execute(f"SELECT COUNT(*) c FROM {TABLE}").fetchone()["c"]
    bkp = conn.execute(f"SELECT COUNT(*) c FROM {BACKUP}").fetchone()["c"]
    print(f"\n  MIGRATION COMPLETE")
    print(f"    {TABLE}: {final:,} rows (was {total:,})")
    print(f"    backup {BACKUP}: {bkp:,} rows preserved (drop after you've verified)")
    print(f"    new constraint: UNIQUE(brand_id, normalized, match_type), match_type NOT NULL DEFAULT ''")
    print(f"\n  NEXT: update the 07 insert path (match_type '' not NULL, "
          f"ON CONFLICT(brand_id, normalized, match_type)) BEFORE re-running 07.")
    conn.close()


if __name__ == "__main__":
    main()
