#!/usr/bin/env python
"""
carrier_product_modifier_breakdown.py -- analysis\ -- READ-ONLY.

Part 1: carrier brand x product category x modifier profile x period.
  - brand: brand_matcher (canonical, home-carrier gated)
  - product: product_taxonomy_terms containment match (longest wins) on query text
  - modifiers: the search_queries modifier_* flag columns
Part 2: DKI impact check -- daily impressions/clicks/conv for trademark-restricted
  brand queries (Costco, AAA) around the 2026-07-14 DKI launch.

    python carrier_product_modifier_breakdown.py --db "C:\\Projects\\google-ads-ops\\db\\paid_search.db"
    [--brands "State Farm,Costco,AAA,Kin Insurance,Farmers,Hartford"]
"""
import argparse
import os
import sqlite3
import sys

import pandas as pd

PERIOD_SQL = """CASE
    WHEN p.report_date BETWEEN '2024-01-01' AND '2024-06-30' THEN '1H2024'
    WHEN p.report_date BETWEEN '2024-07-01' AND '2024-12-31' THEN '2H2024'
    WHEN p.report_date BETWEEN '2025-01-01' AND '2025-06-30' THEN '1H2025'
    WHEN p.report_date BETWEEN '2025-07-01' AND '2025-12-31' THEN '2H2025'
    WHEN p.report_date >= '2026-01-01' THEN 'YTD2026' END"""
PERIODS = ["1H2024", "2H2024", "1H2025", "2H2025", "YTD2026"]
MODIFIERS = ["modifier_geo", "modifier_price", "modifier_coverage",
             "modifier_home_type", "modifier_bundle", "modifier_near_me",
             "modifier_home_purchase"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--brand-matcher-dir",
                    default=r"C:\Projects\google-ads-ops\campaign_db_scripts\change_history")
    ap.add_argument("--brands", default="State Farm,Costco,AAA,Kin Insurance,Farmers,Hartford,Geico")
    args = ap.parse_args()
    want_brands = [b.strip() for b in args.brands.split(",")]

    conn = sqlite3.connect("file:%s?mode=ro" % args.db, uri=True)
    sys.path.insert(0, args.brand_matcher_dir)
    from brand_matcher import BrandMatcher
    bm = BrandMatcher(conn)

    # product term map: normalized term -> product_code (longest term wins)
    terms = conn.execute("""
        SELECT t.normalized, pt.product_code
        FROM product_taxonomy_terms t
        JOIN product_taxonomy pt ON pt.id = t.product_id
        WHERE t.is_ambiguous = 0
        ORDER BY LENGTH(t.normalized) DESC
    """).fetchall()

    def product_of(text):
        for term, code in terms:
            if term and term in text:
                return code
        return "unspecified"

    # pull carrier-tagged query facts with modifier flags
    mods_select = ", ".join("sq.%s" % m for m in MODIFIERS)
    df = pd.read_sql_query(f"""
        SELECT sq.normalized_text, {PERIOD_SQL} AS period, {mods_select},
               SUM(p.impressions) AS impressions,
               SUM(p.clicks) AS clicks,
               SUM(p.primary_conv_count) AS conversions
        FROM search_query_performance p
        JOIN search_queries sq ON sq.id = p.query_id
        WHERE p.platform = 'google' AND p.device = 'ALL'
          AND sq.brand_category LIKE 'carrier_%'
        GROUP BY sq.normalized_text, period
        HAVING period IS NOT NULL
    """, conn)

    print("distinct carrier queries: %d -- matching brands..." % df["normalized_text"].nunique())
    cache = {}
    def brand_of(text):
        if text not in cache:
            m = bm.match(text)
            cache[text] = m.brand_name if (m and m.is_home_carrier) else None
        return cache[text]
    df["brand"] = df["normalized_text"].map(brand_of)
    df = df[df["brand"].isin(want_brands)].copy()
    df["product"] = df["normalized_text"].map(product_of)

    # ---- brand x product share of the brand's impressions, per period ----
    for brand in want_brands:
        sub = df[df["brand"] == brand]
        if sub.empty:
            continue
        print("\n" + "=" * 74)
        print("%s -- product mix (%% of brand impressions) and modifier rates" % brand.upper())
        print("=" * 74)
        pv = sub.pivot_table(index="product", columns="period",
                             values="impressions", aggfunc="sum").fillna(0)
        pv = pv[[p for p in PERIODS if p in pv.columns]]
        share = (pv / pv.sum() * 100).round(1)
        print(share.to_string())
        # modifier attach rates (impression-weighted, per period)
        print("\n  modifier attach rate (%% of brand impressions carrying the modifier):")
        for m in MODIFIERS:
            rates = []
            for per in [p for p in PERIODS if p in set(sub["period"])]:
                s = sub[sub["period"] == per]
                tot = s["impressions"].sum()
                flagged = s[s[m].notnull() & (s[m] != 0) & (s[m] != "")]["impressions"].sum()
                rates.append("%s %4.0f%%" % (per, flagged / tot * 100 if tot else 0))
            print("    %-22s %s" % (m.replace("modifier_", ""), " | ".join(rates)))

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "carrier_product_modifier_breakdown.csv")
    df.to_csv(out, index=False)

    # ---- Part 2: DKI daily check (trademark-restricted marks) ----
    print("\n" + "=" * 74)
    print("DKI IMPACT CHECK -- daily, DKI live 2026-07-14 (data lags ~2 days)")
    print("=" * 74)
    for token in ("costco", "aaa"):
        print("\n%s queries, daily since 2026-07-01:" % token.upper())
        for r in conn.execute("""
            SELECT p.report_date, SUM(p.impressions), SUM(p.clicks),
                   ROUND(SUM(p.primary_conv_count), 1)
            FROM search_query_performance p
            JOIN search_queries sq ON sq.id = p.query_id
            WHERE p.platform = 'google'
              AND sq.normalized_text LIKE '%' || ? || '%'
              AND p.report_date >= '2026-07-01'
            GROUP BY p.report_date ORDER BY p.report_date
        """, (token,)):
            marker = "  <-- DKI live" if r[0] == "2026-07-14" else ""
            print("  %s  impr %7s  clicks %6s  conv %6.1f%s"
                  % (r[0], f"{r[1]:,}", f"{r[2]:,}", r[3] or 0, marker))

    conn.close()
    print("\nCSV: %s" % out)


if __name__ == "__main__":
    main()
