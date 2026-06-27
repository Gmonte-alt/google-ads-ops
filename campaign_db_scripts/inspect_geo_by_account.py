"""
inspect_geo_by_account.py  — read-only: geo coverage PER ACCOUNT (no mixing)
============================================================================
The geo tables hold every account (Google + Microsoft) keyed on account_id.
A table-wide MAX(date) mixes platforms and is meaningless for freshness. This
breaks each geo table down by account so Google (id=1) freshness is read on its
own, never blended with Microsoft (id=2/54).

READ-ONLY.

    python inspect_geo_by_account.py
"""

import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
CANDIDATES = ["geo_performance_daily", "campaign_performance_geo_daily"]
GOOGLE_HOME = 1


def exists(conn, t):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
    ).fetchone() is not None


def main():
    if not DB.exists():
        sys.exit(f"DB not found: {DB}")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    for t in CANDIDATES:
        print("=" * 74)
        print(f"  {t} — coverage by account")
        print("=" * 74)
        if not exists(conn, t):
            print("  (table does not exist)\n")
            continue

        rows = conn.execute(f"""
            SELECT g.account_id,
                   a.platform,
                   a.account_name,
                   COUNT(*)        AS rows,
                   MIN(g.date)     AS lo,
                   MAX(g.date)     AS hi
            FROM {t} g
            LEFT JOIN accounts a ON a.id = g.account_id
            GROUP BY g.account_id
            ORDER BY g.account_id
        """).fetchall()
        print(f"  {'acct':>4}  {'platform':<10} {'name':<18} {'rows':>10}  "
              f"{'min date':<12} {'max date':<12}")
        print(f"  {'-'*4}  {'-'*10} {'-'*18} {'-'*10}  {'-'*12} {'-'*12}")
        for r in rows:
            print(f"  {r['account_id']:>4}  {str(r['platform']):<10} "
                  f"{str(r['account_name'])[:18]:<18} {r['rows']:>10,}  "
                  f"{str(r['lo']):<12} {str(r['hi']):<12}")

        # Google Home device coverage, on its own
        print(f"\n  Google Home (account_id={GOOGLE_HOME}) device values:")
        drows = conn.execute(f"""
            SELECT device, COUNT(*) AS n, MAX(date) AS hi
            FROM {t} WHERE account_id = ?
            GROUP BY device ORDER BY n DESC
        """, (GOOGLE_HOME,)).fetchall()
        if not drows:
            print("    (no rows for account_id=1)")
        for r in drows:
            print(f"    {str(r['device']):<14} {r['n']:>10,}   max date {r['hi']}")
        print()

    conn.close()
    print("=" * 74)
    print("  Read the account_id=1 (Google) row only for this analysis.")
    print("=" * 74)


if __name__ == "__main__":
    main()
