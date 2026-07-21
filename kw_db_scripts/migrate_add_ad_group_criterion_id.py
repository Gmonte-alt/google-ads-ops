#!/usr/bin/env python
"""
migrate_add_ad_group_criterion_id.py  --  Keyword remediation Step 1.

Adds the per-placement edit key `ad_group_criterion_id` = '{ad_group_id}~{criterion_id}' to
keyword_instances and backfills it from existing columns. This is the Google Ads `ad_group_criterion`
resource id that Editor / the API mutate on -- with it, programmatic edits match an existing keyword
by immutable id instead of fragile text+match_type matching.

ADDITIVE and SAFE:
  - Pure derivation from columns already present (ad_group_id, criterion_id). No API needed.
  - Dry-run by default; --apply to write.
  - Backs up the table (keyword_instances_pre_agc) before writing.
  - Requires explicit --db (no production default). Refuses a suspiciously small DB unless --force-small.
  - Verifies the key is unique per placement after backfill (the correctness proof).

NOTE on the UNIQUE constraint: SQLite cannot ADD a table-level UNIQUE constraint to an existing
table via ALTER. We instead create a UNIQUE INDEX, which enforces the same guarantee and is the
correct SQLite idiom. If the uniqueness check finds duplicates, the index creation is skipped and
the collisions are reported (that would mean the grain assumption is violated -- investigate before
forcing).

Usage:
    python migrate_add_ad_group_criterion_id.py --db PATH                 # dry-run
    python migrate_add_ad_group_criterion_id.py --db PATH --apply         # write
    python migrate_add_ad_group_criterion_id.py --db PATH --apply --account 1
"""
import argparse
import os
import sqlite3
import sys

COL = "ad_group_criterion_id"
BACKUP_TABLE = "keyword_instances_pre_agc"
UNIQUE_INDEX = "ux_ki_account_agc"
LOOKUP_INDEX = "idx_ki_account_agc"


def table_columns(conn, table):
    return [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="explicit path to the DB (no default)")
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    ap.add_argument("--account", type=int, default=None,
                    help="optional: restrict backfill to one account_id (default: all)")
    ap.add_argument("--force-small", action="store_true",
                    help="permit --apply against a <10k-row keyword_instances (test only)")
    args = ap.parse_args()

    abspath = os.path.abspath(args.db)
    size = os.path.getsize(args.db) if os.path.exists(args.db) else -1
    print("opening DB: %s  (%s)" % (abspath, ("%d bytes" % size) if size >= 0 else "MISSING"))
    if size < 0:
        sys.exit("[ABORT] DB does not exist at that path.")

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA foreign_keys = ON")

    # --- production-shape guard ---
    total = conn.execute("SELECT COUNT(*) FROM keyword_instances").fetchone()[0]
    print("keyword_instances rows: %s" % f"{total:,}")
    if total < 10000 and args.apply and not args.force_small:
        sys.exit("[ABORT] keyword_instances has only %d rows -- far below the live account. "
                 "This may not be the live DB. Use --force-small to override." % total)

    cols = table_columns(conn, "keyword_instances")
    for req in ("ad_group_id", "criterion_id"):
        if req not in cols:
            sys.exit("[ABORT] required column '%s' missing from keyword_instances." % req)

    have_col = COL in cols
    print("column '%s' present: %s" % (COL, have_col))

    scope = ""
    params = ()
    if args.account is not None:
        scope = " WHERE account_id = ?"
        params = (args.account,)

    # --- preview: what the backfill would compute ---
    to_fill = conn.execute(
        "SELECT COUNT(*) FROM keyword_instances"
        + (" WHERE %s IS NULL" % COL if have_col else "")
        + (" AND account_id = ?" if (have_col and args.account is not None) else
           (" WHERE account_id = ?" if args.account is not None else "")),
        params if args.account is not None else ()
    ).fetchone()[0]
    print("rows that would get %s set: %s" % (COL, f"{to_fill:,}"))

    # --- uniqueness proof (dry-run safe): is (account_id, ag~crit) unique? ---
    dup = conn.execute(
        "SELECT COUNT(*) FROM ("
        "  SELECT account_id, ad_group_id || '~' || criterion_id AS k, COUNT(*) n "
        "  FROM keyword_instances" + scope +
        "  GROUP BY account_id, k HAVING n > 1"
        ")", params
    ).fetchone()[0]
    print("placement collisions (account_id, ad_group~criterion with >1 row): %s" % dup)
    if dup == 0:
        print("  -> key is UNIQUE per placement. Safe to add the unique index.")
    else:
        print("  -> WARNING: %d collisions. Grain assumption violated; will SKIP the unique index "
              "and report. Investigate before forcing." % dup)

    if not args.apply:
        print("\n[dry-run] no changes written. Re-run with --apply.")
        conn.close()
        return

    # ================= APPLY =================
    # 1. backup the table
    print("\n[apply] backing up keyword_instances -> %s ..." % BACKUP_TABLE)
    conn.execute("DROP TABLE IF EXISTS %s" % BACKUP_TABLE)
    conn.execute("CREATE TABLE %s AS SELECT * FROM keyword_instances" % BACKUP_TABLE)
    bkp = conn.execute("SELECT COUNT(*) FROM %s" % BACKUP_TABLE).fetchone()[0]
    print("       backup rows: %s" % f"{bkp:,}")
    if bkp != total:
        conn.rollback()
        sys.exit("[ABORT] backup row count mismatch (%d vs %d)." % (bkp, total))

    # 2. add the column if missing
    if not have_col:
        print("[apply] adding column %s ..." % COL)
        conn.execute("ALTER TABLE keyword_instances ADD COLUMN %s TEXT" % COL)

    # 3. backfill
    print("[apply] backfilling %s = ad_group_id || '~' || criterion_id ..." % COL)
    conn.execute(
        "UPDATE keyword_instances SET %s = ad_group_id || '~' || criterion_id "
        "WHERE %s IS NULL" % (COL, COL)
        + (" AND account_id = ?" if args.account is not None else ""),
        params if args.account is not None else ()
    )

    # 4. lookup index (always safe)
    print("[apply] creating lookup index %s ..." % LOOKUP_INDEX)
    conn.execute("CREATE INDEX IF NOT EXISTS %s ON keyword_instances(account_id, %s)"
                 % (LOOKUP_INDEX, COL))

    # 5. unique index -- only if no collisions
    if dup == 0:
        print("[apply] creating UNIQUE index %s ..." % UNIQUE_INDEX)
        try:
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS %s "
                         "ON keyword_instances(account_id, %s)" % (UNIQUE_INDEX, COL))
        except sqlite3.IntegrityError as e:
            print("  [skip] unique index failed (%s) -- collisions exist across full table; "
                  "the lookup index is in place. Investigate." % e)
    else:
        print("[apply] SKIPPING unique index (%d collisions). Lookup index is in place." % dup)

    conn.commit()

    # 6. verify
    filled = conn.execute(
        "SELECT COUNT(*) FROM keyword_instances WHERE %s IS NOT NULL" % COL
        + (" AND account_id = ?" if args.account is not None else ""),
        params if args.account is not None else ()
    ).fetchone()[0]
    distinct_agc = conn.execute(
        "SELECT COUNT(DISTINCT %s) FROM keyword_instances" % COL + scope, params
    ).fetchone()[0]
    scoped_total = conn.execute(
        "SELECT COUNT(*) FROM keyword_instances" + scope, params
    ).fetchone()[0]
    print("\n[verify] %s populated on %s rows" % (COL, f"{filled:,}"))
    print("[verify] distinct %s = %s ; scoped rows = %s ; match = %s"
          % (COL, f"{distinct_agc:,}", f"{scoped_total:,}", distinct_agc == scoped_total))
    if distinct_agc == scoped_total:
        print("  -> CONFIRMED: ad_group_criterion_id is a true unique key (1 row per placement).")
    else:
        print("  -> NOTE: distinct < rows means either NULLs remain or cross-account overlap; "
              "review before relying on uniqueness.")

    conn.close()
    print("\nDone. Backup retained as %s (drop it once you've confirmed the migration)." % BACKUP_TABLE)


if __name__ == "__main__":
    main()
