#!/usr/bin/env python
"""
intent_theme_evidence.py -- ad_assets/analysis/ -- v2 (schema-corrected).
GEN-01 measurement input: performance per query INTENT THEME from the
standing taxonomy. Fixes vs v1: join is sqp.query_id -> search_queries.id
(asymmetric names); conversions = primary_conv_count / primary_conv_value;
sqp is CROSS-PLATFORM -- filtered to Google by default (--platform all|
google|microsoft).

Conventions honored: sqp is per-device with NO ALL rollup (aggregated
across devices); primary_* = the lead metrics (secondary/all_conv exist
but are not the judgment metrics). READ-ONLY.

  python intent_theme_evidence.py --db "C:\\Projects\\google-ads-ops\\db\\paid_search.db"
  python intent_theme_evidence.py --db "..." --from 2026-06-01 --platform all
"""
import argparse
import datetime
import sqlite3
import sys

def main():
    p = argparse.ArgumentParser(description="Intent-theme performance (read-only).")
    p.add_argument("--db", required=True)
    p.add_argument("--from", dest="date_from", default="2026-01-01", metavar="YYYY-MM-DD")
    p.add_argument("--to", dest="date_to",
                   default=(datetime.date.today() - datetime.timedelta(days=1)).isoformat())
    p.add_argument("--platform", default="google",
                   help="google (default) | microsoft | all")
    p.add_argument("--account", type=int, default=1,
                   help="account_id scope via campaigns join (default 1 = Google Home; "
                        "sqp is MULTI-ACCOUNT with no account_id column -- unfiltered "
                        "runs blend Term Life etc.)")
    args = p.parse_args()
    conn = sqlite3.connect("file:%s?mode=ro" % args.db.replace("\\", "/"), uri=True)

    plats = [r[0] for r in conn.execute(
        "SELECT DISTINCT platform FROM search_query_performance")]
    plat_clause, plat_args = "", []
    if args.platform != "all":
        match = [x for x in plats if x and args.platform.lower() in str(x).lower()]
        if not match:
            sys.exit("[ABORT] no platform value matching %r -- values present: %s"
                     % (args.platform, plats))
        plat_clause = " AND p.platform IN (%s)" % ",".join("?" * len(match))
        plat_args = match
    # theme column: two generations exist (legacy `theme`, restructure-era
    # `target_theme`) -- use whichever is actually populated, prefer target_theme
    counts = conn.execute(
        """SELECT COUNT(NULLIF(TRIM(COALESCE(CAST(target_theme AS TEXT),'')),'')),
                  COUNT(NULLIF(TRIM(COALESCE(CAST(theme AS TEXT),'')),'')),
                  COUNT(*) FROM search_queries""").fetchone()
    theme_col = "target_theme" if counts[0] >= counts[1] else "theme"
    filled = counts[0] if theme_col == "target_theme" else counts[1]
    print("[SOURCES] join p.query_id -> q.id | conv = primary_conv_* | "
          "platforms in table: %s | filtered to: %s | AGGREGATED ACROSS DEVICES"
          % (plats, plat_args or "all"))
    print("[THEME COL] using %s (%s of %s queries tagged; %s has %s)"
          % (theme_col, f"{filled:,}", f"{counts[2]:,}",
             "theme" if theme_col == "target_theme" else "target_theme",
             f"{counts[1] if theme_col == 'target_theme' else counts[0]:,}"))
    print("[WINDOW] %s .. %s\n" % (args.date_from, args.date_to))

    base = f"""
        FROM search_query_performance p
        JOIN search_queries q ON q.id = p.query_id
        JOIN campaigns c ON c.campaign_id = p.campaign_id
        WHERE c.account_id = ? AND p.report_date BETWEEN ? AND ?{plat_clause}
    """
    qargs = [args.account, args.date_from, args.date_to] + plat_args
    print("[ACCOUNT] scoped to account_id=%d via campaigns join" % args.account)

    def table(select_dims, group_label):
        rows = conn.execute(f"""
            SELECT {select_dims},
                   COUNT(DISTINCT p.query_id), SUM(p.impressions), SUM(p.clicks),
                   SUM(p.cost_micros)/1e6,
                   SUM(p.primary_conv_count), SUM(p.primary_conv_value)
            {base} GROUP BY 1, 2 ORDER BY 6 DESC
        """, qargs).fetchall()
        print("%-18s %-14s %8s %10s %8s %10s %6s %6s %7s %7s"
              % (group_label, "dim2", "queries", "impr", "clicks", "cost",
                 "CTR%", "CR%", "CPL", "VPC"))
        print("-" * 106)
        for d1, d2, nq, impr, clicks, cost, conv, val in rows:
            impr, clicks, cost = impr or 0, clicks or 0, cost or 0
            conv, val = conv or 0, val or 0
            print("%-18s %-14s %8s %10s %8s %10s %5.2f%% %5.2f%% %7s %7s"
                  % (str(d1)[:18], str(d2)[:14], f"{nq:,}", f"{impr:,}",
                     f"{clicks:,}", f"${cost:,.0f}",
                     100.0 * clicks / impr if impr else 0,
                     100.0 * conv / clicks if clicks else 0,
                     ("$%.0f" % (cost / conv)) if conv else "--",
                     ("$%.2f" % (val / clicks)) if clicks else "--"))
        print()

    table(f"COALESCE(q.{theme_col},'(untagged)'), COALESCE(q.intent_type,'')", "theme")
    # brand vs nonbrand split per theme -- GEN-01 cares about the nonbrand rows
    table(f"""COALESCE(q.{theme_col},'(untagged)'), CASE WHEN COALESCE(NULLIF(NULLIF(NULLIF(LOWER(
             CAST(q.modifier_brand AS TEXT)),'0'),'none'),''),'') = ''
             THEN 'nonbrand' ELSE 'brand' END""", "theme (brand split)")
    # geo cut: the State group's baseline (geo is a modifier, not a theme)
    table("""CASE WHEN COALESCE(NULLIF(NULLIF(NULLIF(LOWER(
             CAST(q.modifier_geo AS TEXT)),'0'),'none'),''),'') = ''
             THEN 'no-geo' ELSE 'GEO-MODIFIED' END,
             CASE WHEN COALESCE(NULLIF(NULLIF(NULLIF(LOWER(
             CAST(q.modifier_brand AS TEXT)),'0'),'none'),''),'') = ''
             THEN 'nonbrand' ELSE 'brand' END""", "geo split")
    conn.close()
    print("The GEO-MODIFIED x nonbrand row = the State group's baseline.")
    print("Reading for GEN-01: the NONBRAND rows are the baselines -- per-theme CR /")
    print("CPL / VPC are what the themed groups get judged against at the S6 reads.")
    print("Pin these numbers (window + values) before the themed-creative swap posts.")


if __name__ == "__main__":
    main()
