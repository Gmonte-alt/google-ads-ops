#!/usr/bin/env python
"""
build_query_classification.py -- analysis\ -- persist the query classification
(category / brand / product) as a DB table, then export a manager-ready Excel
workbook in seconds. Two modes:

  --rebuild      classify ALL google-platform queries once (the slow ~40-min
                 matcher pass) and write the query_classification table.
                 Re-run only when the registry/matcher/taxonomy changes.
  --export-xlsx  read the persisted table + search_query_performance and write
                 query_share_analysis.xlsx (fast, no matching).

    python build_query_classification.py --db "..." --rebuild
    python build_query_classification.py --db "..." --export-xlsx
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
CREATE TABLE IF NOT EXISTS query_classification (
    query_id        INTEGER PRIMARY KEY,      -- search_queries.id
    normalized_text TEXT NOT NULL,
    category        TEXT NOT NULL,            -- carrier|generic|aggregator_competitor|confie|state_pool|other_brand|excluded_offvertical
    brand_name      TEXT,                     -- matcher brand (carrier queries)
    is_home_carrier INTEGER,                  -- 1/0/NULL
    product_code    TEXT,                     -- product_taxonomy containment match
    query_group     TEXT,                     -- V3 taxonomy label (query_grouper.py, extracted from parity_common)
    classified_at   TEXT NOT NULL,
    source          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_qc_category ON query_classification (category);
CREATE INDEX IF NOT EXISTS idx_qc_brand    ON query_classification (brand_name);
CREATE INDEX IF NOT EXISTS idx_qc_group    ON query_classification (query_group);
"""


def category_of(brand_category, vertical):
    if vertical in ("medicare", "life"):
        return "excluded_offvertical"
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


def rebuild(conn, matcher_dir):
    sys.path.insert(0, matcher_dir)
    from brand_matcher import BrandMatcher
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
        SELECT DISTINCT sq.id, sq.normalized_text, sq.brand_category, sq.vertical
        FROM search_queries sq
        JOIN search_query_performance p ON p.query_id = sq.id
        WHERE p.platform = 'google'
    """).fetchall()
    print("classifying %d queries (matcher runs on carrier-tagged only)..." % len(rows))

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from query_grouper import query_group

    now = datetime.datetime.now().isoformat(timespec="seconds")
    out, done, downgraded = [], 0, 0
    for qid, text, bcat, vert in rows:
        cat = category_of(bcat, vert)
        brand, is_home = None, None
        if cat == "carrier":
            m = bm.match(text)
            if m:
                brand, is_home = m.brand_name, int(m.is_home_carrier)
            else:
                # stored tag says carrier but the canonical matcher finds no brand:
                # matcher is the authority -> reclassify to generic (08 mis-tag noise)
                cat = "generic"
                downgraded += 1
        has_brand = cat in ("carrier", "aggregator_competitor", "confie",
                            "state_pool", "other_brand")
        qgroup = query_group(text or "", brand=has_brand)
        out.append((qid, text, cat, brand, is_home, product_of(text or ""),
                    qgroup, now, "build_query_classification v2"))
        done += 1
        if done % 20000 == 0:
            print("  %d / %d" % (done, len(rows)), flush=True)
    print("carrier-tagged queries downgraded to generic (no canonical brand match): %d"
          % downgraded)

    # Derived artifact, regenerated wholesale each rebuild: drop-and-recreate so
    # schema changes (new columns) always take effect. Never contains hand-edits.
    conn.execute("DROP TABLE IF EXISTS query_classification")
    conn.executescript(DDL)
    conn.executemany(
        "INSERT INTO query_classification VALUES (?,?,?,?,?,?,?,?,?)", out)
    conn.commit()
    print("query_classification written: %d rows" % len(out))


def export(conn, out_path):
    df = pd.read_sql_query(f"""
        SELECT qc.normalized_text AS query, qc.category, qc.brand_name AS brand,
               qc.product_code AS product, qc.query_group, {PERIOD_SQL} AS period,
               SUM(p.impressions) AS impressions, SUM(p.clicks) AS clicks,
               SUM(p.primary_conv_count) AS conversions,
               SUM(p.primary_conv_value) AS conv_value,
               SUM(p.cost_micros)/1e6 AS cost
        FROM search_query_performance p
        JOIN query_classification qc ON qc.query_id = p.query_id
        WHERE p.platform = 'google' AND p.device = 'ALL'
        GROUP BY qc.query_id, period HAVING period IS NOT NULL
    """, conn)
    # Export-time override (Gilbert 2026-07-21): brand+product queries with NO
    # specific product (product IS NULL -- e.g. "state farm insurance") are
    # navigational, not product-qualified -> regroup into pure brand name.
    # DB tables are NOT retagged; this is analysis-layer only.
    mask = (df["query_group"] == "1. Brand + product name") & (df["product"].isna())
    df.loc[mask, "query_group"] = "2. Pure brand name"
    print("regrouped %d brand+product rows (no specific product) -> pure brand name"
          % int(mask.sum()))
    live = df[df["category"] != "excluded_offvertical"]

    with pd.ExcelWriter(out_path, engine="openpyxl") as xw:
        # Tab 1: category share by period (impressions / conversions)
        for metric in ("impressions", "conversions"):
            pv = live.pivot_table(index="category", columns="period",
                                  values=metric, aggfunc="sum").fillna(0)
            pv = pv[[p for p in PERIODS if p in pv.columns]]
            (pv / pv.sum() * 100).round(1).to_excel(
                xw, sheet_name="category_share_%s" % metric[:4])

        # Tab 2: carrier brand share of carrier impressions
        car = live[(live["category"] == "carrier") & live["brand"].notna()]
        bp = car.pivot_table(index="brand", columns="period",
                             values="impressions", aggfunc="sum").fillna(0)
        bp = bp[[p for p in PERIODS if p in bp.columns]]
        bs = (bp / bp.sum() * 100).round(2)
        if len(bs.columns) >= 2:
            bs["delta_pts"] = bs[bs.columns[-1]] - bs[bs.columns[0]]
        bs.sort_values(bs.columns[-1], ascending=False).to_excel(
            xw, sheet_name="brand_share_movers")

        # Tab 3: query-group share of impressions and conversions per period
        for metric in ("impressions", "conversions"):
            gp = live.pivot_table(index="query_group", columns="period",
                                  values=metric, aggfunc="sum").fillna(0)
            gp = gp[[p for p in PERIODS if p in gp.columns]]
            gs = (gp / gp.sum() * 100).round(1)
            if len(gs.columns) >= 2:
                gs["delta_pts"] = gs[gs.columns[-1]] - gs[gs.columns[0]]
            gs.sort_values(gs.columns[-2] if "delta_pts" in gs.columns else gs.columns[-1],
                           ascending=False).to_excel(
                xw, sheet_name="group_share_%s" % metric[:4])

        # Tab 4: brand x product impressions
        car.pivot_table(index=["brand", "product"], columns="period",
                        values="impressions", aggfunc="sum").fillna(0).to_excel(
            xw, sheet_name="brand_x_product")

        # Tab 5: raw queries (>= 50 lifetime impressions to keep it usable)
        raw = df.pivot_table(index=["query", "query_group", "category", "brand", "product"],
                             columns="period", values="impressions",
                             aggfunc="sum").fillna(0)
        raw["total"] = raw.sum(axis=1)
        raw = raw[raw["total"] >= 50].sort_values("total", ascending=False)
        raw.to_excel(xw, sheet_name="raw_queries_50plus_impr")

    # full raw CSV alongside (no threshold)
    csv_path = out_path.replace(".xlsx", "_raw_full.csv")
    df.to_csv(csv_path, index=False)
    print("workbook: %s" % out_path)
    print("full raw CSV: %s" % csv_path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--export-xlsx", action="store_true")
    ap.add_argument("--brand-matcher-dir",
                    default=r"C:\Projects\google-ads-ops\campaign_db_scripts\change_history")
    args = ap.parse_args()

    if args.rebuild:
        conn = sqlite3.connect(args.db)
        rebuild(conn, args.brand_matcher_dir)
        conn.close()
    if args.export_xlsx:
        conn = sqlite3.connect("file:%s?mode=ro" % args.db, uri=True)
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "query_share_analysis.xlsx")
        export(conn, out)
        conn.close()
    if not (args.rebuild or args.export_xlsx):
        print("nothing to do: pass --rebuild and/or --export-xlsx")


if __name__ == "__main__":
    main()
