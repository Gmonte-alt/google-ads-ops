"""
verify_campaigns_schema.py
==========================
The geo thread guessed at column names on the campaigns table for the
snapshot-field merge in the geo loader. This verifies the actual schema
so we don't waste a round-trip on column-name mismatches.

Run:
    python verify_campaigns_schema.py
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")


def main():
    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}")
        return 1

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # CREATE statement for campaigns
    print("=" * 78)
    print("campaigns table CREATE statement")
    print("=" * 78)
    row = cur.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='campaigns'"
    ).fetchone()
    if row:
        print(row[0])
    print()

    # Sample Microsoft campaign row
    print("=" * 78)
    print("Sample Microsoft campaign row (account_id=2)")
    print("=" * 78)
    row = cur.execute(
        "SELECT * FROM campaigns WHERE account_id = 2 LIMIT 1"
    ).fetchone()
    if row:
        col_names = [d[0] for d in cur.description]
        for name, val in zip(col_names, row):
            print(f"  {name:<35} = {val!r}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
