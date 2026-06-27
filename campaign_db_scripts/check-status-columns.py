"""
check_status_columns.py
-----------------------
Confirms the campaigns and ad_groups table names + column names for the
status fields and join keys.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # All tables
    tables = [r["name"] for r in cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()]

    # Find campaign-ish and ad-group-ish tables
    candidates = [t for t in tables if
                  "campaign" in t.lower() or
                  "ad_group" in t.lower() or
                  "adgroup" in t.lower()]

    print("=" * 70)
    print("Candidate tables and their columns")
    print("=" * 70)

    for table in candidates:
        cols = cursor.execute(f"PRAGMA table_info({table})").fetchall()
        n    = cursor.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"\n── {table}  [{n:,} rows] ──")
        for c in cols:
            print(f"    {c['name']:<30} {c['type']}")


if __name__ == "__main__":
    main()