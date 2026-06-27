"""
migrate_sqp_add_matchtype_key.py  — widen the sqp uniqueness key
================================================================
Adds match_type_served to the UNIQUE key of search_query_performance so that
rows differing only by match type are no longer collapsed (last-write-wins).

  OLD: UNIQUE (query_id, instance_id, report_date, device)
  NEW: UNIQUE (query_id, instance_id, report_date, device, match_type_served)

The constraint is declared inline in CREATE TABLE (a table-level UNIQUE, backed
by sqlite_autoindex), which SQLite cannot drop in place — so this does a
create-copy-drop-rename rebuild inside one transaction. Every existing row is
copied 1:1 (each is already unique under the new key, since the table currently
holds one row per old 4-col key), so the row count is unchanged by the migration
itself. The previously-collapsed sibling rows are recovered by RE-PULLING after
this runs (see the note printed at the end).

SAFETY
  - Backs up the .db file (WAL-checkpointed) before touching anything.
  - Guard: if the table already has the 5-column UNIQUE key, it exits without
    changes (safe to run twice).
  - Wrapped in a transaction; verifies post-count == pre-count and runs
    foreign_key_check before committing the rename.

    python migrate_sqp_add_matchtype_key.py
"""

import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
TABLE   = "search_query_performance"

NEW_TABLE_DDL = """
CREATE TABLE search_query_performance_new (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    query_id             INTEGER NOT NULL REFERENCES search_queries(id),
    instance_id          INTEGER NOT NULL REFERENCES keyword_instances(id),
    campaign_id          TEXT,
    platform             TEXT    NOT NULL,
    report_date          TEXT    NOT NULL,
    device               TEXT    NOT NULL,
    match_type_served    TEXT    NOT NULL DEFAULT 'UNKNOWN',
    impressions          INTEGER DEFAULT 0,
    clicks               INTEGER DEFAULT 0,
    cost_micros          REAL    DEFAULT 0,
    primary_conv_count   REAL    DEFAULT 0,
    primary_conv_value   REAL    DEFAULT 0,
    secondary_conv_count REAL    DEFAULT 0,
    all_conv_count       REAL    DEFAULT 0,
    all_conv_value       REAL    DEFAULT 0,
    UNIQUE (query_id, instance_id, report_date, device, match_type_served)
)
"""

COPY_SQL = """
INSERT INTO search_query_performance_new
    (id, query_id, instance_id, campaign_id, platform, report_date, device,
     match_type_served, impressions, clicks, cost_micros,
     primary_conv_count, primary_conv_value, secondary_conv_count,
     all_conv_count, all_conv_value)
SELECT
     id, query_id, instance_id, campaign_id, platform, report_date, device,
     COALESCE(match_type_served, 'UNKNOWN'), impressions, clicks, cost_micros,
     primary_conv_count, primary_conv_value, secondary_conv_count,
     all_conv_count, all_conv_value
FROM search_query_performance
"""

SECONDARY_INDEXES = [
    "CREATE INDEX idx_sqp_query_date    ON search_query_performance(query_id, report_date)",
    "CREATE INDEX idx_sqp_campaign_date ON search_query_performance(campaign_id, report_date)",
    "CREATE INDEX idx_sqp_platform_date ON search_query_performance(platform, report_date)",
    "CREATE INDEX idx_sqp_instance_date ON search_query_performance(instance_id, report_date)",
    "CREATE INDEX idx_sqp_date_device   ON search_query_performance(report_date, device)",
]


def already_migrated(conn) -> bool:
    """True if the table's UNIQUE(...) clause already names match_type_served."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
    ).fetchone()
    if not row:
        sys.exit(f"  Table {TABLE} not found.")
    sql = " ".join(row[0].split())  # normalize whitespace
    after_unique = sql.split("UNIQUE", 1)[1] if "UNIQUE" in sql else ""
    # The UNIQUE clause is the parenthesized group right after the keyword
    clause = after_unique.split(")", 1)[0] if ")" in after_unique else after_unique
    return "match_type_served" in clause


def backup_db() -> Path:
    # Fold WAL into the main file so a plain file copy is a clean snapshot.
    c = sqlite3.connect(DB_PATH)
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = DB_PATH.with_name(f"{DB_PATH.stem}.bak_premigration_{stamp}.db")
    shutil.copy2(DB_PATH, bak)
    return bak


def main():
    if not DB_PATH.exists():
        sys.exit(f"DB not found: {DB_PATH}")

    # Guard check on a throwaway connection
    guard = sqlite3.connect(DB_PATH)
    if already_migrated(guard):
        guard.close()
        print("Already migrated: search_query_performance already includes "
              "match_type_served in its UNIQUE key. No changes made.")
        return
    guard.close()

    print("=" * 70)
    print("  Migrating search_query_performance UNIQUE key")
    print("=" * 70)

    bak = backup_db()
    print(f"  Backup written: {bak}")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    pre = conn.execute(f"SELECT COUNT(*) AS n FROM {TABLE}").fetchone()["n"]
    pre_key = conn.execute(f"""
        SELECT COUNT(*) AS n FROM (
            SELECT 1 FROM {TABLE}
            GROUP BY query_id, instance_id, report_date, device
        )
    """).fetchone()["n"]
    print(f"  Pre-migration rows: {pre:,}")
    print(f"  Distinct old 4-col keys: {pre_key:,}  (should equal row count)")

    # foreign_keys pragma can't be toggled inside a transaction
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN")
        conn.execute(NEW_TABLE_DDL)
        conn.execute(COPY_SQL)
        post = conn.execute(
            "SELECT COUNT(*) AS n FROM search_query_performance_new"
        ).fetchone()["n"]
        if post != pre:
            conn.execute("ROLLBACK")
            sys.exit(f"  ABORT: copied {post:,} != pre {pre:,}. Rolled back; "
                     f"DB unchanged. Backup at {bak}")
        conn.execute("DROP TABLE search_query_performance")
        conn.execute("ALTER TABLE search_query_performance_new "
                     "RENAME TO search_query_performance")
        for ddl in SECONDARY_INDEXES:
            conn.execute(ddl)
        conn.execute("COMMIT")
    except Exception as e:
        conn.execute("ROLLBACK")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.close()
        sys.exit(f"  ABORT: {e}\n  Rolled back; DB unchanged. Backup at {bak}")

    # Post-checks
    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    conn.execute("PRAGMA foreign_keys = ON")
    final = conn.execute(f"SELECT COUNT(*) AS n FROM {TABLE}").fetchone()["n"]
    new_key = conn.execute(f"""
        SELECT COUNT(*) AS n FROM (
            SELECT 1 FROM {TABLE}
            GROUP BY query_id, instance_id, report_date, device, match_type_served
        )
    """).fetchone()["n"]
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
    ).fetchone()[0]
    idx = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=? "
        "AND sql IS NOT NULL", (TABLE,)).fetchall()]
    conn.close()

    print(f"  Post-migration rows: {final:,}")
    print(f"  Distinct new 5-col keys: {new_key:,}")
    print(f"  foreign_key_check violations: {len(fk)}")
    print(f"  Secondary indexes recreated: {len(idx)}  {idx}")
    print("\n  New UNIQUE key in DDL:")
    for ln in ddl.splitlines():
        if "UNIQUE" in ln:
            print(f"    {ln.strip()}")

    if final == pre and len(fk) == 0:
        print("\n  ✓ MIGRATION OK")
        print("  Next: deploy v6 of 05_sync_search_queries.py (ON CONFLICT now")
        print("  targets the 5-col key), then RE-PULL all windows to recover the")
        print("  previously-collapsed match-type rows. After re-pull, the sync's")
        print("  'written' will equal table rows for each window (no collapse).")
    else:
        print("\n  ⚠ REVIEW: counts or FK check unexpected. Backup at:", bak)


if __name__ == "__main__":
    main()
