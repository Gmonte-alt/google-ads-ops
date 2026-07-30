#!/usr/bin/env python
"""
query_share_by_half.py -- analysis\ -- Ben's ask: where did we lose/gain share?
Google-only. Periods: 1H2024, 2H2024, 1H2025, 2H2025, YTD2026 (2024 halves appear
automatically once the 05 backfill + tagging have run; absent otherwise).

Share basis: each category's % of that period's impressions / clicks / primary conversions.
READ-ONLY. Writes two CSVs next to the script.

Classification = the canonical search_queries tags (brand_category, vertical), so this
reconciles with the tagging pipeline. brand_matcher runs only to name brands for the
per-brand movers table. Excludes medicare/life-vertical queries per the carrier ruling.

    python query_share_by_half.py --db "C:\\Projects\\google-ads-ops\\db\\paid_search.db"
    [--brand-matcher-dir "C:\\Projects\\google-ads-ops\\campaign_db_scripts\\change_history"]
"""
import argparse
import os
import sqlite3
import sys
from collections import defaultdict

import pandas as pd


def period_expr(col="p.report_date"):
    return f"""CASE
        WHEN {col} BETWEEN '2024-01-01' AND '2024-06-30' THEN '1H2024'
        WHEN {col} BETWEEN '2024-07-01' AND '2024-12-31' THEN '2H2024'
        WHEN {col} BETWEEN '2025-01-01' AND '2025-06-30' THEN '1H2025'
        WHEN {col} BETWEEN '2025-07-01' AND '2025-12-31' THEN '2H2025'
        WHEN {col} >= '2026-01-01' THEN 'YTD2026'
        ELSE NULL END"""


PERIOD_ORDER = ["1H2024", "2H2024", "1H2025", "2H2025", "YTD2026"]

CATEGORY_SQL = """CASE
    WHEN sq.vertical IN ('medicare','life')            THEN '_excluded_offvertical'
    WHEN sq.brand_category LIKE 'carrier_%'            THEN 'carrier'
    WHEN sq.brand_category LIKE 'aggregator%'          THEN 'aggregator_competitor'
    WHEN sq.brand_category = 'confie_subsidiary'       THEN 'confie'
    WHEN sq.brand_category = 'state_residual_pool'     THEN 'state_pool'
    WHEN sq.brand_category IS NOT NULL                 THEN 'other_brand'
    ELSE 'generic' END"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--brand-matcher-dir",
                    default=r"C:\Projects\google-ads-ops\campaign_db_scripts\change_history")
    ap.add_argument("--top-movers", type=int, default=15)
    args = ap.parse_args()

    conn = sqlite3.connect("file:%s?mode=ro" % args.db, uri=True)

    # ---------- 1. category share per period ----------
    df = pd.read_sql_query(f"""
        SELECT {period_expr()} AS period,
               {CATEGORY_SQL}  AS category,
               SUM(p.impressions)        AS impressions,
               SUM(p.clicks)             AS clicks,
               SUM(p.primary_conv_count) AS conversions,
               SUM(p.primary_conv_value) AS conv_value,
               SUM(p.cost_micros)/1e6    AS cost
        FROM search_query_performance p
        JOIN search_queries sq ON sq.id = p.query_id
        WHERE p.platform = 'google' AND p.device = 'ALL'
        GROUP BY period, category
        HAVING period IS NOT NULL
    """, conn)

    periods = [p for p in PERIOD_ORDER if p in set(df["period"])]
    print("periods with data:", ", ".join(periods))
    missing = [p for p in PERIOD_ORDER if p not in periods]
    if missing:
        print("periods ABSENT (need backfill + tagging):", ", ".join(missing))

    excl = df[df["category"] == "_excluded_offvertical"]
    df = df[df["category"] != "_excluded_offvertical"]
    if not excl.empty:
        print("\n(medicare/life-vertical queries excluded: %s impressions total)"
              % f"{int(excl['impressions'].sum()):,}")

    for metric in ("impressions", "clicks", "conversions"):
        pivot = df.pivot_table(index="category", columns="period",
                               values=metric, aggfunc="sum").fillna(0)
        pivot = pivot[[p for p in periods]]
        share = pivot / pivot.sum() * 100
        print("\n" + "=" * 74)
        print("SHARE OF %s BY CATEGORY (%% of period total)" % metric.upper())
        print("=" * 74)
        with pd.option_context("display.float_format", "{:6.1f}".format):
            print(share.round(1).to_string())
        if len(periods) >= 2:
            delta = (share[periods[-1]] - share[periods[0]]).sort_values()
            print("\n  delta %s -> %s (share points):" % (periods[0], periods[-1]))
            for cat, d in delta.items():
                print("    %-22s %+5.1f" % (cat, d))

    share_csv = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "query_share_by_half_categories.csv")
    df.to_csv(share_csv, index=False)

    # ---------- 2. per-brand movers (carrier queries only) ----------
    sys.path.insert(0, args.brand_matcher_dir)
    try:
        from brand_matcher import BrandMatcher
        bm = BrandMatcher(conn)
    except Exception as e:
        print("\n[WARN] brand_matcher unavailable (%s) -- skipping per-brand movers." % e)
        conn.close()
        return

    bq = pd.read_sql_query(f"""
        SELECT sq.normalized_text,
               {period_expr()} AS period,
               SUM(p.impressions)        AS impressions,
               SUM(p.primary_conv_count) AS conversions
        FROM search_query_performance p
        JOIN search_queries sq ON sq.id = p.query_id
        WHERE p.platform = 'google' AND p.device = 'ALL'
          AND sq.brand_category LIKE 'carrier_%'
          AND (sq.vertical IS NULL OR sq.vertical NOT IN ('medicare','life'))
        GROUP BY sq.normalized_text, period
        HAVING period IS NOT NULL
    """, conn)

    cache = {}
    def brand_of(text):
        if text not in cache:
            m = bm.match(text)
            cache[text] = m.brand_name if (m and m.is_home_carrier) else "_nonhome_or_unmatched"
        return cache[text]

    print("\nclassifying %d distinct carrier queries for brand movers..." % bq["normalized_text"].nunique())
    bq["brand"] = bq["normalized_text"].map(brand_of)
    bq = bq[bq["brand"] != "_nonhome_or_unmatched"]

    bp = bq.pivot_table(index="brand", columns="period", values="impressions",
                        aggfunc="sum").fillna(0)
    bp = bp[[p for p in periods if p in bp.columns]]
    bshare = bp / bp.sum() * 100  # share of carrier impressions
    first, last = bshare.columns[0], bshare.columns[-1]
    bshare["delta_pts"] = bshare[last] - bshare[first]
    movers = bshare.sort_values("delta_pts")

    print("\n" + "=" * 74)
    print("CARRIER BRAND SHARE MOVERS (%% of carrier-query impressions, %s -> %s)" % (first, last))
    print("=" * 74)
    print("\nTOP LOSERS:")
    with pd.option_context("display.float_format", "{:6.1f}".format):
        print(movers.head(args.top_movers).round(1).to_string())
        print("\nTOP GAINERS:")
        print(movers.tail(args.top_movers).round(1).to_string())

    movers_csv = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "query_share_by_half_brand_movers.csv")
    movers.to_csv(movers_csv)
    conn.close()
    print("\nCSVs: %s | %s" % (share_csv, movers_csv))
    print("NOTE for the writeup: total captured volume contracted ~75%% across the window "
          "(restructuring/consolidation) -- shares are comparable across periods, absolute "
          "volumes are NOT. YTD2026 is a partial period: compare rates, never totals.")


if __name__ == "__main__":
    main()
