"""
diagnose_auto_routing.py
------------------------
Inspects the conversion_actions table for both auto accounts and prints
the routing currently seeded in paid_search.db.

Compare the output against the live conversion action names in each
Google Ads account UI (Tools -> Conversions). Any name mismatch =
revenue silently routed to 'monitor' instead of 'primary'.

Usage:
    conda activate paid-search
    python kw_db_scripts/diagnose_auto_routing.py
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")


def main():
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Database not found at {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    print("=" * 78)
    print("Auto Accounts — Conversion Routing Audit")
    print(f"DB: {DB_PATH}")
    print("=" * 78)

    # ── 1. Show the seeded routing for both auto accounts ────────────
    rows = cursor.execute(
        """
        SELECT
            a.account_name,
            a.platform_account_id,
            ca.conversion_name,
            ca.include_mode,
            ca.notes
        FROM conversion_actions ca
        JOIN accounts a ON ca.account_id = a.id
        WHERE a.platform = 'google'
          AND (a.account_name LIKE '%Auto%' OR a.account_name LIKE '%auto%')
        ORDER BY a.account_name, ca.include_mode, ca.conversion_name
        """
    ).fetchall()

    if not rows:
        print("\n  No auto accounts found in conversion_actions table.")
        print("  Check accounts table — were the auto accounts seeded?")
        conn.close()
        return

    current_account = None
    for row in rows:
        if row["account_name"] != current_account:
            current_account = row["account_name"]
            print(f"\n── {current_account}  (Google ID: {row['platform_account_id']}) ──")
            print(f"  {'INCLUDE_MODE':<18} {'CONVERSION_NAME':<55}")
            print(f"  {'-' * 18} {'-' * 55}")
        print(f"  {row['include_mode']:<18} {row['conversion_name']:<55}")
        if row["notes"]:
            # Wrap notes to keep readable
            notes = row["notes"]
            print(f"  {'':<18}   ↳ {notes[:100]}{'...' if len(notes) > 100 else ''}")

    # ── 2. Summary count per account by include_mode ────────────────
    print("\n" + "=" * 78)
    print("Routing summary by mode")
    print("=" * 78)

    summary = cursor.execute(
        """
        SELECT
            a.account_name,
            ca.include_mode,
            COUNT(*) AS n
        FROM conversion_actions ca
        JOIN accounts a ON ca.account_id = a.id
        WHERE a.platform = 'google'
          AND (a.account_name LIKE '%Auto%' OR a.account_name LIKE '%auto%')
        GROUP BY a.account_name, ca.include_mode
        ORDER BY a.account_name, ca.include_mode
        """
    ).fetchall()

    current_account = None
    for row in summary:
        if row["account_name"] != current_account:
            current_account = row["account_name"]
            print(f"\n  {current_account}")
        print(f"    {row['include_mode']:<18} {row['n']:>3}")

    # ── 3. Recent revenue check (sanity) ─────────────────────────────
    print("\n" + "=" * 78)
    print("Last 30 days — primary revenue routed (sanity check)")
    print("=" * 78)

    revenue = cursor.execute(
        """
        SELECT
            a.account_name,
            ROUND(SUM(kpd.cost_micros) / 1e6, 2)        AS spend,
            ROUND(SUM(kpd.primary_conv_count), 1)       AS primary_conv,
            ROUND(SUM(kpd.primary_conv_value), 2)       AS primary_revenue,
            ROUND(SUM(kpd.all_conv_value), 2)           AS all_conv_revenue
        FROM keyword_performance_daily kpd
        JOIN keyword_instances ki ON kpd.instance_id = ki.id
        JOIN accounts a           ON ki.account_id   = a.id
        WHERE a.platform = 'google'
          AND (a.account_name LIKE '%Auto%' OR a.account_name LIKE '%auto%')
          AND kpd.device    = 'ALL'
          AND kpd.geo_state = 'ALL'
          AND kpd.report_date >= DATE('now', '-30 days')
        GROUP BY a.account_name
        ORDER BY a.account_name
        """
    ).fetchall()

    if not revenue:
        print("  No performance data in last 30 days for auto accounts.")
    else:
        print(f"\n  {'Account':<40} {'Spend':>12} {'Prim Conv':>10} "
              f"{'Prim Rev':>12} {'All Rev':>12}")
        print(f"  {'-' * 40} {'-' * 12} {'-' * 10} {'-' * 12} {'-' * 12}")
        for row in revenue:
            print(f"  {row['account_name']:<40} "
                  f"${row['spend']:>11,.2f} "
                  f"{row['primary_conv']:>10,.1f} "
                  f"${row['primary_revenue']:>11,.2f} "
                  f"${row['all_conv_revenue']:>11,.2f}")

        print("\n  Diagnostic hint:")
        print("    If 'All Rev' >> 'Prim Rev', conversion actions are firing")
        print("    but not routed as 'primary' — likely a name mismatch.")

    conn.close()
    print("\n" + "=" * 78)
    print("Done. Next step: compare CONVERSION_NAME values above against the")
    print("live names in Google Ads UI (Tools -> Conversions) for each account.")
    print("=" * 78)


if __name__ == "__main__":
    main()