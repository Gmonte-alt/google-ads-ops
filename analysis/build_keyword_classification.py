#!/usr/bin/env python
"""
build_keyword_classification.py -- analysis\ -- keyword-side companion to
build_query_classification.py. Classifies every account-1 keyword PLACEMENT
(ad_group_criterion_id) with the SAME canonical modules (brand_matcher,
query_grouper, product terms), stores match_type, and exports the
group x match_type and brand x match_type five-period cuts.

Differences from the query side, by design:
  - No 08 tags exist on keywords -> the matcher runs on EVERY distinct keyword
    text (fast: distinct texts are few; shared across state-shell ad groups).
  - category comes from the matcher's registry row directly (matcher is the
    only authority here -- no stored-tag reconciliation needed).
  - match_type is a placement attribute (from keyword_instances, post enum
    fix) and a first-class analysis axis.

  --rebuild      classify + write keyword_classification (minutes, not 40)
  --export-xlsx  write keyword_share_analysis.xlsx + full CSV

    python build_keyword_classification.py --db "..." --rebuild --export-xlsx
"""
import argparse
import datetime
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

DDL = """
CREATE TABLE IF NOT EXISTS keyword_classification (
    account_id             INTEGER NOT NULL,
    ad_group_criterion_id  TEXT NOT NULL,     -- the per-placement edit key
    keyword_text           TEXT,
    match_type             TEXT,              -- placement attribute (post enum fix)
    category               TEXT NOT NULL,     -- carrier|generic|aggregator_competitor|confie|state_pool|other_brand
    brand_name             TEXT,
    is_home_carrier        INTEGER,
    product_code           TEXT,
    query_group            TEXT,              -- V3 taxonomy (query_grouper.py)
    classified_at          TEXT NOT NULL,
    source                 TEXT NOT NULL,
    PRIMARY KEY (account_id, ad_group_criterion_id)
);
CREATE INDEX IF NOT EXISTS idx_kc_brand ON keyword_classification (brand_name);
CREATE INDEX IF NOT EXISTS idx_kc_group ON keyword_classification (query_group);
CREATE INDEX IF NOT EXISTS idx_kc_match ON keyword_classification (match_type);
"""


def category_from_registry(brand_category):
    if brand_category is None:
        return "generic"
    if brand_category.startswith("carrier_"):
        return "carrier"
    if brand_category.startswith("aggregator"):
        return "aggregator_competitor"
    if brand_category == "confie_subsidiary":
        return "confie"
    if brand_category == "state_residual_pool":
        return "state_pool"
    return "other_brand"


def rebuild(conn, matcher_dir, account_id):
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, matcher_dir)
    sys.path.insert(0, here)
    from brand_matcher import BrandMatcher, normalize
    from query_grouper import query_group
    bm = BrandMatcher(conn)

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
        return None

    rows = conn.execute("""
        SELECT ad_group_criterion_id, keyword_text, match_type
        FROM keyword_instances WHERE account_id = ?
    """, (account_id,)).fetchall()
    print("placements: %d" % len(rows))

    # classify each distinct text once
    texts = sorted({normalize(r[1] or "") for r in rows})
    print("distinct keyword texts: %d -- classifying..." % len(texts))
    cls = {}
    for i, t in enumerate(texts):
        m = bm.match(t)
        if m:
            cat = category_from_registry(m.brand_category)
            brand, is_home = m.brand_name, int(m.is_home_carrier)
        else:
            cat, brand, is_home = "generic", None, None
        cls[t] = (cat, brand, is_home, product_of(t),
                  query_group(t, brand=bool(m)))
        if (i + 1) % 10000 == 0:
            print("  %d / %d" % (i + 1, len(texts)), flush=True)

    now = datetime.datetime.now().isoformat(timespec="seconds")
    out = []
    for agc, text, mt in rows:
        cat, brand, is_home, prod, grp = cls[normalize(text or "")]
        out.append((account_id, agc, text, mt, cat, brand, is_home, prod, grp,
                    now, "build_keyword_classification v1"))

    conn.execute("DROP TABLE IF EXISTS keyword_classification")
    conn.executescript(DDL)
    conn.executemany(
        "INSERT INTO keyword_classification VALUES (?,?,?,?,?,?,?,?,?,?,?)", out)
    conn.commit()
    print("keyword_classification written: %d placements" % len(out))


def export(conn, out_path, account_id):
    df = pd.read_sql_query(f"""
        SELECT kc.keyword_text, kc.query_group, kc.category, kc.brand_name AS brand,
               kc.product_code AS product, kc.match_type,
               {PERIOD_SQL} AS period,
               SUM(p.impressions) AS impressions, SUM(p.clicks) AS clicks,
               SUM(p.primary_conv_count) AS conversions,
               SUM(p.primary_conv_value) AS conv_value,
               SUM(p.cost_micros)/1e6 AS cost
        FROM keyword_performance_daily p
        JOIN keyword_instances ki ON ki.id = p.instance_id
        JOIN keyword_classification kc
             ON kc.account_id = ki.account_id
            AND kc.ad_group_criterion_id = ki.ad_group_criterion_id
        WHERE ki.account_id = ? AND p.device = 'ALL'
        GROUP BY kc.keyword_text, kc.query_group, kc.category, kc.brand_name,
                 kc.product_code, kc.match_type, period
        HAVING period IS NOT NULL
    """, conn, params=(account_id,))
    # Export-time override (Gilbert 2026-07-21): brand+product keywords with NO
    # specific product (product IS NULL -- e.g. "state farm insurance") are
    # navigational -> regroup into pure brand name. DB not retagged.
    mask = (df["query_group"] == "1. Brand + product name") & (df["product"].isna())
    df.loc[mask, "query_group"] = "2. Pure brand name"
    print("regrouped %d brand+product rows (no specific product) -> pure brand name"
          % int(mask.sum()))

    with pd.ExcelWriter(out_path, engine="openpyxl") as xw:
        # group x match_type share of impressions per period
        gm = df.pivot_table(index=["query_group", "match_type"], columns="period",
                            values="impressions", aggfunc="sum").fillna(0)
        gm = gm[[p for p in PERIODS if p in gm.columns]]
        (gm / gm.sum() * 100).round(2).to_excel(xw, sheet_name="group_x_matchtype_impr")

        # brand x match_type share of carrier impressions per period
        car = df[(df["category"] == "carrier") & df["brand"].notna()]
        bmix = car.pivot_table(index=["brand", "match_type"], columns="period",
                               values="impressions", aggfunc="sum").fillna(0)
        bmix = bmix[[p for p in PERIODS if p in bmix.columns]]
        (bmix / bmix.sum() * 100).round(2).to_excel(xw, sheet_name="brand_x_matchtype_impr")

        # match_type share overall (impr + conv)
        for metric in ("impressions", "conversions"):
            mt = df.pivot_table(index="match_type", columns="period",
                                values=metric, aggfunc="sum").fillna(0)
            mt = mt[[p for p in PERIODS if p in mt.columns]]
            ms = (mt / mt.sum() * 100).round(1)
            if len(ms.columns) >= 2:
                ms["delta_pts"] = ms[ms.columns[-1]] - ms[ms.columns[0]]
            ms.to_excel(xw, sheet_name="matchtype_share_%s" % metric[:4])

        # category share (for reconciliation against the query-side workbook)
        cat = df.pivot_table(index="category", columns="period",
                             values="impressions", aggfunc="sum").fillna(0)
        cat = cat[[p for p in PERIODS if p in cat.columns]]
        (cat / cat.sum() * 100).round(1).to_excel(xw, sheet_name="category_share_impr")

        # raw keywords tab (>= 50 lifetime impressions to keep it usable)
        raw = df.pivot_table(
            index=["keyword_text", "match_type", "query_group", "category",
                   "brand", "product"],
            columns="period", values="impressions", aggfunc="sum").fillna(0)
        raw["total"] = raw.sum(axis=1)
        raw = raw[raw["total"] >= 50].sort_values("total", ascending=False)
        raw.to_excel(xw, sheet_name="raw_keywords_50plus_impr")

    csv_path = out_path.replace(".xlsx", "_raw_full.csv")
    df.to_csv(csv_path, index=False)
    print("workbook: %s" % out_path)
    print("full raw CSV: %s" % csv_path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--export-xlsx", action="store_true")
    ap.add_argument("--account-id", type=int, default=1)
    ap.add_argument("--brand-matcher-dir",
                    default=r"C:\Projects\google-ads-ops\campaign_db_scripts\change_history")
    args = ap.parse_args()

    if args.rebuild:
        conn = sqlite3.connect(args.db)
        rebuild(conn, args.brand_matcher_dir, args.account_id)
        conn.close()
    if args.export_xlsx:
        conn = sqlite3.connect("file:%s?mode=ro" % args.db, uri=True)
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "keyword_share_analysis.xlsx")
        export(conn, out, args.account_id)
        conn.close()
    if not (args.rebuild or args.export_xlsx):
        print("nothing to do: pass --rebuild and/or --export-xlsx")


if __name__ == "__main__":
    main()
