"""
check_campaign_perf_is.py
==========================
Diagnostic script to verify that campaign_performance_daily contains
the impression share columns needed for the Efficiency Frontier model.

Checks:
  1. Schema — confirms all four IS columns exist in the table
  2. Population — % of rows where each IS column is NOT NULL, by account
  3. Recent coverage — IS availability for the last 30 / 14 days
  4. Per-campaign IS sample — actual values for the Home account (device=ALL)
     so you can verify against the Google Ads UI before building the model

RUN
───
    conda activate paid-search
    python analysis/check_campaign_perf_is.py
    python analysis/check_campaign_perf_is.py --account home --days 30
"""

import sqlite3
import argparse
from pathlib import Path
from datetime import date, timedelta

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")

IS_COLUMNS = [
    "search_impression_share",
    "search_budget_lost_is",
    "search_rank_lost_is",
    "search_exact_match_is",
]

ACCOUNT_MAP = {
    "home":       "8886502147",
    "medicare":   "5310820901",
    "auto":       "3863660890",
    "auto_quote": "1006729211",
    "term_life":  "1671155176",
}


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def check_schema(conn) -> bool:
    """Confirm IS columns exist in campaign_performance_daily."""
    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(campaign_performance_daily)"
    ).fetchall()}
    print("\n── 1. SCHEMA CHECK ──────────────────────────────────────────")
    all_present = True
    for col in IS_COLUMNS:
        present = col in cols
        status  = "✓" if present else "✗ MISSING"
        print(f"  {status}  {col}")
        if not present:
            all_present = False
    if not all_present:
        print("\n  ⚠  One or more IS columns are missing from the schema.")
        print("     Re-run 01_create_campaign_tables.py to add them,")
        print("     then re-sync with 03_sync_campaign_performance.py.")
    return all_present


def check_population(conn):
    """
    Per account: total rows, null count, and fill % for each IS column
    in campaign_performance_daily where device = 'ALL'.
    """
    print("\n── 2. POPULATION — IS column fill rate (device='ALL') ───────")

    accounts = conn.execute("""
        SELECT a.platform_account_id, a.account_name, a.platform,
               a.id AS account_id
        FROM accounts a
        WHERE a.platform = 'google'
        ORDER BY a.account_name
    """).fetchall()

    if not accounts:
        print("  No Google accounts found in accounts table.")
        return

    for acct in accounts:
        aid  = acct["account_id"]
        name = acct["account_name"] or acct["platform_account_id"]

        totals = conn.execute("""
            SELECT COUNT(*) AS total_rows,
                   MIN(date) AS min_date,
                   MAX(date) AS max_date
            FROM campaign_performance_daily cpd
            JOIN campaigns c ON c.campaign_id = cpd.campaign_id
            WHERE c.account_id = ? AND cpd.device = 'ALL'
        """, (aid,)).fetchone()

        total = totals["total_rows"]
        if total == 0:
            print(f"\n  {name}: no campaign_performance_daily rows")
            continue

        print(f"\n  {name}  ({acct['platform_account_id']})")
        print(f"    Total rows (device=ALL): {total:,}  "
              f"[{totals['min_date']} → {totals['max_date']}]")

        for col in IS_COLUMNS:
            try:
                r = conn.execute(f"""
                    SELECT COUNT(*) AS non_null
                    FROM campaign_performance_daily cpd
                    JOIN campaigns c ON c.campaign_id = cpd.campaign_id
                    WHERE c.account_id = ? AND cpd.device = 'ALL'
                      AND cpd.{col} IS NOT NULL
                """, (aid,)).fetchone()
                non_null = r["non_null"]
                pct      = non_null / total * 100 if total > 0 else 0
                bar      = "█" * int(pct / 5)
                status   = "✓" if pct >= 80 else ("⚠" if pct >= 20 else "✗")
                print(f"    {status}  {col:<35}  "
                      f"{non_null:>7,} / {total:,}  ({pct:5.1f}%)  {bar}")
            except Exception as e:
                print(f"    ✗  {col}: ERROR — {e}")


def check_recent(conn, account_id: int, account_name: str, days: int):
    """
    IS availability for the last N days — the window the model will use.
    """
    end   = date.today() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    print(f"\n── 3. RECENT COVERAGE — last {days} days "
          f"({start} → {end}) ──────────")

    rows = conn.execute("""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN search_rank_lost_is   IS NOT NULL THEN 1 ELSE 0 END) AS rank_nn,
               SUM(CASE WHEN search_budget_lost_is IS NOT NULL THEN 1 ELSE 0 END) AS budget_nn,
               SUM(CASE WHEN search_impression_share IS NOT NULL THEN 1 ELSE 0 END) AS sis_nn,
               MIN(cpd.date) AS min_date,
               MAX(cpd.date) AS max_date
        FROM campaign_performance_daily cpd
        JOIN campaigns c ON c.campaign_id = cpd.campaign_id
        WHERE c.account_id = ?
          AND cpd.device = 'ALL'
          AND cpd.date BETWEEN ? AND ?
    """, (account_id, start.isoformat(), end.isoformat())).fetchone()

    total = rows["total"] or 0
    if total == 0:
        print(f"  ⚠  No rows found for {account_name} in this window.")
        print("     Run: python campaign_db_scripts/03_sync_campaign_performance.py "
              f"--daily-start {start.isoformat()}")
        return

    print(f"  {account_name}: {total:,} rows  "
          f"[{rows['min_date']} → {rows['max_date']}]")
    for label, key in [
        ("search_impression_share", "sis_nn"),
        ("search_rank_lost_is",     "rank_nn"),
        ("search_budget_lost_is",   "budget_nn"),
    ]:
        nn  = rows[key] or 0
        pct = nn / total * 100 if total > 0 else 0
        ok  = "✓" if pct >= 80 else ("⚠" if pct > 0 else "✗ ALL NULL")
        print(f"  {ok}  {label:<35}  {nn:,}/{total:,}  ({pct:.1f}%)")

    if rows["rank_nn"] == 0:
        print("\n  ⚠  search_rank_lost_is is ALL NULL in this window.")
        print("     The Efficiency Frontier model cannot run without this column.")
        print("     Re-sync campaign performance for this date range:")
        print(f"     python campaign_db_scripts/03_sync_campaign_performance.py "
              f"--daily-start {start.isoformat()} --daily-only")


def check_per_campaign(conn, account_id: int, account_name: str,
                        days: int):
    """
    Per-campaign IS values averaged over the window — what the model will use.
    Verify these against your Google Ads UI campaign report.
    """
    end   = date.today() - timedelta(days=1)
    start = end - timedelta(days=days - 1)

    print(f"\n── 4. PER-CAMPAIGN IS VALUES ({days}d, device='ALL') ─────────")
    print(f"  {account_name}  |  {start} → {end}")
    print()

    rows = conn.execute("""
        SELECT
            c.campaign_name,
            c.status,
            COUNT(cpd.date)                              AS days_in_window,
            ROUND(SUM(cpd.cost_micros)/1e6, 2)          AS spend,
            SUM(cpd.clicks)                              AS clicks,
            SUM(cpd.conversions)                         AS convs,
            ROUND(AVG(cpd.search_impression_share), 4)  AS avg_sis,
            ROUND(AVG(cpd.search_rank_lost_is),     4)  AS avg_rank_lost,
            ROUND(AVG(cpd.search_budget_lost_is),   4)  AS avg_budget_lost,
            ROUND(AVG(cpd.search_exact_match_is),   4)  AS avg_exact_match,
            SUM(CASE WHEN cpd.search_rank_lost_is IS NOT NULL
                     THEN 1 ELSE 0 END)                 AS rank_non_null,
            SUM(CASE WHEN cpd.search_rank_lost_is IS NULL
                     THEN 1 ELSE 0 END)                 AS rank_null
        FROM campaign_performance_daily cpd
        JOIN campaigns c ON c.campaign_id = cpd.campaign_id
        WHERE c.account_id = ?
          AND cpd.device   = 'ALL'
          AND cpd.date BETWEEN ? AND ?
        GROUP BY cpd.campaign_id
        ORDER BY spend DESC
    """, (account_id, start.isoformat(), end.isoformat())).fetchall()

    if not rows:
        print("  No rows found.")
        return

    # Header
    h = (f"  {'Campaign':<52} {'Status':<8} {'Days':>4} "
         f"{'Spend':>10} {'Clicks':>7} {'Convs':>6} "
         f"{'SIS%':>6} {'Rank%':>6} {'Bdgt%':>6} {'Exact%':>7} "
         f"{'NullDays':>8}")
    print(h)
    print("  " + "─" * (len(h) - 2))

    for r in rows:
        sis   = f"{r['avg_sis']*100:.1f}%"   if r['avg_sis']         is not None else "NULL"
        rank  = f"{r['avg_rank_lost']*100:.1f}%" if r['avg_rank_lost'] is not None else "NULL"
        bdgt  = f"{r['avg_budget_lost']*100:.1f}%" if r['avg_budget_lost'] is not None else "NULL"
        exact = f"{r['avg_exact_match']*100:.1f}%" if r['avg_exact_match'] is not None else "NULL"
        null_flag = f"⚠ {r['rank_null']}d NULL" if r['rank_null'] > 0 else ""

        print(f"  {str(r['campaign_name']):<52} "
              f"{str(r['status']):<8} "
              f"{r['days_in_window']:>4} "
              f"${r['spend']:>9,.2f} "
              f"{r['clicks']:>7,} "
              f"{r['convs']:>6,.1f} "
              f"{sis:>6} "
              f"{rank:>6} "
              f"{bdgt:>6} "
              f"{exact:>7}  "
              f"{null_flag}")

    print()
    print("  SIS   = Search Impression Share (% of eligible impressions won)")
    print("  Rank% = IS Lost to Rank  ← primary Efficiency Frontier input")
    print("  Bdgt% = IS Lost to Budget (budget constraint — model flags separately)")
    print("  Exact%= IS Lost to Exact Match")
    print()

    # Readiness verdict
    null_campaigns = [r for r in rows if r["rank_null"] > 0]
    zero_rank = [r for r in rows
                 if r["avg_rank_lost"] is None or r["avg_rank_lost"] == 0]

    if not null_campaigns and not zero_rank:
        print("  ✓  All campaigns have search_rank_lost_is populated.")
        print("     The Efficiency Frontier model is ready to run on this account.")
    else:
        if null_campaigns:
            print(f"  ⚠  {len(null_campaigns)} campaign(s) have NULL rank_lost_is on some days:")
            for r in null_campaigns:
                print(f"     {r['campaign_name']}  ({r['rank_null']} null days)")
        if zero_rank:
            print(f"  ℹ  {len(zero_rank)} campaign(s) show 0% rank loss "
                  "(already owning most eligible impressions — tightening may be appropriate):")
            for r in zero_rank:
                print(f"     {r['campaign_name']}")


def main():
    parser = argparse.ArgumentParser(
        description="Verify IS column availability in campaign_performance_daily."
    )
    parser.add_argument("--account", default="home",
                        choices=list(ACCOUNT_MAP.keys()),
                        help="Account vertical for per-campaign detail "
                             "(default: home).")
    parser.add_argument("--days", type=int, default=30,
                        help="Rolling window for recent coverage and "
                             "per-campaign detail (default: 30).")
    args = parser.parse_args()

    print("=" * 65)
    print("check_campaign_perf_is.py")
    print(f"  Database : {DB_PATH}")
    print(f"  Account  : {args.account}  ({ACCOUNT_MAP[args.account]})")
    print(f"  Window   : last {args.days} days")
    print("=" * 65)

    conn = get_conn()

    # 1. Schema
    schema_ok = check_schema(conn)
    if not schema_ok:
        conn.close()
        return

    # 2. Population across all Google accounts
    check_population(conn)

    # 3 & 4. Focused on the requested account
    target_pid = ACCOUNT_MAP[args.account]
    acct_row = conn.execute(
        "SELECT id, account_name FROM accounts "
        "WHERE platform_account_id = ? AND platform = 'google'",
        (target_pid,)
    ).fetchone()

    if not acct_row:
        print(f"\n  ⚠  Account '{args.account}' ({target_pid}) not found "
              "in accounts table.")
        conn.close()
        return

    account_id   = acct_row["id"]
    account_name = acct_row["account_name"] or args.account.title()

    check_recent(conn, account_id, account_name, args.days)
    check_per_campaign(conn, account_id, account_name, args.days)

    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
