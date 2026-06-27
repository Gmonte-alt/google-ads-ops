"""
10_tagging_diagnostic.py  [v2 — multi-account]
--------------------------
Diagnostic report for search query and keyword tagging quality.

Replaces the hardcoded Home Insurance version. Pass --account to select
which account to diagnose. Defaults to 'home' for backward compatibility.

Answers:
  1. Tag method distribution for the selected account
  2. Non-brand revenue breakdown by vertical and tag method
  3. Top 50 non-brand queries by revenue
  4. Known brand names in query text that weren't detected (tagger misses)
  5. Brand revenue aggregation across the full date range
  6. Vertical coverage -- what share of revenue is tagged vs unclassified
  7. CSV export of full non-brand query list

Usage:
    conda activate paid-search

    python kw_db_scripts/10_tagging_diagnostic.py --account home
    python kw_db_scripts/10_tagging_diagnostic.py --account medicare
    python kw_db_scripts/10_tagging_diagnostic.py --account auto
    python kw_db_scripts/10_tagging_diagnostic.py --account term_life
    python kw_db_scripts/10_tagging_diagnostic.py --account auto_quote

    # Limit revenue window to last N days
    python kw_db_scripts/10_tagging_diagnostic.py --account medicare --days 90
"""

import sqlite3
import csv
import argparse
from pathlib import Path
from datetime import date, timedelta

DB_PATH    = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
OUTPUT_DIR = Path(r"C:\Projects\google-ads-ops\kw_db_scripts\output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Maps --account to Google platform_account_id and optional Microsoft account ID.
# microsoft_id is None for accounts with no active Microsoft presence.
ACCOUNT_MAP = {
    "home": {
        "display_name":  "Home Insurance",
        "google_id":     "8886502147",
        "microsoft_id":  "F149SN63",
        "ms_start_date": date(2026, 1, 13),   # when Microsoft offline uploads began
    },
    "medicare": {
        "display_name":  "Medicare",
        "google_id":     "5310820901",
        "microsoft_id":  None,                 # Google-only
        "ms_start_date": None,
    },
    "auto": {
        "display_name":  "Auto Insurance (autoinsurance.org)",
        "google_id":     "3863660890",
        "microsoft_id":  "F149Y8EN",
        "ms_start_date": None,                 # update when MS offline uploads begin
    },
    "term_life": {
        "display_name":  "Term Life",
        "google_id":     "1671155176",
        "microsoft_id":  "F149ZJ6F",
        "ms_start_date": None,
    },
    "auto_quote": {
        "display_name":  "Auto Insurance (auto.quote.com)",
        "google_id":     "1006729211",
        "microsoft_id":  None,
        "ms_start_date": None,
    },
}


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Tagging diagnostic report for a single account."
    )
    parser.add_argument(
        "--account",
        default="home",
        choices=list(ACCOUNT_MAP.keys()),
        help="Account to diagnose. Default: home",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        metavar="N",
        help="Limit revenue window to last N days. Default: full date range.",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Database connection
# ---------------------------------------------------------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Build account ID list and date filter for queries
# ---------------------------------------------------------------------------

def build_account_filter(cfg: dict, revenue_start: date | None):
    """
    Returns (account_ids, revenue_start_str, platform_clause) for use in
    SQL WHERE clauses.

    account_ids: list of platform_account_id strings to include
    revenue_start_str: ISO date string or '2000-01-01' if no restriction
    platform_clause: extra SQL for Microsoft date restriction if applicable
    """
    ids = [cfg["google_id"]]
    if cfg["microsoft_id"]:
        ids.append(cfg["microsoft_id"])

    start_str = revenue_start.isoformat() if revenue_start else "2000-01-01"
    return ids, start_str


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()
    cfg  = ACCOUNT_MAP[args.account]

    # Revenue date window
    if args.days:
        revenue_start = date.today() - timedelta(days=args.days)
    else:
        revenue_start = None   # no restriction -- full date range

    account_ids, start_str = build_account_filter(cfg, revenue_start)

    # Placeholders for SQL IN clause
    id_placeholders = ",".join("?" * len(account_ids))

    print("=" * 65)
    print("10_tagging_diagnostic.py")
    print(f"Account  : {cfg['display_name']}")
    print(f"Platform : Google{' + Microsoft' if cfg['microsoft_id'] else ' only'}")
    print(f"Rev from : {start_str if revenue_start else 'full range'}")
    print(f"Date     : {date.today()}")
    print("=" * 65)

    conn   = get_conn()
    cursor = conn.cursor()

    # Verify the account exists in the DB
    for aid in account_ids:
        row = cursor.execute(
            "SELECT account_name FROM accounts WHERE platform_account_id = ?", (aid,)
        ).fetchone()
        if not row:
            print(f"\nWARNING: account {aid} not found in accounts table.")
            print("Run 01_create_schema.py and 02_sync_keyword_structure.py first.")

    # ---------------------------------------------------------------
    # 1. Tag method distribution for this account
    # ---------------------------------------------------------------
    print("\n[1] Search query tagging distribution (this account):")
    rows = cursor.execute(f"""
        SELECT
            sq.tag_method,
            COUNT(DISTINCT sq.id)                     AS queries,
            COALESCE(SUM(sqp.primary_conv_value), 0)  AS revenue,
            ROUND(COUNT(DISTINCT sq.id) * 100.0 /
                  SUM(COUNT(DISTINCT sq.id)) OVER (), 1) AS pct
        FROM search_query_performance sqp
        JOIN search_queries sq   ON sqp.query_id   = sq.id
        JOIN keyword_instances ki ON sqp.instance_id = ki.id
        JOIN accounts a           ON ki.account_id  = a.id
        WHERE sqp.device = 'ALL'
          AND sqp.report_date >= ?
          AND a.platform_account_id IN ({id_placeholders})
        GROUP BY sq.tag_method
        ORDER BY queries DESC
    """, [start_str] + account_ids).fetchall()

    total_queries = sum(r["queries"] for r in rows)
    total_revenue = sum(r["revenue"] for r in rows)
    print(f"  {'Method':<20} {'Queries':>9} {'Pct':>7}  {'Revenue':>12}")
    print(f"  {'-'*20} {'-'*9} {'-'*7}  {'-'*12}")
    for r in rows:
        print(f"  {str(r['tag_method'] or 'NULL'):<20} "
              f"{r['queries']:>9,} "
              f"{r['pct']:>6.1f}%  "
              f"${r['revenue']:>11,.2f}")
    print(f"  {'TOTAL':<20} {total_queries:>9,} {'100.0':>6}%  ${total_revenue:>11,.2f}")

    # ---------------------------------------------------------------
    # 2. Vertical coverage -- what share of revenue is tagged
    # ---------------------------------------------------------------
    print("\n[2] Revenue by vertical (tagged vs unclassified):")
    rows = cursor.execute(f"""
        SELECT
            COALESCE(sq.vertical, 'NULL')            AS vertical,
            COUNT(DISTINCT sq.id)                    AS queries,
            COALESCE(SUM(sqp.primary_conv_value), 0) AS revenue,
            COALESCE(SUM(sqp.clicks), 0)             AS clicks
        FROM search_query_performance sqp
        JOIN search_queries sq    ON sqp.query_id   = sq.id
        JOIN keyword_instances ki ON sqp.instance_id = ki.id
        JOIN accounts a           ON ki.account_id  = a.id
        WHERE sqp.device = 'ALL'
          AND sqp.report_date >= ?
          AND a.platform_account_id IN ({id_placeholders})
        GROUP BY sq.vertical
        ORDER BY revenue DESC
    """, [start_str] + account_ids).fetchall()

    print(f"  {'Vertical':<20} {'Queries':>9} {'Revenue':>12} {'Clicks':>9}")
    print(f"  {'-'*20} {'-'*9} {'-'*12} {'-'*9}")
    for r in rows:
        print(f"  {r['vertical']:<20} {r['queries']:>9,} "
              f"${r['revenue']:>11,.2f} {r['clicks']:>9,}")

    # ---------------------------------------------------------------
    # 3. Non-brand revenue by tag method and vertical
    # ---------------------------------------------------------------
    print("\n[3] Non-brand revenue by tag method and vertical:")
    rows = cursor.execute(f"""
        SELECT
            sq.tag_method,
            COALESCE(sq.vertical, 'unclassified')    AS vertical,
            COUNT(DISTINCT sq.id)                    AS queries,
            COALESCE(SUM(sqp.primary_conv_value), 0) AS revenue,
            COALESCE(SUM(sqp.clicks), 0)             AS clicks
        FROM search_query_performance sqp
        JOIN search_queries sq    ON sqp.query_id   = sq.id
        JOIN keyword_instances ki ON sqp.instance_id = ki.id
        JOIN accounts a           ON ki.account_id  = a.id
        WHERE sqp.device = 'ALL'
          AND sqp.report_date >= ?
          AND a.platform_account_id IN ({id_placeholders})
          AND (sq.modifier_brand IS NULL OR sq.modifier_brand = '')
        GROUP BY sq.tag_method, sq.vertical
        ORDER BY revenue DESC
    """, [start_str] + account_ids).fetchall()

    total_nonbrand = sum(r["revenue"] for r in rows)
    print(f"  Total non-brand revenue: ${total_nonbrand:,.2f}")
    print(f"  {'Method':<20} {'Vertical':<18} {'Queries':>9} {'Revenue':>12} {'Clicks':>9}")
    print(f"  {'-'*20} {'-'*18} {'-'*9} {'-'*12} {'-'*9}")
    for r in rows:
        print(f"  {str(r['tag_method'] or 'NULL'):<20} "
              f"{r['vertical']:<18} "
              f"{r['queries']:>9,} "
              f"${r['revenue']:>11,.2f} "
              f"{r['clicks']:>9,}")

    # ---------------------------------------------------------------
    # 4. Top 50 non-brand queries by revenue
    # ---------------------------------------------------------------
    print("\n[4] Top 50 non-brand queries by revenue:")
    rows = cursor.execute(f"""
        SELECT
            sq.query_text,
            sq.vertical,
            sq.tag_method,
            sq.modifier_geo,
            COALESCE(SUM(CASE WHEN a.platform = 'google'
                         THEN sqp.primary_conv_value END), 0) AS g_revenue,
            COALESCE(SUM(CASE WHEN a.platform = 'microsoft'
                         THEN sqp.primary_conv_value END), 0) AS ms_revenue,
            COALESCE(SUM(sqp.primary_conv_value), 0)          AS total_revenue,
            COALESCE(SUM(sqp.clicks), 0)                      AS clicks
        FROM search_query_performance sqp
        JOIN search_queries sq    ON sqp.query_id   = sq.id
        JOIN keyword_instances ki ON sqp.instance_id = ki.id
        JOIN accounts a           ON ki.account_id  = a.id
        WHERE sqp.device = 'ALL'
          AND sqp.report_date >= ?
          AND a.platform_account_id IN ({id_placeholders})
          AND (sq.modifier_brand IS NULL OR sq.modifier_brand = '')
        GROUP BY sq.id
        ORDER BY total_revenue DESC
        LIMIT 50
    """, [start_str] + account_ids).fetchall()

    print(f"  {'Query':<45} {'Vert':<12} {'Method':<15} {'G_Rev':>10} {'MS_Rev':>8} {'Total':>10}")
    print("  " + "-" * 105)
    for r in rows:
        print(f"  {str(r['query_text'])[:44]:<45} "
              f"{str(r['vertical'] or ''):<12} "
              f"{str(r['tag_method'] or ''):<15} "
              f"${r['g_revenue']:>9,.2f} "
              f"${r['ms_revenue']:>7,.2f} "
              f"${r['total_revenue']:>9,.2f}")

    # ---------------------------------------------------------------
    # 5. Tagger miss detection
    #    Queries with known brand text but no modifier_brand tag
    # ---------------------------------------------------------------
    print("\n[5] Known brand names in query text but no modifier_brand tag:")
    brand_terms = cursor.execute("""
        SELECT brand_name, normalized_name
        FROM brand_registry
        WHERE normalized_name != 'unattributed_negative_keyword'
        ORDER BY LENGTH(normalized_name) DESC
    """).fetchall()

    candidates = cursor.execute(f"""
        SELECT
            sq.query_text, sq.normalized_text, sq.tag_method,
            sq.modifier_brand, sq.vertical,
            COALESCE(SUM(sqp.primary_conv_value), 0) AS revenue
        FROM search_query_performance sqp
        JOIN search_queries sq    ON sqp.query_id   = sq.id
        JOIN keyword_instances ki ON sqp.instance_id = ki.id
        JOIN accounts a           ON ki.account_id  = a.id
        WHERE sqp.device = 'ALL'
          AND sqp.report_date >= ?
          AND a.platform_account_id IN ({id_placeholders})
          AND (sq.modifier_brand IS NULL OR sq.modifier_brand = '')
        GROUP BY sq.id
        HAVING revenue > 10
        ORDER BY revenue DESC
        LIMIT 500
    """, [start_str] + account_ids).fetchall()

    missed = []
    for r in candidates:
        q_norm    = str(r["normalized_text"] or "").lower()
        detected  = []
        for br in brand_terms:
            bn = str(br["normalized_name"]).lower()
            if len(bn) >= 3 and bn in q_norm:
                detected.append(br["brand_name"])
        if detected:
            missed.append({
                "query":           r["query_text"],
                "revenue":         r["revenue"],
                "tag_method":      r["tag_method"],
                "detected_brands": ", ".join(detected[:3]),
            })

    print(f"  Queries with brand text but no brand tag: {len(missed)}")
    if missed:
        print(f"  {'Revenue':>10}  {'Detected brand(s)':<30}  Query")
        print(f"  {'-'*10}  {'-'*30}  {'-'*40}")
        for r in missed[:25]:
            print(f"  ${r['revenue']:>9,.2f}  "
                  f"{r['detected_brands']:<30}  "
                  f"'{str(r['query'])[:60]}'")
    if len(missed) > 25:
        print(f"  ... and {len(missed) - 25} more (see CSV export)")

    # ---------------------------------------------------------------
    # 6. Brand revenue aggregation
    # ---------------------------------------------------------------
    print("\n[6] Brand revenue breakdown (top 40 by combined revenue):")
    rows = cursor.execute(f"""
        SELECT
            COALESCE(sq.modifier_brand, '(non-brand)') AS brand,
            sq.brand_category,
            sq.vertical,
            COALESCE(SUM(CASE WHEN a.platform = 'google'
                         THEN sqp.primary_conv_value END), 0) AS g_revenue,
            COALESCE(SUM(CASE WHEN a.platform = 'microsoft'
                         THEN sqp.primary_conv_value END), 0) AS ms_revenue,
            COALESCE(SUM(sqp.primary_conv_value), 0)          AS total_revenue
        FROM search_query_performance sqp
        JOIN search_queries sq    ON sqp.query_id   = sq.id
        JOIN keyword_instances ki ON sqp.instance_id = ki.id
        JOIN accounts a           ON ki.account_id  = a.id
        WHERE sqp.device = 'ALL'
          AND sqp.report_date >= ?
          AND a.platform_account_id IN ({id_placeholders})
        GROUP BY sq.modifier_brand, sq.brand_category, sq.vertical
        HAVING total_revenue > 100
        ORDER BY total_revenue DESC
        LIMIT 40
    """, [start_str] + account_ids).fetchall()

    print(f"  {'Brand':<28} {'Category':<32} {'Vert':<10} "
          f"{'G_Rev':>11} {'MS_Rev':>9} {'Total':>11}")
    print("  " + "-" * 108)
    for r in rows:
        print(f"  {str(r['brand'])[:27]:<28} "
              f"{str(r['brand_category'] or '')[:31]:<32} "
              f"{str(r['vertical'] or ''):<10} "
              f"${r['g_revenue']:>10,.2f} "
              f"${r['ms_revenue']:>8,.2f} "
              f"${r['total_revenue']:>10,.2f}")

    # ---------------------------------------------------------------
    # 7. Keyword identity tagging coverage
    # ---------------------------------------------------------------
    print("\n[7] Keyword identity tagging coverage (this account):")
    rows = cursor.execute(f"""
        SELECT
            ki2.tag_method,
            ki2.vertical,
            COUNT(DISTINCT ki.id) AS instances
        FROM keyword_instances ki
        JOIN keyword_identities ki2 ON ki.identity_id = ki2.id
        JOIN accounts a             ON ki.account_id  = a.id
        WHERE a.platform_account_id IN ({id_placeholders})
        GROUP BY ki2.tag_method, ki2.vertical
        ORDER BY instances DESC
        LIMIT 20
    """, account_ids).fetchall()

    print(f"  {'Tag method':<20} {'Vertical':<18} {'Instances':>10}")
    print(f"  {'-'*20} {'-'*18} {'-'*10}")
    for r in rows:
        print(f"  {str(r['tag_method'] or 'untagged'):<20} "
              f"{str(r['vertical'] or 'none'):<18} "
              f"{r['instances']:>10,}")

    # ---------------------------------------------------------------
    # 8. CSV export
    # ---------------------------------------------------------------
    output_path = OUTPUT_DIR / f"tagging_diagnostic_{args.account}.csv"
    all_nonbrand = cursor.execute(f"""
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
            COALESCE(SUM(CASE WHEN a.platform = 'google'
                         THEN sqp.primary_conv_value END), 0) AS g_revenue,
            COALESCE(SUM(CASE WHEN a.platform = 'microsoft'
                         THEN sqp.primary_conv_value END), 0) AS ms_revenue,
            COALESCE(SUM(sqp.primary_conv_value), 0)          AS total_revenue,
            COALESCE(SUM(sqp.clicks), 0)                      AS clicks,
            COALESCE(SUM(sqp.cost_micros), 0) / 1e6           AS spend
        FROM search_query_performance sqp
        JOIN search_queries sq    ON sqp.query_id   = sq.id
        JOIN keyword_instances ki ON sqp.instance_id = ki.id
        JOIN accounts a           ON ki.account_id  = a.id
        WHERE sqp.device = 'ALL'
          AND sqp.report_date >= ?
          AND a.platform_account_id IN ({id_placeholders})
          AND (sq.modifier_brand IS NULL OR sq.modifier_brand = '')
        GROUP BY sq.id
        ORDER BY total_revenue DESC
    """, [start_str] + account_ids).fetchall()

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "query_text", "vertical", "brand_category", "modifier_brand",
            "modifier_geo", "modifier_price", "modifier_coverage",
            "tag_method", "tag_confidence", "first_seen_platform",
            "first_seen_date", "google_revenue", "ms_revenue",
            "total_revenue", "clicks", "spend",
        ])
        for r in all_nonbrand:
            writer.writerow([
                r["query_text"], r["vertical"], r["brand_category"],
                r["modifier_brand"], r["modifier_geo"], r["modifier_price"],
                r["modifier_coverage"], r["tag_method"], r["tag_confidence"],
                r["first_seen_platform"], r["first_seen_date"],
                round(r["g_revenue"], 2), round(r["ms_revenue"], 2),
                round(r["total_revenue"], 2), r["clicks"],
                round(r["spend"], 2),
            ])

    print(f"\n[8] Non-brand query CSV exported:")
    print(f"  {output_path}")
    print(f"  {len(all_nonbrand):,} rows")

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
