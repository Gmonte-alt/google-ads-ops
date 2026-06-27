"""
10_tagging_diagnostic.py
--------------------------
Diagnostic report for search query and keyword tagging quality.
Answers:
  1. What queries are tagged (non-brand) and why?
  2. Which high-revenue queries have no brand tag?
  3. Which brand names appear in query text but weren't detected?
  4. What does the brand revenue breakdown look like by platform?

Outputs:
  kw_db_scripts/output/tagging_diagnostic.csv  -- full non-brand query list
  Terminal -- summary tables for quick review

Usage:
    conda activate paid-search
    python kw_db_scripts/10_tagging_diagnostic.py
"""

import sqlite3
import csv
import re
from pathlib import Path
from datetime import date

DB_PATH    = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
OUTPUT_DIR = Path(r"C:\Projects\google-ads-ops\kw_db_scripts\output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

GOOGLE_ACCOUNT_ID = "8886502147"
MS_ACCOUNT_ID     = "F149SN63"
REVENUE_START_DATE = date(2026, 1, 13)

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def main():
    print("=" * 65)
    print("10_tagging_diagnostic.py")
    print(f"Date : {date.today()}")
    print("=" * 65)

    conn = get_conn()
    cursor = conn.cursor()

    # ---------------------------------------------------------------
    # 1. Overall tag method distribution
    # ---------------------------------------------------------------
    print("\n[1] Search query tagging distribution:")
    rows = cursor.execute("""
        SELECT tag_method, COUNT(*) as cnt,
               ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 1) as pct
        FROM search_queries
        GROUP BY tag_method
        ORDER BY cnt DESC
    """).fetchall()
    for r in rows:
        print(f"  {r['tag_method'] or 'NULL':<20} {r['cnt']:>8,}  ({r['pct']}%)")

    # ---------------------------------------------------------------
    # 2. Non-brand revenue by platform YTD
    # ---------------------------------------------------------------
    print("\n[2] Non-brand / unclassified query revenue YTD (Jan 13+):")
    rows = cursor.execute("""
        SELECT
            a.platform,
            sq.modifier_brand,
            sq.tag_method,
            sq.vertical,
            COALESCE(SUM(sqp.primary_conv_value), 0) AS revenue,
            COUNT(DISTINCT sq.id)                     AS queries,
            COALESCE(SUM(sqp.clicks), 0)              AS clicks
        FROM search_query_performance sqp
        JOIN search_queries sq ON sqp.query_id = sq.id
        JOIN keyword_instances ki ON sqp.instance_id = ki.id
        JOIN accounts a ON ki.account_id = a.id
        WHERE sqp.device = 'ALL'
          AND sqp.report_date >= ?
          AND a.platform_account_id IN (?, ?)
          AND (sq.modifier_brand IS NULL OR sq.modifier_brand = '')
        GROUP BY a.platform, sq.tag_method, sq.vertical
        ORDER BY revenue DESC
    """, (str(REVENUE_START_DATE), GOOGLE_ACCOUNT_ID, MS_ACCOUNT_ID)).fetchall()

    total_nonbrand = sum(r['revenue'] for r in rows)
    print(f"  Total non-brand revenue: ${total_nonbrand:,.2f}")
    for r in rows:
        print(f"  {r['platform']:<12} {r['tag_method'] or 'NULL':<20} "
              f"vert={r['vertical'] or 'none':<15} "
              f"${r['revenue']:>9,.2f}  ({r['queries']:,} queries, {r['clicks']:,} clicks)")

    # ---------------------------------------------------------------
    # 3. Top 50 non-brand queries by revenue
    # ---------------------------------------------------------------
    print("\n[3] Top 50 non-brand queries by combined revenue YTD:")
    rows = cursor.execute("""
        SELECT
            sq.query_text,
            sq.normalized_text,
            sq.vertical,
            sq.tag_method,
            sq.tag_confidence,
            COALESCE(SUM(CASE WHEN a.platform='google'
                         THEN sqp.primary_conv_value END), 0) AS g_revenue,
            COALESCE(SUM(CASE WHEN a.platform='microsoft'
                         THEN sqp.primary_conv_value END), 0) AS ms_revenue,
            COALESCE(SUM(sqp.primary_conv_value), 0)           AS total_revenue,
            COALESCE(SUM(sqp.clicks), 0)                       AS clicks
        FROM search_query_performance sqp
        JOIN search_queries sq ON sqp.query_id = sq.id
        JOIN keyword_instances ki ON sqp.instance_id = ki.id
        JOIN accounts a ON ki.account_id = a.id
        WHERE sqp.device = 'ALL'
          AND sqp.report_date >= ?
          AND a.platform_account_id IN (?, ?)
          AND (sq.modifier_brand IS NULL OR sq.modifier_brand = '')
        GROUP BY sq.id
        ORDER BY total_revenue DESC
        LIMIT 50
    """, (str(REVENUE_START_DATE), GOOGLE_ACCOUNT_ID, MS_ACCOUNT_ID)).fetchall()

    print(f"  {'Query':<45} {'Vert':<8} {'Method':<15} "
          f"{'G_Rev':>9} {'MS_Rev':>9} {'Total':>9}")
    print("  " + "-" * 100)
    for r in rows:
        print(f"  {str(r['query_text']):<45} {str(r['vertical'] or ''):<8} "
              f"{str(r['tag_method'] or ''):<15} "
              f"${r['g_revenue']:>8,.2f} ${r['ms_revenue']:>8,.2f} "
              f"${r['total_revenue']:>8,.2f}")

    # ---------------------------------------------------------------
    # 4. Queries containing known brand text but no brand tag
    #    (tagger miss detection)
    # ---------------------------------------------------------------
    print("\n[4] Known brand names in query text but no modifier_brand tag:")
    brand_terms = cursor.execute(
        "SELECT brand_name, normalized_name FROM brand_registry "
        "WHERE bid_status != 'restricted' OR bid_status IS NULL "
        "ORDER BY LENGTH(normalized_name) DESC"
    ).fetchall()

    missed = cursor.execute("""
        SELECT
            sq.query_text, sq.normalized_text, sq.tag_method,
            sq.modifier_brand, sq.vertical,
            COALESCE(SUM(sqp.primary_conv_value), 0) AS revenue
        FROM search_query_performance sqp
        JOIN search_queries sq ON sqp.query_id = sq.id
        JOIN keyword_instances ki ON sqp.instance_id = ki.id
        JOIN accounts a ON ki.account_id = a.id
        WHERE sqp.device = 'ALL'
          AND sqp.report_date >= ?
          AND a.platform_account_id IN (?, ?)
          AND (sq.modifier_brand IS NULL OR sq.modifier_brand = '')
        GROUP BY sq.id
        HAVING revenue > 10
        ORDER BY revenue DESC
        LIMIT 500
    """, (str(REVENUE_START_DATE), GOOGLE_ACCOUNT_ID, MS_ACCOUNT_ID)).fetchall()

    missed_with_brand = []
    for r in missed:
        q_norm = str(r['normalized_text'] or '').lower()
        detected = []
        for br in brand_terms:
            bn = str(br['normalized_name']).lower()
            if len(bn) >= 3 and bn in q_norm:
                detected.append(br['brand_name'])
        if detected:
            missed_with_brand.append({
                'query': r['query_text'],
                'revenue': r['revenue'],
                'tag_method': r['tag_method'],
                'detected_brands': ', '.join(detected[:3]),
            })

    print(f"  Queries with brand text but no brand tag: {len(missed_with_brand)}")
    for r in missed_with_brand[:20]:
        print(f"  ${r['revenue']:>8,.2f}  '{r['query']}'  "
              f"-> detected: {r['detected_brands']}")

    # ---------------------------------------------------------------
    # 5. Brand revenue aggregation -- full window Google, YTD Microsoft
    # ---------------------------------------------------------------
    print("\n[5] Brand revenue -- Google (full window) vs Microsoft (YTD):")
    rows = cursor.execute("""
        SELECT
            COALESCE(sq.modifier_brand, '(non-brand)') AS brand,
            sq.brand_category,
            COALESCE(SUM(CASE WHEN a.platform='google'
                         THEN sqp.primary_conv_value END), 0) AS g_revenue,
            COALESCE(SUM(CASE WHEN a.platform='microsoft'
                         THEN sqp.primary_conv_value END), 0) AS ms_revenue
        FROM search_query_performance sqp
        JOIN search_queries sq ON sqp.query_id = sq.id
        JOIN keyword_instances ki ON sqp.instance_id = ki.id
        JOIN accounts a ON ki.account_id = a.id
        WHERE sqp.device = 'ALL'
          AND (
              (a.platform = 'google')
              OR
              (a.platform = 'microsoft' AND sqp.report_date >= ?)
          )
          AND a.platform_account_id IN (?, ?)
        GROUP BY sq.modifier_brand, sq.brand_category
        HAVING (g_revenue + ms_revenue) > 100
        ORDER BY (g_revenue + ms_revenue) DESC
        LIMIT 40
    """, (str(REVENUE_START_DATE), GOOGLE_ACCOUNT_ID, MS_ACCOUNT_ID)).fetchall()

    print(f"  {'Brand':<30} {'Category':<35} {'Google Rev':>12} {'MS Rev':>10} {'Combined':>10}")
    print("  " + "-" * 100)
    for r in rows:
        print(f"  {str(r['brand']):<30} {str(r['brand_category'] or ''):<35} "
              f"${r['g_revenue']:>11,.2f} ${r['ms_revenue']:>9,.2f} "
              f"${r['g_revenue']+r['ms_revenue']:>9,.2f}")

    # ---------------------------------------------------------------
    # 6. Export full non-brand query list to CSV
    # ---------------------------------------------------------------
    output_path = OUTPUT_DIR / "tagging_diagnostic.csv"
    all_nonbrand = cursor.execute("""
        SELECT
            sq.query_text,
            sq.normalized_text,
            sq.vertical,
            sq.brand_category,
            sq.modifier_brand,
            sq.modifier_geo,
            sq.modifier_price,
            sq.modifier_coverage,
            sq.tag_method,
            sq.tag_confidence,
            sq.first_seen_platform,
            sq.first_seen_date,
            COALESCE(SUM(CASE WHEN a.platform='google'
                         THEN sqp.primary_conv_value END), 0) AS g_revenue,
            COALESCE(SUM(CASE WHEN a.platform='microsoft'
                         THEN sqp.primary_conv_value END), 0) AS ms_revenue,
            COALESCE(SUM(sqp.primary_conv_value), 0)           AS total_revenue,
            COALESCE(SUM(sqp.clicks), 0)                       AS clicks,
            COALESCE(SUM(sqp.cost_micros), 0) / 1e6            AS spend
        FROM search_query_performance sqp
        JOIN search_queries sq ON sqp.query_id = sq.id
        JOIN keyword_instances ki ON sqp.instance_id = ki.id
        JOIN accounts a ON ki.account_id = a.id
        WHERE sqp.device = 'ALL'
          AND sqp.report_date >= ?
          AND a.platform_account_id IN (?, ?)
          AND (sq.modifier_brand IS NULL OR sq.modifier_brand = '')
        GROUP BY sq.id
        ORDER BY total_revenue DESC
    """, (str(REVENUE_START_DATE), GOOGLE_ACCOUNT_ID, MS_ACCOUNT_ID)).fetchall()

    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([
            'query_text', 'vertical', 'brand_category', 'modifier_brand',
            'modifier_geo', 'modifier_price', 'modifier_coverage',
            'tag_method', 'tag_confidence', 'first_seen_platform',
            'first_seen_date', 'google_revenue', 'ms_revenue',
            'total_revenue', 'clicks', 'spend'
        ])
        for r in all_nonbrand:
            writer.writerow([
                r['query_text'], r['vertical'], r['brand_category'],
                r['modifier_brand'], r['modifier_geo'], r['modifier_price'],
                r['modifier_coverage'], r['tag_method'], r['tag_confidence'],
                r['first_seen_platform'], r['first_seen_date'],
                round(r['g_revenue'], 2), round(r['ms_revenue'], 2),
                round(r['total_revenue'], 2), r['clicks'],
                round(r['spend'], 2)
            ])

    print(f"\n[6] Full non-brand query list exported:")
    print(f"  {output_path}")
    print(f"  {len(all_nonbrand):,} rows")

    conn.close()
    print("\nDone.")

if __name__ == "__main__":
    main()
