"""
diag_home_performance.py
========================
Targeted diagnostic for the three known problem areas in the
Home Insurance Google Ads account (8886502147):

  1. ad_group_performance_daily — conversion routing (revenue = $0)
  2. keyword_instances — TIER TWO attribution after 02_sync_keyword_structure.py
  3. keyword_performance_daily — TIER TWO coverage after kpd refresh

Run:
    python campaign_db_scripts/diag_home_performance.py
"""

import sqlite3
from pathlib import Path
from datetime import date, timedelta

DB_PATH          = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
HOME_ACCOUNT_ID  = "8886502147"
TIER_TWO_ID      = "23496334752"
WINDOW_30_START  = (date.today() - timedelta(days=29)).isoformat()
TODAY            = date.today().isoformat()

SEP  = "─" * 65
SEP2 = "═" * 65

def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def main():
    db = conn()
    cur = db.cursor()

    print(SEP2)
    print("  Home Insurance — Performance Diagnostic")
    print(f"  Account  : {HOME_ACCOUNT_ID}")
    print(f"  Run date : {TODAY}")
    print(f"  30d from : {WINDOW_30_START}")
    print(SEP2)

    # ════════════════════════════════════════════════════════════════
    # AREA 1: ad_group_performance_daily — conversion routing
    # ════════════════════════════════════════════════════════════════
    print(f"\n{'─'*65}")
    print("  AREA 1: ad_group_performance_daily — conversion routing")
    print(f"{'─'*65}")

    # 1a. Table row counts by device
    print("\n[1a] Row counts by device:")
    rows = cur.execute("""
        SELECT device, COUNT(*) AS rows,
               MIN(date) AS min_d, MAX(date) AS max_d
        FROM ad_group_performance_daily
        WHERE account_id = ?
        GROUP BY device ORDER BY rows DESC
    """, (HOME_ACCOUNT_ID,)).fetchall()
    for r in rows:
        print(f"  device='{r['device']}'  rows={r['rows']:>8,}  "
              f"{r['min_d']} → {r['max_d']}")

    # 1b. Revenue vs spend -- is revenue always $0?
    print("\n[1b] Revenue vs spend (ALL device, last 30d):")
    r = cur.execute("""
        SELECT
            COUNT(*)                            AS rows,
            SUM(cost_micros)/1e6                AS total_spend,
            SUM(primary_conv_value)             AS total_revenue,
            SUM(CASE WHEN primary_conv_value > 0
                THEN 1 ELSE 0 END)              AS rows_with_revenue,
            SUM(CASE WHEN conversions > 0
                THEN 1 ELSE 0 END)              AS rows_with_google_convs,
            SUM(conversions)                    AS google_conv_total,
            SUM(conversion_value)               AS google_conv_value_total
        FROM ad_group_performance_daily
        WHERE account_id = ? AND device = 'ALL'
          AND date >= ?
    """, (HOME_ACCOUNT_ID, WINDOW_30_START)).fetchone()
    print(f"  Rows (ALL, 30d)          : {r['rows']:,}")
    print(f"  Total spend              : ${r['total_spend']:,.2f}")
    print(f"  Primary conv revenue     : ${r['total_revenue']:,.2f}  "
          f"← {'$0 everywhere = routing bug' if r['total_revenue'] == 0 else 'OK'}")
    print(f"  Rows with primary rev    : {r['rows_with_revenue']:,} / {r['rows']:,}")
    print(f"  Google conversions total : {r['google_conv_total']:,.1f}  "
          f"(raw all_conversions from API)")
    print(f"  Google conv value total  : ${r['google_conv_value_total']:,.2f}  "
          f"(raw all_conversions_value)")
    print(f"  Rows with google convs   : {r['rows_with_google_convs']:,}")

    # 1c. conversion_actions table for home account
    print("\n[1c] conversion_actions table (home account):")
    acct_int = cur.execute(
        "SELECT id FROM accounts WHERE platform_account_id = ? AND platform='google'",
        (HOME_ACCOUNT_ID,)
    ).fetchone()
    if acct_int:
        ca_rows = cur.execute("""
            SELECT conversion_name, include_mode
            FROM conversion_actions WHERE account_id = ?
            ORDER BY include_mode, conversion_name
        """, (acct_int['id'],)).fetchall()
        if ca_rows:
            for r in ca_rows:
                print(f"  include_mode='{r['include_mode']}'  "
                      f"name='{r['conversion_name']}'")
        else:
            print("  ⚠ NO ROWS — conversion_actions is empty for this account")
            print("    This is the root cause of revenue = $0 in agpd")
    else:
        print("  ⚠ account not found in accounts table")

    # 1d. TIER TWO ad groups in agpd
    print(f"\n[1d] agpd TIER TWO — last 30d (ALL device):")
    t2_rows = cur.execute("""
        SELECT ad_group_name,
               SUM(cost_micros)/1e6     AS spend,
               SUM(primary_conv_value)  AS revenue,
               SUM(conversions)         AS google_convs,
               SUM(conversion_value)    AS google_value
        FROM ad_group_performance_daily
        WHERE account_id = ? AND device = 'ALL'
          AND campaign_id = ? AND date >= ?
        GROUP BY ad_group_id, ad_group_name
        ORDER BY spend DESC
    """, (HOME_ACCOUNT_ID, TIER_TWO_ID, WINDOW_30_START)).fetchall()
    if t2_rows:
        print(f"  {'Ad Group':<40} {'Spend':>8} {'PrimRev':>9} "
              f"{'GConvs':>8} {'GValue':>10}")
        print(f"  {'─'*40} {'─'*8} {'─'*9} {'─'*8} {'─'*10}")
        for r in t2_rows:
            print(f"  {str(r['ad_group_name'])[:39]:<40} "
                  f"${r['spend']:>7,.0f} "
                  f"${r['revenue']:>8,.0f} "
                  f"{r['google_convs']:>8,.1f} "
                  f"${r['google_value']:>9,.0f}")
    else:
        print("  No rows found for TIER TWO in agpd (30d)")

    # ════════════════════════════════════════════════════════════════
    # AREA 2: keyword_instances — TIER TWO attribution
    # ════════════════════════════════════════════════════════════════
    print(f"\n{'─'*65}")
    print("  AREA 2: keyword_instances — TIER TWO attribution")
    print(f"{'─'*65}")

    # 2a. How many ki rows are attributed to TIER TWO?
    t2_ki = cur.execute("""
        SELECT COUNT(*) AS cnt,
               COUNT(DISTINCT ad_group_id) AS ad_groups
        FROM keyword_instances ki
        JOIN accounts a ON ki.account_id = a.id
        WHERE a.platform_account_id = ?
          AND ki.campaign_id = ?
    """, (HOME_ACCOUNT_ID, TIER_TWO_ID)).fetchone()
    print(f"\n[2a] keyword_instances rows for TIER TWO:")
    print(f"  Total keyword slots  : {t2_ki['cnt']:,}")
    print(f"  Distinct ad groups   : {t2_ki['ad_groups']:,}")

    # 2b. Ad groups in ki for TIER TWO
    print(f"\n[2b] Ad groups in keyword_instances for TIER TWO:")
    ag_rows = cur.execute("""
        SELECT ad_group_name,
               COUNT(*) AS kw_count,
               SUM(CASE WHEN status='ENABLED' THEN 1 ELSE 0 END) AS enabled,
               SUM(CASE WHEN status='PAUSED'  THEN 1 ELSE 0 END) AS paused
        FROM keyword_instances ki
        JOIN accounts a ON ki.account_id = a.id
        WHERE a.platform_account_id = ?
          AND ki.campaign_id = ?
        GROUP BY ad_group_id, ad_group_name
        ORDER BY kw_count DESC
    """, (HOME_ACCOUNT_ID, TIER_TWO_ID)).fetchall()
    for r in ag_rows:
        print(f"  '{str(r['ad_group_name'])[:45]}'  "
              f"kws={r['kw_count']}  "
              f"enabled={r['enabled']}  paused={r['paused']}")

    # 2c. Distribution of campaign_ids across all home ki rows
    print(f"\n[2c] keyword_instances — top 10 campaigns by keyword count (home):")
    camp_rows = cur.execute("""
        SELECT ki.campaign_name,
               ki.campaign_id,
               COUNT(*) AS kw_count
        FROM keyword_instances ki
        JOIN accounts a ON ki.account_id = a.id
        WHERE a.platform_account_id = ?
        GROUP BY ki.campaign_id
        ORDER BY kw_count DESC
        LIMIT 10
    """, (HOME_ACCOUNT_ID,)).fetchall()
    for r in camp_rows:
        marker = " ← TIER TWO" if r['campaign_id'] == TIER_TWO_ID else ""
        print(f"  {r['kw_count']:>5} kws  {r['campaign_id']}  "
              f"'{str(r['campaign_name'])[:45]}'{marker}")

    # 2d. Last sync date on ki rows for TIER TWO
    sync_row = cur.execute("""
        SELECT MAX(last_seen_date) AS last_seen,
               MIN(first_seen_date) AS first_seen
        FROM keyword_instances ki
        JOIN accounts a ON ki.account_id = a.id
        WHERE a.platform_account_id = ? AND ki.campaign_id = ?
    """, (HOME_ACCOUNT_ID, TIER_TWO_ID)).fetchone()
    print(f"\n[2d] keyword_instances TIER TWO sync dates:")
    print(f"  first_seen_date : {sync_row['first_seen']}")
    print(f"  last_seen_date  : {sync_row['last_seen']}  "
          f"{'← recent sync' if sync_row['last_seen'] and sync_row['last_seen'] >= '2026-04-08' else '← may be stale'}")

    # ════════════════════════════════════════════════════════════════
    # AREA 3: keyword_performance_daily — TIER TWO coverage
    # ════════════════════════════════════════════════════════════════
    print(f"\n{'─'*65}")
    print("  AREA 3: keyword_performance_daily — TIER TWO coverage")
    print(f"{'─'*65}")

    # 3a. How many kpd rows exist for TIER TWO keywords?
    t2_kpd = cur.execute("""
        SELECT COUNT(*) AS rows,
               COUNT(DISTINCT ki.id) AS keyword_slots,
               COUNT(DISTINCT ki.ad_group_id) AS ad_groups,
               MIN(kpd.report_date) AS min_date,
               MAX(kpd.report_date) AS max_date,
               SUM(kpd.cost_micros)/1e6 AS spend,
               SUM(kpd.primary_conv_value) AS revenue
        FROM keyword_performance_daily kpd
        JOIN keyword_instances ki ON kpd.instance_id = ki.id
        JOIN accounts a ON ki.account_id = a.id
        WHERE a.platform_account_id = ?
          AND ki.campaign_id = ?
          AND kpd.device = 'ALL'
    """, (HOME_ACCOUNT_ID, TIER_TWO_ID)).fetchone()
    print(f"\n[3a] keyword_performance_daily for TIER TWO keywords (device=ALL):")
    print(f"  Total kpd rows     : {t2_kpd['rows']:,}")
    print(f"  Distinct kw slots  : {t2_kpd['keyword_slots']:,}")
    print(f"  Distinct ad groups : {t2_kpd['ad_groups']:,}")
    print(f"  Date range         : {t2_kpd['min_date']} → {t2_kpd['max_date']}")
    print(f"  Total spend        : ${t2_kpd['spend']:,.2f}")
    print(f"  Primary revenue    : ${t2_kpd['revenue']:,.2f}")

    # 3b. kpd for TIER TWO last 30d by ad group
    print(f"\n[3b] kpd TIER TWO — last 30d by ad group:")
    ag_kpd = cur.execute("""
        SELECT ki.ad_group_name,
               COUNT(DISTINCT ki.id) AS kw_slots,
               SUM(kpd.cost_micros)/1e6 AS spend,
               SUM(kpd.primary_conv_value) AS revenue,
               SUM(kpd.impressions) AS impressions
        FROM keyword_performance_daily kpd
        JOIN keyword_instances ki ON kpd.instance_id = ki.id
        JOIN accounts a ON ki.account_id = a.id
        WHERE a.platform_account_id = ?
          AND ki.campaign_id = ?
          AND kpd.device = 'ALL'
          AND kpd.report_date >= ?
        GROUP BY ki.ad_group_id, ki.ad_group_name
        ORDER BY spend DESC
    """, (HOME_ACCOUNT_ID, TIER_TWO_ID, WINDOW_30_START)).fetchall()
    if ag_kpd:
        print(f"  {'Ad Group':<40} {'KW Slots':>9} {'Spend':>8} "
              f"{'Revenue':>8} {'Impr':>7}")
        print(f"  {'─'*40} {'─'*9} {'─'*8} {'─'*8} {'─'*7}")
        for r in ag_kpd:
            print(f"  {str(r['ad_group_name'])[:39]:<40} "
                  f"{r['kw_slots']:>9,} "
                  f"${r['spend']:>7,.0f} "
                  f"${r['revenue']:>7,.0f} "
                  f"{r['impressions']:>7,}")
    else:
        print("  No kpd rows for TIER TWO in last 30d")

    # 3c. Compare kpd attribution vs agpd for TIER TWO (30d spend)
    kpd_spend = sum(r['spend'] for r in ag_kpd) if ag_kpd else 0
    agpd_spend = sum(r['spend'] for r in t2_rows) if t2_rows else 0
    print(f"\n[3c] Spend reconciliation — TIER TWO (30d):")
    print(f"  ad_group_performance_daily spend : ${agpd_spend:>10,.2f}  (API source, correct)")
    print(f"  keyword_performance_daily spend  : ${kpd_spend:>10,.2f}  (via keyword_instances)")
    gap = agpd_spend - kpd_spend
    print(f"  Gap                              : ${gap:>10,.2f}  "
          f"{'← kpd is missing most TIER TWO spend' if gap > 100 else '← reconciled'}")

    # ════════════════════════════════════════════════════════════════
    # SUMMARY
    # ════════════════════════════════════════════════════════════════
    print(f"\n{SEP2}")
    print("  DIAGNOSTIC SUMMARY")
    print(SEP2)
    no_ca  = len(ca_rows) == 0 if acct_int else True
    no_rev = r['total_revenue'] == 0
    kw_gap = t2_ki['cnt'] < 10
    kpd_gap = gap > 100

    print(f"\n  Area 1 — agpd revenue = $0")
    if no_ca:
        print("  ⚠ CONFIRMED: conversion_actions table is EMPTY for home account")
        print("    No routing rules → primary_conv_value stays $0 on every agpd row")
        print("    Fix: seed conversion_actions with the home account's offline conversion")
        print("         action name and include_mode='primary'")
    elif no_rev:
        print("  ⚠ CONFIRMED: conversion_actions exist but routing produces $0")
        print("    The action name matching in Pass 2 is not matching the API resource strings")
        print("    Fix: check the conversion action name format in conversion_actions")
        print("         vs what the API returns in segments.conversion_action")
    else:
        print("  ✓ revenue is populated correctly")

    print(f"\n  Area 2 — keyword_instances TIER TWO attribution")
    if kw_gap:
        print(f"  ⚠ Only {t2_ki['cnt']} keyword slots attributed to TIER TWO")
        print("    Most keywords still carry old campaign_id values")
        print("    02_sync_keyword_structure.py may not have run yet or")
        print("    TIER TWO's keywords have criterion_ids in ki but still")
        print("    pointing to other campaign_ids")
    else:
        print(f"  ✓ {t2_ki['cnt']} keyword slots attributed to TIER TWO")

    print(f"\n  Area 3 — keyword_performance_daily TIER TWO coverage")
    if kpd_gap:
        print(f"  ⚠ kpd spend (${kpd_spend:,.0f}) << agpd spend (${agpd_spend:,.0f})")
        print(f"    ${gap:,.0f} of TIER TWO spend has no keyword-level attribution in kpd")
        print("    Root cause: keyword_instances.campaign_id is stale for most keywords")
        print("    Fix: run 03_sync_keyword_performance.py --refresh 90 after ki is fixed")
    else:
        print("  ✓ kpd spend reconciles with agpd spend")

    db.close()
    print(f"\n{'═'*65}")
    print("  Done.")
    print(f"{'═'*65}")


if __name__ == "__main__":
    main()
