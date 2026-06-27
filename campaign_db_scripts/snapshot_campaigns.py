"""
snapshot_campaigns.py
=====================
Insurance snapshot of the campaigns table before the campaign restructure
rename lands in the DB. Captures the old name → campaign_id mapping in case
it's needed later for a campaign_name_history table backfill.

Run BEFORE the next monthly full refresh (loader 08).
Safe to run multiple times — will skip if the snapshot table already exists.

Usage:
    python snapshot_campaigns.py
"""

import sqlite3
import sys
from pathlib import Path
from datetime import date

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
SNAPSHOT_TABLE = "campaigns_snapshot_pre_restructure_20260603"


def main():
    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}")
        return 1

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Check if snapshot already exists
    existing = cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (SNAPSHOT_TABLE,),
    ).fetchone()
    if existing:
        n = cur.execute(f"SELECT COUNT(*) FROM {SNAPSHOT_TABLE}").fetchone()[0]
        print(f"Snapshot table '{SNAPSHOT_TABLE}' already exists ({n:,} rows).")
        print("Skipping. Drop and re-run if you want a fresh snapshot.")
        conn.close()
        return 0

    # Source row count
    src_count = cur.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0]
    ms_count = cur.execute(
        "SELECT COUNT(*) FROM campaigns WHERE account_id = 2"
    ).fetchone()[0]
    print(f"Source `campaigns` table: {src_count:,} total rows ({ms_count:,} Microsoft Home)")

    # Create snapshot
    cur.execute(f"""
        CREATE TABLE {SNAPSHOT_TABLE} AS
        SELECT *, DATE() AS snapshot_date
        FROM campaigns
    """)
    conn.commit()

    # Verify
    snap_count = cur.execute(f"SELECT COUNT(*) FROM {SNAPSHOT_TABLE}").fetchone()[0]
    snap_date = cur.execute(
        f"SELECT DISTINCT snapshot_date FROM {SNAPSHOT_TABLE}"
    ).fetchone()[0]

    print(f"Snapshot created: '{SNAPSHOT_TABLE}'")
    print(f"  Rows: {snap_count:,}")
    print(f"  Snapshot date: {snap_date}")

    if snap_count != src_count:
        print(f"WARNING: row count mismatch. src={src_count}, snapshot={snap_count}")
        conn.close()
        return 1

    # Show a few sample rows
    sample = cur.execute(f"""
        SELECT campaign_id, campaign_name, status
        FROM {SNAPSHOT_TABLE}
        WHERE account_id = 2
        ORDER BY campaign_name
        LIMIT 5
    """).fetchall()
    print(f"\nSample Microsoft Home campaign names captured:")
    for row in sample:
        print(f"  {row[0]:<14} {row[2]:<10} {row[1]}")

    conn.close()
    print(f"\nDone. Old names safe. Proceed with full refresh when ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
