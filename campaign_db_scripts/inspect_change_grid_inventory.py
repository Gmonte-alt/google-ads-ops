"""
inspect_change_grid_inventory.py
================================
Inventory of what's actually in microsoft_change_grid_raw — avoids the
nested-quote escaping problems of a cmd one-liner.

Prints:
  1. Does the table exist? How many rows total / for account 2?
  2. Date range of captured change events
  3. Every distinct attribute_changed value with its count
  4. A few sample rows so we can see the real shape

Run:
    python inspect_change_grid_inventory.py
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
MS_ACCOUNT_ID = 2


def main():
    print(f"DB path: {DB_PATH}")
    print(f"Exists : {DB_PATH.exists()}")
    if not DB_PATH.exists():
        print("ERROR: database file not found at that path. Check the path.")
        return 1

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # 1. Does the table exist?
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='microsoft_change_grid_raw'"
    ).fetchone()
    if not exists:
        print("\nERROR: table microsoft_change_grid_raw does not exist.")
        print("Was 11_create_change_history_schema.py --apply run against THIS db?")
        conn.close()
        return 1
    print("\nTable microsoft_change_grid_raw: EXISTS")

    # 2. Row counts
    total = conn.execute("SELECT COUNT(*) FROM microsoft_change_grid_raw").fetchone()[0]
    acct = conn.execute(
        "SELECT COUNT(*) FROM microsoft_change_grid_raw WHERE account_id = ?",
        (MS_ACCOUNT_ID,)
    ).fetchone()[0]
    print(f"  Total rows           : {total:,}")
    print(f"  Rows for account {MS_ACCOUNT_ID}   : {acct:,}")

    if acct == 0:
        print("\n  Table is empty for account 2. Either the load didn't run,")
        print("  ran against a different account, or ran against a different db file.")
        # Show what account_ids ARE present, if any
        rows = conn.execute(
            "SELECT account_id, COUNT(*) AS n FROM microsoft_change_grid_raw GROUP BY account_id"
        ).fetchall()
        if rows:
            print("  account_ids present in the table:")
            for r in rows:
                print(f"    account_id={r['account_id']}  rows={r['n']:,}")
        conn.close()
        return 0

    # 3. Date range
    dr = conn.execute("""
        SELECT MIN(change_date) AS mn, MAX(change_date) AS mx
        FROM microsoft_change_grid_raw
        WHERE account_id = ? AND change_date IS NOT NULL AND change_date != ''
    """, (MS_ACCOUNT_ID,)).fetchone()
    print(f"  Change-date range    : {dr['mn']}  ->  {dr['mx']}")

    # 4. Attribute inventory
    print("\nDistinct attribute_changed values (account 2):")
    print(f"  {'attribute_changed':<40} {'count':>8}")
    print(f"  {'-'*40} {'-'*8}")
    rows = conn.execute("""
        SELECT attribute_changed, COUNT(*) AS n
        FROM microsoft_change_grid_raw
        WHERE account_id = ?
        GROUP BY attribute_changed
        ORDER BY n DESC
    """, (MS_ACCOUNT_ID,)).fetchall()
    for r in rows:
        print(f"  {(r['attribute_changed'] or '(null)'):<40} {r['n']:>8,}")

    # 5. Sample rows
    print("\nSample rows (first 5 for account 2):")
    rows = conn.execute("""
        SELECT change_date, attribute_changed, item_changed, old_value, new_value
        FROM microsoft_change_grid_raw
        WHERE account_id = ?
        LIMIT 5
    """, (MS_ACCOUNT_ID,)).fetchall()
    for r in rows:
        print(f"  {r['change_date']} | {r['attribute_changed']} | "
              f"item={r['item_changed']!r} | {r['old_value']!r} -> {r['new_value']!r}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
