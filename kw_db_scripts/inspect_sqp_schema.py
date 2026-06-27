"""
inspect_sqp_schema.py  — read-only: facts needed to plan the match_type fix
===========================================================================
Dumps everything needed to decide HOW to add match_type_served to the sqp
uniqueness key, and checks whether keyword_instances resolution is unambiguous.

  1. search_query_performance CREATE TABLE statement.
  2. Index list (with origin: pk / unique-constraint / created-index) + columns,
     so we know whether uniqueness is a PRIMARY KEY (needs table rebuild) or a
     UNIQUE INDEX (drop + recreate, no rebuild).
  3. sqp basic counts + match_type_served value distribution + any NULLs.
  4. keyword_instances duplicate check: how many (account_id, ad_group_id,
     criterion_id) tuples map to MORE THAN ONE instance row. >0 means
     get_instance_id()'s fetchone() can grab the wrong keyword.

READ-ONLY. No writes.

    python inspect_sqp_schema.py
"""

import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
TABLE = "search_query_performance"


def main():
    if not DB_PATH.exists():
        sys.exit(f"DB not found: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    print("=" * 72)
    print(f"  schema + integrity inspection: {TABLE}")
    print("=" * 72)

    # 1. CREATE TABLE
    print("\n  1. CREATE TABLE")
    print("  " + "-" * 68)
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
    ).fetchone()
    print(row["sql"] if row else "  (table not found)")

    # 2. Indexes + origins + columns
    print("\n  2. INDEXES (origin: pk=PRIMARY KEY, u=UNIQUE constraint, c=CREATE INDEX)")
    print("  " + "-" * 68)
    for idx in conn.execute(f"PRAGMA index_list({TABLE})").fetchall():
        # keys: seq, name, unique, origin, partial
        cols = [c["name"] for c in
                conn.execute(f"PRAGMA index_info({idx['name']})").fetchall()]
        print(f"    {idx['name']:<40} unique={idx['unique']} "
              f"origin={idx['origin']}  cols=({', '.join(cols)})")
    # Also show the CREATE INDEX sql for any explicit indexes
    for r in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name=? "
        "AND sql IS NOT NULL", (TABLE,)
    ).fetchall():
        print(f"    sql: {r['sql']}")

    # 3. sqp counts + match_type distribution
    print("\n  3. sqp counts + match_type_served distribution")
    print("  " + "-" * 68)
    total = conn.execute(f"SELECT COUNT(*) AS n FROM {TABLE}").fetchone()["n"]
    print(f"    total rows: {total:,}")
    nulls = conn.execute(
        f"SELECT COUNT(*) AS n FROM {TABLE} WHERE match_type_served IS NULL"
    ).fetchone()["n"]
    print(f"    rows with NULL match_type_served: {nulls:,}")
    print("    match_type_served values:")
    for r in conn.execute(f"""
        SELECT COALESCE(match_type_served,'(null)') AS mt, COUNT(*) AS n
        FROM {TABLE} GROUP BY mt ORDER BY n DESC
    """).fetchall():
        print(f"      {r['mt']:<16} {r['n']:>10,}")

    # 4. keyword_instances duplicate resolution check
    print("\n  4. keyword_instances resolution uniqueness")
    print("  " + "-" * 68)
    dup = conn.execute("""
        SELECT COUNT(*) AS dup_tuples, COALESCE(SUM(n),0) AS dup_rows
        FROM (
            SELECT account_id, ad_group_id, criterion_id, COUNT(*) AS n
            FROM keyword_instances
            GROUP BY account_id, ad_group_id, criterion_id
            HAVING COUNT(*) > 1
        )
    """).fetchone()
    print(f"    (account_id, ad_group_id, criterion_id) tuples with >1 instance: "
          f"{dup['dup_tuples']:,}")
    print(f"    total instance rows involved in those collisions: {dup['dup_rows']:,}")
    if dup["dup_tuples"]:
        print("    => get_instance_id() fetchone() is AMBIGUOUS for these — sample:")
        for r in conn.execute("""
            SELECT account_id, ad_group_id, criterion_id, COUNT(*) AS n
            FROM keyword_instances
            GROUP BY account_id, ad_group_id, criterion_id
            HAVING COUNT(*) > 1
            ORDER BY n DESC LIMIT 5
        """).fetchall():
            print(f"       acct={r['account_id']} ag={r['ad_group_id']} "
                  f"crit={r['criterion_id']}  instances={r['n']}")
    else:
        print("    => clean: every (account, ad_group, criterion) maps to exactly one instance.")

    conn.close()
    print("\n" + "=" * 72)
    print("  Paste this output back — it determines index-swap vs table-rebuild.")
    print("=" * 72)


if __name__ == "__main__":
    main()
