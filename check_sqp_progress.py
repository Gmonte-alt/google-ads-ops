"""
check_sqp_progress.py  — read-only: is the H2 pull progressing, and why slow?
=============================================================================
Safe to run in a SECOND terminal while the sync is writing (WAL allows a
concurrent reader). Run it twice a minute apart: if the counts climb, the sync
is working, just slow.

  - Progress: sqp rows + MAX(report_date) in the in-progress window.
  - Root-cause test: is search_queries.normalized_text indexed? (the per-row
    lookups in get_or_create_query scan this table; unindexed = the slowdown.)

    python check_sqp_progress.py
"""

import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
WIN_LO, WIN_HI = "2025-07-01", "2025-12-31"


def main():
    conn = sqlite3.connect(DB_PATH, timeout=2.0)
    conn.row_factory = sqlite3.Row

    print("=" * 64)
    print("  sqp progress + slowness diagnostic (read-only)")
    print("=" * 64)

    # Progress in the in-flight window
    r = conn.execute(f"""
        SELECT COUNT(*) AS rows, MAX(report_date) AS hi,
               COUNT(DISTINCT sqp.campaign_id) AS camps
        FROM search_query_performance sqp
        JOIN keyword_instances ki ON ki.id = sqp.instance_id
        WHERE ki.account_id = 1 AND sqp.device='ALL'
          AND sqp.report_date BETWEEN ? AND ?
    """, (WIN_LO, WIN_HI)).fetchone()
    print(f"\n  In-flight window {WIN_LO}..{WIN_HI}:")
    print(f"    rows so far : {r['rows']:,}")
    print(f"    max date    : {r['hi']}")
    print(f"    campaigns   : {r['camps']}")
    print("    (run again in ~60s — if rows climb, it's working, just slow)")

    # Table sizes
    sq = conn.execute("SELECT COUNT(*) AS n FROM search_queries").fetchone()["n"]
    sp = conn.execute("SELECT COUNT(*) AS n FROM search_query_performance").fetchone()["n"]
    print(f"\n  Table sizes: search_queries={sq:,}  search_query_performance={sp:,}")

    # Root-cause: is normalized_text indexed?
    print("\n  search_queries indexes:")
    indexed_cols = set()
    for idx in conn.execute("PRAGMA index_list(search_queries)").fetchall():
        cols = [c["name"] for c in conn.execute(f"PRAGMA index_info({idx['name']})").fetchall()]
        indexed_cols.update(cols)
        print(f"    {idx['name']:<42} unique={idx['unique']} cols=({', '.join(cols)})")
    if "normalized_text" in indexed_cols:
        print("\n  => normalized_text IS indexed — lookups are fast; slowness is")
        print("     mostly the O(conv x traffic) match loop on big campaigns.")
    else:
        print("\n  => normalized_text is NOT indexed — every written row triggers")
        print(f"     full scans of {sq:,} search_queries rows. THIS is the slowdown.")
        print("     Adding an index makes writes dramatically faster (see chat).")

    conn.close()


if __name__ == "__main__":
    main()
