"""
Diagnose the Audience impressions discrepancy. The visualization chart
shows ~13K Audience impressions for the Total Account, week of May 4-10,
but Microsoft Ads UI shows ~719K. That's a ~55x gap, far too large to
be a rollup-summing bug.

This script breaks down impressions and clicks in
campaign_performance_network_daily by every (campaign, device, network)
combination for May 4-10 to find where the missing impressions are
hiding. Then does the same on campaign_performance_daily so we can
compare what's there for Search.

Run:
    python diagnose_microsoft_impressions.py
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
ACCOUNT_ID = 2
START = "2026-05-04"
END = "2026-05-10"


def fetch_one(conn, sql, params=()):
    cur = conn.cursor()
    return cur.execute(sql, params).fetchall()


def main():
    conn = sqlite3.connect(DB_PATH)
    print("=" * 90)
    print(f"Impressions diagnostic | account_id={ACCOUNT_ID} | {START} to {END}")
    print("=" * 90)
    print()

    # ── 1. campaign_performance_daily totals by device (Search) ──
    print("── Search table (campaign_performance_daily) totals by device ──")
    print(f"  {'device':<10} {'rows':>6} {'impressions':>14} {'clicks':>10} "
          f"{'spend':>12}")
    rows = fetch_one(conn, """
        SELECT cpd.device,
               COUNT(*) AS n,
               SUM(cpd.impressions) AS imp,
               SUM(cpd.clicks) AS clicks,
               SUM(cpd.cost_micros)/1e6 AS spend
        FROM campaign_performance_daily cpd
        JOIN campaigns c ON cpd.campaign_id = c.campaign_id
        WHERE c.account_id = ?
          AND cpd.date BETWEEN ? AND ?
        GROUP BY cpd.device
        ORDER BY cpd.device
    """, (ACCOUNT_ID, START, END))
    for dev, n, imp, clicks, spend in rows:
        print(f"  {dev:<10} {n:>6} {imp or 0:>14,} {clicks or 0:>10,} "
              f"${spend or 0:>11,.2f}")
    print()

    # ── 2. campaign_performance_network_daily by network + device ──
    print("── Audience table (campaign_performance_network_daily) by network × device ──")
    print(f"  {'network':<28} {'device':<10} {'rows':>6} {'impressions':>14} "
          f"{'clicks':>10} {'spend':>12}")
    rows = fetch_one(conn, """
        SELECT cpnd.network, cpnd.device,
               COUNT(*) AS n,
               SUM(cpnd.impressions) AS imp,
               SUM(cpnd.clicks) AS clicks,
               SUM(cpnd.cost_micros)/1e6 AS spend
        FROM campaign_performance_network_daily cpnd
        JOIN campaigns c ON cpnd.campaign_id = c.campaign_id
        WHERE c.account_id = ?
          AND cpnd.date BETWEEN ? AND ?
        GROUP BY cpnd.network, cpnd.device
        ORDER BY cpnd.network, cpnd.device
    """, (ACCOUNT_ID, START, END))
    for net, dev, n, imp, clicks, spend in rows:
        print(f"  {(net or 'NULL'):<28} {dev:<10} {n:>6} "
              f"{imp or 0:>14,} {clicks or 0:>10,} ${spend or 0:>11,.2f}")
    print()

    # ── 3. UI expectations vs each candidate aggregation ──
    print("── Reconciliation: what the UI says vs what the DB shows ──")
    print(f"  UI for week of {START} to {END}:")
    print(f"    Search    impressions ≈     51,445   CTR ≈ 3.11%")
    print(f"    Audience  impressions ≈    718,693   CTR ≈ 0.26%")
    print()
    print(f"  Candidate aggregations from campaign_performance_network_daily")
    print(f"  (account_id={ACCOUNT_ID}, network='Audience'):")
    for label, dev_clause in [
        ("  device = 'ALL'         ", "AND cpnd.device = 'ALL'"),
        ("  device IN per-device   ", "AND cpnd.device != 'ALL'"),
        ("  no device filter (all) ", ""),
    ]:
        row = fetch_one(conn, f"""
            SELECT SUM(cpnd.impressions),
                   SUM(cpnd.clicks),
                   SUM(cpnd.cost_micros)/1e6
            FROM campaign_performance_network_daily cpnd
            JOIN campaigns c ON cpnd.campaign_id = c.campaign_id
            WHERE c.account_id = ?
              AND cpnd.date BETWEEN ? AND ?
              AND cpnd.network = 'Audience'
              {dev_clause}
        """, (ACCOUNT_ID, START, END))[0]
        imp, clicks, spend = row
        ctr = (clicks / imp * 100) if imp else 0
        print(f"  {label}  imp={imp or 0:>10,}  clicks={clicks or 0:>8,}  "
              f"spend=${spend or 0:>9,.2f}  CTR={ctr:>5.2f}%")
    print()
    print(f"  Candidate aggregations from campaign_performance_daily (Search):")
    for label, dev_clause in [
        ("  device = 'ALL'         ", "AND cpd.device = 'ALL'"),
        ("  device IN per-device   ", "AND cpd.device != 'ALL'"),
        ("  no device filter (all) ", ""),
    ]:
        row = fetch_one(conn, f"""
            SELECT SUM(cpd.impressions),
                   SUM(cpd.clicks),
                   SUM(cpd.cost_micros)/1e6
            FROM campaign_performance_daily cpd
            JOIN campaigns c ON cpd.campaign_id = c.campaign_id
            WHERE c.account_id = ?
              AND cpd.date BETWEEN ? AND ?
              {dev_clause}
        """, (ACCOUNT_ID, START, END))[0]
        imp, clicks, spend = row
        ctr = (clicks / imp * 100) if imp else 0
        print(f"  {label}  imp={imp or 0:>10,}  clicks={clicks or 0:>8,}  "
              f"spend=${spend or 0:>9,.2f}  CTR={ctr:>5.2f}%")
    print()

    print("Interpretation:")
    print("  - For SEARCH impressions: which row matches 51,445?")
    print("    Whichever it is, that's the right device filter to use.")
    print("  - For AUDIENCE impressions: which row matches 718,693?")
    print("    If none come close, the impressions data may be missing")
    print("    from the loader entirely or stored at a different grain.")
    print("  - Critical: ROAS / spend / conversions reconcile against")
    print("    device='ALL' (which the previous loader fix established).")
    print("    If impressions need a DIFFERENT device filter than spend,")
    print("    the SQL needs to be split: device='ALL' for $/conv, but")
    print("    something else for impressions/clicks.")
    conn.close()


if __name__ == "__main__":
    main()
