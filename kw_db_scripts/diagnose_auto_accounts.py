"""
diagnose_auto_accounts.py
-------------------------
Confirms whether the auto accounts exist in the accounts table at all,
and shows ALL conversion_actions rows in the database so we can see
which accounts DID get seeded.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # ── 1. Every account in the database ─────────────────────────────
    print("=" * 78)
    print("All accounts in the database")
    print("=" * 78)
    rows = cursor.execute(
        """
        SELECT id, account_name, platform, platform_account_id, mcc_id
        FROM accounts
        ORDER BY platform, account_name
        """
    ).fetchall()
    if not rows:
        print("  (none)")
    else:
        print(f"\n  {'id':<4} {'platform':<10} {'platform_account_id':<22} "
              f"{'mcc_id':<14} account_name")
        print(f"  {'-'*4} {'-'*10} {'-'*22} {'-'*14} {'-'*40}")
        for r in rows:
            print(f"  {r['id']:<4} {r['platform']:<10} "
                  f"{r['platform_account_id']:<22} "
                  f"{(r['mcc_id'] or ''):<14} {r['account_name']}")

    # ── 2. conversion_actions row count per account ──────────────────
    print("\n" + "=" * 78)
    print("conversion_actions seeded per account")
    print("=" * 78)
    rows = cursor.execute(
        """
        SELECT
            a.id              AS account_id,
            a.account_name,
            a.platform,
            COUNT(ca.id)      AS n_actions
        FROM accounts a
        LEFT JOIN conversion_actions ca ON ca.account_id = a.id
        GROUP BY a.id, a.account_name, a.platform
        ORDER BY a.platform, a.account_name
        """
    ).fetchall()
    print(f"\n  {'account_name':<45} {'platform':<10} {'n_actions':>10}")
    print(f"  {'-'*45} {'-'*10} {'-'*10}")
    for r in rows:
        flag = "  ← EMPTY" if r["n_actions"] == 0 else ""
        print(f"  {r['account_name']:<45} {r['platform']:<10} "
              f"{r['n_actions']:>10}{flag}")

    # ── 3. Distinct conversion action names actually present in the
    #      performance API data — useful for re-seeding correctly ────
    print("\n" + "=" * 78)
    print("Performance row counts per account (sanity check)")
    print("=" * 78)
    rows = cursor.execute(
        """
        SELECT
            a.account_name,
            COUNT(kpd.id) AS n_perf_rows,
            MIN(kpd.report_date) AS first_date,
            MAX(kpd.report_date) AS last_date
        FROM accounts a
        LEFT JOIN keyword_instances ki        ON ki.account_id = a.id
        LEFT JOIN keyword_performance_daily kpd ON kpd.instance_id = ki.id
        WHERE a.platform = 'google'
        GROUP BY a.account_name
        ORDER BY a.account_name
        """
    ).fetchall()
    print(f"\n  {'account_name':<45} {'n_perf_rows':>12} "
          f"{'first':<12} {'last':<12}")
    print(f"  {'-'*45} {'-'*12} {'-'*12} {'-'*12}")
    for r in rows:
        print(f"  {r['account_name']:<45} {r['n_perf_rows'] or 0:>12,} "
              f"{(r['first_date'] or '—'):<12} {(r['last_date'] or '—'):<12}")

    conn.close()


if __name__ == "__main__":
    main()