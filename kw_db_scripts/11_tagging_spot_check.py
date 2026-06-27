"""
11_tagging_spot_check.py
------------------------
Standalone diagnostic and sample-export script for the tagging system.
Complements 10_tagging_diagnostic.py (which is account-scoped and revenue-
driven). This script works at the full search_queries / keyword_identities
table level and does NOT require revenue data -- useful immediately after
a re-tag run before performance data is refreshed.

What it produces (all written to kw_db_scripts/output/spot_check/):
  [A] HEALTH SUMMARY     -- printed to terminal + spot_check_summary.txt
  [B] CROSS INFLATION    -- cross_vertical_sample.csv
                            Sample of cross-tagged queries that contain a
                            vertical keyword -- likely infer_vertical misses
  [C] FAILED RELEVANCE   -- failed_relevance_sample.csv
                            Queries that got vertical=NULL/none -- completely
                            failed the relevance gate
  [D] UNCLASSIFIED       -- unclassified_queries_sample.csv
                            Passed relevance gate but no vertical assigned
  [E] UNCLASSIFIED KW    -- unclassified_keywords.csv
                            Active keyword identities with tag_method=unclassified
  [F] TAG SAMPLES        -- tag_samples_<tag>.csv  (one file per tag combo)
                            Random samples of what each tag combination looks
                            like -- the main "show me what was tagged" output
  [G] NEW MODIFIERS      -- new_modifiers_sample.csv
                            Sample of rows where modifier_home_type,
                            intent_type, or intent_action was populated

Usage:
    conda activate paid-search
    python kw_db_scripts/11_tagging_spot_check.py

    # Limit sample sizes (default 200 per bucket)
    python kw_db_scripts/11_tagging_spot_check.py --sample 100

    # Only run specific sections (comma-separated: A,B,C,D,E,F,G)
    python kw_db_scripts/11_tagging_spot_check.py --sections B,C,D

    # Target keyword_identities instead of search_queries for tag samples
    python kw_db_scripts/11_tagging_spot_check.py --table keywords
"""

import sqlite3
import csv
import argparse
import sys
import os
from pathlib import Path
from datetime import date
from collections import defaultdict

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PATH    = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
OUTPUT_DIR = Path(r"C:\Projects\google-ads-ops\kw_db_scripts\output\spot_check")

# Vertical keywords used to detect cross-inflation (mirrors VERTICAL_SIGNALS
# in the tagger -- abbreviated here for SQL LIKE matching)
VERTICAL_KEYWORDS = {
    'home':     ['home insurance', 'homeowners', 'homeowner', 'house insurance',
                 'property insurance', 'dwelling insurance'],
    'auto':     ['auto insurance', 'car insurance', 'vehicle insurance',
                 'motorcycle insurance'],
    'medicare': ['medicare', 'medigap', 'medicare supplement',
                 'medicare advantage'],
    'life':     ['life insurance', 'term life', 'whole life', 'final expense',
                 'burial insurance'],
    'renters':  ['renters insurance', 'renter insurance', 'apartment insurance'],
}


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Tagging spot-check and sample export script."
    )
    parser.add_argument(
        "--sample", type=int, default=200, metavar="N",
        help="Max rows per sample bucket. Default: 200"
    )
    parser.add_argument(
        "--sections", type=str, default="A,B,C,D,E,F,G",
        help="Comma-separated sections to run. Default: all (A,B,C,D,E,F,G)"
    )
    parser.add_argument(
        "--table", choices=["queries", "keywords", "both"], default="both",
        help="Which table to use for tag samples (section F). Default: both"
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Database connection
# ---------------------------------------------------------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def write_csv(path: Path, rows, fieldnames: list):
    """Write a list of sqlite3.Row or dict objects to a CSV file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(row))
    return len(rows)


def write_txt(path: Path, lines: list):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def sep(title="", width=65):
    if title:
        print(f"\n{'=' * width}")
        print(f"  {title}")
        print(f"{'=' * width}")
    else:
        print("=" * width)


# ---------------------------------------------------------------------------
# Section A: Health summary
# ---------------------------------------------------------------------------

def section_a(cursor, sample_n: int) -> list[str]:
    """Prints and returns lines for the terminal health summary."""
    sep("A  HEALTH SUMMARY")
    lines = [
        "11_tagging_spot_check.py",
        f"Run date : {date.today()}",
        "=" * 65,
        "",
    ]

    # --- search_queries overview ---
    total_sq = cursor.execute(
        "SELECT COUNT(*) FROM search_queries"
    ).fetchone()[0]

    method_rows = cursor.execute("""
        SELECT
            COALESCE(tag_method, 'NULL') AS tag_method,
            COUNT(*) AS n,
            ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM search_queries), 1) AS pct
        FROM search_queries
        GROUP BY tag_method
        ORDER BY n DESC
    """).fetchall()

    vertical_rows = cursor.execute("""
        SELECT
            COALESCE(vertical, 'NULL') AS vertical,
            COUNT(*) AS n,
            ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM search_queries), 1) AS pct
        FROM search_queries
        GROUP BY vertical
        ORDER BY n DESC
    """).fetchall()

    # New modifier coverage
    mod_rows = cursor.execute("""
        SELECT
            SUM(CASE WHEN modifier_home_type IS NOT NULL THEN 1 ELSE 0 END)  AS home_type,
            SUM(CASE WHEN intent_type IS NOT NULL THEN 1 ELSE 0 END)         AS intent_type,
            SUM(CASE WHEN intent_action IS NOT NULL THEN 1 ELSE 0 END)       AS intent_action,
            SUM(CASE WHEN modifier_bundle = 1 THEN 1 ELSE 0 END)             AS bundle,
            SUM(CASE WHEN modifier_price  = 1 THEN 1 ELSE 0 END)             AS price,
            SUM(CASE WHEN modifier_geo IS NOT NULL THEN 1 ELSE 0 END)        AS geo,
            SUM(CASE WHEN modifier_coverage IS NOT NULL THEN 1 ELSE 0 END)   AS coverage,
            SUM(CASE WHEN modifier_near_me = 1 THEN 1 ELSE 0 END)            AS near_me
        FROM search_queries
    """).fetchone()

    print(f"\n  search_queries total: {total_sq:,}")
    print(f"\n  Tag method breakdown:")
    print(f"    {'Method':<22} {'Count':>9} {'Pct':>7}")
    print(f"    {'-'*22} {'-'*9} {'-'*7}")
    for r in method_rows:
        print(f"    {r['tag_method']:<22} {r['n']:>9,} {r['pct']:>6.1f}%")
        lines.append(f"  sq method  {r['tag_method']:<22} {r['n']:>9,}  {r['pct']:>5.1f}%")

    print(f"\n  Vertical breakdown:")
    print(f"    {'Vertical':<22} {'Count':>9} {'Pct':>7}")
    print(f"    {'-'*22} {'-'*9} {'-'*7}")
    for r in vertical_rows:
        print(f"    {r['vertical']:<22} {r['n']:>9,} {r['pct']:>6.1f}%")
        lines.append(f"  sq vertical {r['vertical']:<22} {r['n']:>9,}  {r['pct']:>5.1f}%")

    print(f"\n  New modifier coverage (search_queries):")
    mods = [
        ("modifier_home_type",  mod_rows["home_type"]),
        ("intent_type",         mod_rows["intent_type"]),
        ("intent_action",       mod_rows["intent_action"]),
        ("modifier_bundle",     mod_rows["bundle"]),
        ("modifier_price",      mod_rows["price"]),
        ("modifier_geo",        mod_rows["geo"]),
        ("modifier_coverage",   mod_rows["coverage"]),
        ("modifier_near_me",    mod_rows["near_me"]),
    ]
    for name, count in mods:
        pct = count / total_sq * 100 if total_sq else 0
        print(f"    {name:<25} {count:>9,}  ({pct:.1f}%)")
        lines.append(f"  sq modifier {name:<25} {count:>9,}  {pct:.1f}%")

    # Intent type breakdown
    intent_rows = cursor.execute("""
        SELECT
            COALESCE(intent_type, 'not set') AS intent_type,
            COUNT(*) AS n
        FROM search_queries
        GROUP BY intent_type
        ORDER BY n DESC
    """).fetchall()
    print(f"\n  intent_type breakdown:")
    for r in intent_rows:
        pct = r['n'] / total_sq * 100 if total_sq else 0
        print(f"    {r['intent_type']:<20} {r['n']:>9,}  ({pct:.1f}%)")

    # Intent action breakdown
    action_rows = cursor.execute("""
        SELECT
            COALESCE(intent_action, 'not set') AS intent_action,
            COUNT(*) AS n
        FROM search_queries
        GROUP BY intent_action
        ORDER BY n DESC
    """).fetchall()
    print(f"\n  intent_action breakdown:")
    for r in action_rows:
        pct = r['n'] / total_sq * 100 if total_sq else 0
        print(f"    {r['intent_action']:<20} {r['n']:>9,}  ({pct:.1f}%)")

    # modifier_home_type breakdown
    ht_rows = cursor.execute("""
        SELECT modifier_home_type, COUNT(*) AS n
        FROM search_queries
        WHERE modifier_home_type IS NOT NULL
        GROUP BY modifier_home_type
        ORDER BY n DESC
    """).fetchall()
    if ht_rows:
        print(f"\n  modifier_home_type breakdown:")
        for r in ht_rows:
            print(f"    {r['modifier_home_type']:<25} {r['n']:>9,}")

    # --- keyword_identities overview ---
    total_ki = cursor.execute(
        "SELECT COUNT(*) FROM keyword_identities"
    ).fetchone()[0]
    ki_method_rows = cursor.execute("""
        SELECT
            COALESCE(tag_method, 'NULL') AS tag_method,
            COUNT(*) AS n,
            ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM keyword_identities), 1) AS pct
        FROM keyword_identities
        GROUP BY tag_method
        ORDER BY n DESC
    """).fetchall()
    ki_new_mods = cursor.execute("""
        SELECT
            SUM(CASE WHEN modifier_home_type IS NOT NULL THEN 1 ELSE 0 END) AS home_type,
            SUM(CASE WHEN intent_type IS NOT NULL THEN 1 ELSE 0 END)        AS intent_type,
            SUM(CASE WHEN intent_action IS NOT NULL THEN 1 ELSE 0 END)      AS intent_action
        FROM keyword_identities
    """).fetchone()

    print(f"\n  keyword_identities total: {total_ki:,}")
    print(f"\n  Tag method breakdown:")
    for r in ki_method_rows:
        print(f"    {r['tag_method']:<22} {r['n']:>9,} {r['pct']:>6.1f}%")
    print(f"\n  New modifier coverage (keyword_identities):")
    for name, count in [
        ("modifier_home_type", ki_new_mods["home_type"]),
        ("intent_type",        ki_new_mods["intent_type"]),
        ("intent_action",      ki_new_mods["intent_action"]),
    ]:
        pct = count / total_ki * 100 if total_ki else 0
        print(f"    {name:<25} {count:>9,}  ({pct:.1f}%)")

    lines.append("")
    lines.append(f"keyword_identities total: {total_ki:,}")
    return lines


# ---------------------------------------------------------------------------
# Section B: Cross-vertical inflation diagnostic
# ---------------------------------------------------------------------------

def section_b(cursor, sample_n: int):
    sep("B  CROSS-VERTICAL INFLATION")
    print("  Checking cross-tagged queries that contain a vertical keyword...")
    print("  These are candidates for infer_vertical_from_brand fix.\n")

    # Build LIKE conditions for each vertical
    results = []
    for vertical, kw_list in VERTICAL_KEYWORDS.items():
        for kw in kw_list:
            rows = cursor.execute(f"""
                SELECT
                    query_text,
                    modifier_brand,
                    tag_method,
                    '{vertical}' AS expected_vertical,
                    '{kw}'       AS matched_keyword
                FROM search_queries
                WHERE vertical = 'cross'
                  AND (query_text LIKE ? OR normalized_text LIKE ?)
                ORDER BY RANDOM()
                LIMIT ?
            """, (f"%{kw}%", f"%{kw}%", max(5, sample_n // 20))).fetchall()
            results.extend(rows)

    # Deduplicate by query_text, keep first occurrence
    seen = set()
    deduped = []
    for r in results:
        key = r["query_text"]
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    deduped = deduped[:sample_n]

    print(f"  Found {len(deduped)} cross-tagged queries with vertical keywords")
    print(f"  (sample capped at {sample_n})\n")

    if deduped:
        print(f"  {'Expected Vert':<15} {'Matched KW':<30} {'Brand':<20} Query")
        print(f"  {'-'*15} {'-'*30} {'-'*20} {'-'*40}")
        for r in deduped[:25]:
            print(f"  {r['expected_vertical']:<15} "
                  f"{r['matched_keyword'][:29]:<30} "
                  f"{str(r['modifier_brand'] or '')[:19]:<20} "
                  f"'{str(r['query_text'])[:50]}'")
        if len(deduped) > 25:
            print(f"  ... {len(deduped) - 25} more in CSV")

    out_path = OUTPUT_DIR / "cross_vertical_sample.csv"
    fieldnames = ["query_text", "modifier_brand", "tag_method",
                  "expected_vertical", "matched_keyword"]
    n = write_csv(out_path, deduped, fieldnames)
    print(f"\n  Exported: {out_path}  ({n} rows)")


# ---------------------------------------------------------------------------
# Section C: Failed relevance gate (vertical = NULL / none)
# ---------------------------------------------------------------------------

def section_c(cursor, sample_n: int):
    sep("C  FAILED RELEVANCE GATE  (vertical = NULL)")
    print("  These queries got no vertical at all -- they failed the insurance")
    print("  relevance gate. May be noise, or may indicate missing signals.\n")

    rows = cursor.execute(f"""
        SELECT
            query_text,
            normalized_text,
            tag_method,
            tag_confidence,
            modifier_brand,
            modifier_geo
        FROM search_queries
        WHERE vertical IS NULL OR vertical = 'none'
        ORDER BY RANDOM()
        LIMIT ?
    """, (sample_n,)).fetchall()

    count_total = cursor.execute(
        "SELECT COUNT(*) FROM search_queries WHERE vertical IS NULL OR vertical = 'none'"
    ).fetchone()[0]

    print(f"  Total in table : {count_total:,}")
    print(f"  Sample size    : {len(rows)}\n")

    if rows:
        print(f"  {'Tag method':<18} {'Brand':<20} Query")
        print(f"  {'-'*18} {'-'*20} {'-'*40}")
        for r in rows[:25]:
            print(f"  {str(r['tag_method'] or ''):<18} "
                  f"{str(r['modifier_brand'] or ''):<20} "
                  f"'{str(r['query_text'])[:60]}'")
        if len(rows) > 25:
            print(f"  ... {len(rows) - 25} more in CSV")

    out_path = OUTPUT_DIR / "failed_relevance_sample.csv"
    fieldnames = ["query_text", "normalized_text", "tag_method",
                  "tag_confidence", "modifier_brand", "modifier_geo"]
    n = write_csv(out_path, rows, fieldnames)
    print(f"\n  Exported: {out_path}  ({n} rows)")


# ---------------------------------------------------------------------------
# Section D: Unclassified queries (passed gate but no vertical)
# ---------------------------------------------------------------------------

def section_d(cursor, sample_n: int):
    sep("D  UNCLASSIFIED QUERIES  (passed relevance, no vertical)")
    print("  These passed the insurance relevance gate but no vertical was")
    print("  assigned. Prime candidates for the API layer or signal expansion.\n")

    rows = cursor.execute(f"""
        SELECT
            query_text,
            normalized_text,
            tag_method,
            tag_confidence,
            modifier_brand,
            modifier_geo,
            modifier_coverage,
            modifier_home_type,
            intent_type,
            intent_action
        FROM search_queries
        WHERE vertical = 'unclassified'
        ORDER BY RANDOM()
        LIMIT ?
    """, (sample_n,)).fetchall()

    count_total = cursor.execute(
        "SELECT COUNT(*) FROM search_queries WHERE vertical = 'unclassified'"
    ).fetchone()[0]

    print(f"  Total in table : {count_total:,}")
    print(f"  Sample size    : {len(rows)}\n")

    if rows:
        print(f"  {'Tag method':<15} {'Intent type':<14} Query")
        print(f"  {'-'*15} {'-'*14} {'-'*50}")
        for r in rows[:25]:
            print(f"  {str(r['tag_method'] or ''):<15} "
                  f"{str(r['intent_type'] or ''):<14} "
                  f"'{str(r['query_text'])[:60]}'")
        if len(rows) > 25:
            print(f"  ... {len(rows) - 25} more in CSV")

    out_path = OUTPUT_DIR / "unclassified_queries_sample.csv"
    fieldnames = ["query_text", "normalized_text", "tag_method", "tag_confidence",
                  "modifier_brand", "modifier_geo", "modifier_coverage",
                  "modifier_home_type", "intent_type", "intent_action"]
    n = write_csv(out_path, rows, fieldnames)
    print(f"\n  Exported: {out_path}  ({n} rows)")


# ---------------------------------------------------------------------------
# Section E: Unclassified keyword identities
# ---------------------------------------------------------------------------

def section_e(cursor, sample_n: int):
    sep("E  UNCLASSIFIED KEYWORD IDENTITIES")
    print("  Active keyword identities that the tagger couldn't classify.")
    print("  These are keywords you're bidding on with no tag.\n")

    rows = cursor.execute(f"""
        SELECT
            normalized_text,
            match_type,
            tag_method,
            vertical,
            modifier_brand
        FROM keyword_identities
        WHERE tag_method = 'unclassified'
        ORDER BY normalized_text
        LIMIT ?
    """, (sample_n,)).fetchall()

    count_total = cursor.execute(
        "SELECT COUNT(*) FROM keyword_identities WHERE tag_method = 'unclassified'"
    ).fetchone()[0]

    print(f"  Total unclassified : {count_total:,}")
    print(f"  Sample size        : {len(rows)}\n")

    if rows:
        print(f"  {'Match type':<12} Keyword text")
        print(f"  {'-'*12} {'-'*55}")
        for r in rows[:40]:
            print(f"  {str(r['match_type'] or ''):<12} '{str(r['normalized_text'])[:60]}'")
        if len(rows) > 40:
            print(f"  ... {len(rows) - 40} more in CSV")

    out_path = OUTPUT_DIR / "unclassified_keywords.csv"
    fieldnames = ["normalized_text", "match_type", "tag_method",
                  "vertical", "modifier_brand"]
    n = write_csv(out_path, rows, fieldnames)
    print(f"\n  Exported: {out_path}  ({n} rows)")


# ---------------------------------------------------------------------------
# Section F: Tag samples -- show what each tag combination looks like
# ---------------------------------------------------------------------------

def section_f_queries(cursor, sample_n: int):
    """Samples of search_queries grouped by key tag combinations."""
    sep("F  TAG SAMPLES  -- search_queries")
    print("  Exports random samples of queries for each tag combination so")
    print("  you can see what the tagger is actually catching.\n")

    # --- F1: By vertical + tag_method ---
    combos = cursor.execute("""
        SELECT
            COALESCE(vertical, 'NULL')   AS vertical,
            COALESCE(tag_method, 'NULL') AS tag_method,
            COUNT(*) AS n
        FROM search_queries
        GROUP BY vertical, tag_method
        ORDER BY n DESC
    """).fetchall()

    all_rows = []
    per_bucket = max(10, sample_n // max(len(combos), 1))

    for combo in combos:
        rows = cursor.execute(f"""
            SELECT
                query_text,
                vertical,
                tag_method,
                tag_confidence,
                modifier_brand,
                modifier_geo,
                modifier_price,
                modifier_coverage,
                modifier_home_type,
                modifier_bundle,
                modifier_near_me,
                intent_type,
                intent_action,
                brand_category
            FROM search_queries
            WHERE COALESCE(vertical, 'NULL')   = ?
              AND COALESCE(tag_method, 'NULL') = ?
            ORDER BY RANDOM()
            LIMIT ?
        """, (combo["vertical"], combo["tag_method"], per_bucket)).fetchall()
        all_rows.extend(rows)

    out_path = OUTPUT_DIR / "tag_samples_queries_by_vertical.csv"
    fieldnames = [
        "query_text", "vertical", "tag_method", "tag_confidence",
        "modifier_brand", "modifier_geo", "modifier_price", "modifier_coverage",
        "modifier_home_type", "modifier_bundle", "modifier_near_me",
        "intent_type", "intent_action", "brand_category",
    ]
    n = write_csv(out_path, all_rows, fieldnames)
    print(f"  [F1] By vertical + tag_method:  {out_path.name}  ({n} rows)")

    # --- F2: By intent_type + intent_action ---
    intent_combos = cursor.execute("""
        SELECT
            COALESCE(intent_type,   'not set') AS intent_type,
            COALESCE(intent_action, 'not set') AS intent_action,
            COUNT(*) AS n
        FROM search_queries
        GROUP BY intent_type, intent_action
        ORDER BY n DESC
    """).fetchall()

    intent_rows = []
    per_bucket_i = max(15, sample_n // max(len(intent_combos), 1))
    for combo in intent_combos:
        it_val = None if combo["intent_type"]   == "not set" else combo["intent_type"]
        ia_val = None if combo["intent_action"] == "not set" else combo["intent_action"]

        rows = cursor.execute(f"""
            SELECT
                query_text,
                vertical,
                tag_method,
                intent_type,
                intent_action,
                modifier_brand,
                modifier_geo,
                modifier_price
            FROM search_queries
            WHERE (intent_type IS ?) AND (intent_action IS ?)
            ORDER BY RANDOM()
            LIMIT ?
        """, (it_val, ia_val, per_bucket_i)).fetchall()
        intent_rows.extend(rows)

    out_path2 = OUTPUT_DIR / "tag_samples_queries_by_intent.csv"
    fieldnames2 = ["query_text", "vertical", "tag_method",
                   "intent_type", "intent_action",
                   "modifier_brand", "modifier_geo", "modifier_price"]
    n2 = write_csv(out_path2, intent_rows, fieldnames2)
    print(f"  [F2] By intent_type + action:   {out_path2.name}  ({n2} rows)")

    # --- F3: By modifier_home_type ---
    ht_combos = cursor.execute("""
        SELECT modifier_home_type, COUNT(*) AS n
        FROM search_queries
        WHERE modifier_home_type IS NOT NULL
        GROUP BY modifier_home_type
        ORDER BY n DESC
    """).fetchall()

    ht_rows = []
    for combo in ht_combos:
        rows = cursor.execute(f"""
            SELECT
                query_text,
                vertical,
                modifier_home_type,
                modifier_coverage,
                modifier_geo,
                intent_type,
                intent_action,
                modifier_brand,
                tag_method
            FROM search_queries
            WHERE modifier_home_type = ?
            ORDER BY RANDOM()
            LIMIT ?
        """, (combo["modifier_home_type"], max(20, sample_n // 10))).fetchall()
        ht_rows.extend(rows)

    out_path3 = OUTPUT_DIR / "tag_samples_queries_home_type.csv"
    fieldnames3 = ["query_text", "vertical", "modifier_home_type", "modifier_coverage",
                   "modifier_geo", "intent_type", "intent_action",
                   "modifier_brand", "tag_method"]
    n3 = write_csv(out_path3, ht_rows, fieldnames3)
    print(f"  [F3] By modifier_home_type:     {out_path3.name}  ({n3} rows)")

    # --- F4: High-value combos: commercial + compare/quote + geo ---
    hv_rows = cursor.execute(f"""
        SELECT
            query_text,
            vertical,
            intent_type,
            intent_action,
            modifier_geo,
            modifier_brand,
            modifier_price,
            modifier_bundle,
            tag_method
        FROM search_queries
        WHERE intent_type = 'commercial'
          AND intent_action IN ('compare', 'quote')
          AND modifier_geo IS NOT NULL
        ORDER BY RANDOM()
        LIMIT ?
    """, (sample_n,)).fetchall()

    out_path4 = OUTPUT_DIR / "tag_samples_queries_high_value.csv"
    fieldnames4 = ["query_text", "vertical", "intent_type", "intent_action",
                   "modifier_geo", "modifier_brand", "modifier_price",
                   "modifier_bundle", "tag_method"]
    n4 = write_csv(out_path4, hv_rows, fieldnames4)
    print(f"  [F4] Commercial+compare/quote+geo (high-value): "
          f"{out_path4.name}  ({n4} rows)")

    # --- F5: Brand eligibility queries (informational + answer + brand) ---
    be_rows = cursor.execute(f"""
        SELECT
            query_text,
            vertical,
            intent_type,
            intent_action,
            modifier_brand,
            brand_category,
            modifier_geo,
            tag_method
        FROM search_queries
        WHERE intent_type = 'informational'
          AND intent_action = 'answer'
          AND modifier_brand IS NOT NULL
        ORDER BY RANDOM()
        LIMIT ?
    """, (sample_n,)).fetchall()

    out_path5 = OUTPUT_DIR / "tag_samples_queries_brand_eligibility.csv"
    fieldnames5 = ["query_text", "vertical", "intent_type", "intent_action",
                   "modifier_brand", "brand_category", "modifier_geo", "tag_method"]
    n5 = write_csv(out_path5, be_rows, fieldnames5)
    print(f"  [F5] Informational+answer+brand (brand eligibility): "
          f"{out_path5.name}  ({n5} rows)")

    # --- F6: Bundle queries ---
    bundle_rows = cursor.execute(f"""
        SELECT
            query_text,
            vertical,
            modifier_bundle,
            modifier_brand,
            intent_type,
            intent_action,
            modifier_geo,
            tag_method
        FROM search_queries
        WHERE modifier_bundle = 1
        ORDER BY RANDOM()
        LIMIT ?
    """, (sample_n,)).fetchall()

    out_path6 = OUTPUT_DIR / "tag_samples_queries_bundle.csv"
    fieldnames6 = ["query_text", "vertical", "modifier_bundle", "modifier_brand",
                   "intent_type", "intent_action", "modifier_geo", "tag_method"]
    n6 = write_csv(out_path6, bundle_rows, fieldnames6)
    print(f"  [F6] Bundle queries:            {out_path6.name}  ({n6} rows)")


def section_f_keywords(cursor, sample_n: int):
    """Samples of keyword_identities grouped by key tag combinations."""
    sep("F  TAG SAMPLES  -- keyword_identities")

    combos = cursor.execute("""
        SELECT
            COALESCE(vertical, 'NULL')   AS vertical,
            COALESCE(tag_method, 'NULL') AS tag_method,
            COUNT(*) AS n
        FROM keyword_identities
        GROUP BY vertical, tag_method
        ORDER BY n DESC
    """).fetchall()

    all_rows = []
    per_bucket = max(10, sample_n // max(len(combos), 1))

    for combo in combos:
        rows = cursor.execute(f"""
            SELECT
                normalized_text,
                match_type,
                vertical,
                tag_method,
                modifier_brand,
                modifier_geo,
                modifier_price,
                modifier_coverage,
                modifier_home_type,
                modifier_bundle,
                intent_type,
                intent_action,
                brand_category
            FROM keyword_identities
            WHERE COALESCE(vertical, 'NULL')   = ?
              AND COALESCE(tag_method, 'NULL') = ?
            ORDER BY RANDOM()
            LIMIT ?
        """, (combo["vertical"], combo["tag_method"], per_bucket)).fetchall()
        all_rows.extend(rows)

    out_path = OUTPUT_DIR / "tag_samples_keywords_by_vertical.csv"
    fieldnames = [
        "normalized_text", "match_type", "vertical", "tag_method",
        "modifier_brand", "modifier_geo", "modifier_price", "modifier_coverage",
        "modifier_home_type", "modifier_bundle", "intent_type", "intent_action",
        "brand_category",
    ]
    n = write_csv(out_path, all_rows, fieldnames)
    print(f"  [F-kw] By vertical + tag_method: {out_path.name}  ({n} rows)")

    # Intent breakdown for keywords
    intent_rows = cursor.execute(f"""
        SELECT
            normalized_text,
            match_type,
            vertical,
            intent_type,
            intent_action,
            modifier_brand,
            modifier_geo,
            tag_method
        FROM keyword_identities
        WHERE intent_type IS NOT NULL OR intent_action IS NOT NULL
        ORDER BY RANDOM()
        LIMIT ?
    """, (sample_n,)).fetchall()

    out_path_i = OUTPUT_DIR / "tag_samples_keywords_intent.csv"
    fieldnames_i = ["normalized_text", "match_type", "vertical",
                    "intent_type", "intent_action",
                    "modifier_brand", "modifier_geo", "tag_method"]
    ni = write_csv(out_path_i, intent_rows, fieldnames_i)
    print(f"  [F-kw] Intent-tagged keywords:   {out_path_i.name}  ({ni} rows)")


# ---------------------------------------------------------------------------
# Section G: New modifiers sample
# ---------------------------------------------------------------------------

def section_g(cursor, sample_n: int):
    sep("G  NEW MODIFIERS SAMPLE")
    print("  Sample of rows where the new columns (modifier_home_type,")
    print("  intent_type, intent_action) were populated.\n")

    # modifier_home_type
    ht_rows = cursor.execute(f"""
        SELECT
            query_text, vertical, modifier_home_type, modifier_coverage,
            intent_type, intent_action, modifier_brand, tag_method
        FROM search_queries
        WHERE modifier_home_type IS NOT NULL
        ORDER BY modifier_home_type, RANDOM()
        LIMIT ?
    """, (sample_n,)).fetchall()

    # intent_type = commercial
    com_rows = cursor.execute(f"""
        SELECT
            query_text, vertical, intent_type, intent_action,
            modifier_brand, modifier_geo, modifier_price, tag_method
        FROM search_queries
        WHERE intent_type = 'commercial'
        ORDER BY RANDOM()
        LIMIT ?
    """, (sample_n // 2,)).fetchall()

    # intent_type = informational
    info_rows = cursor.execute(f"""
        SELECT
            query_text, vertical, intent_type, intent_action,
            modifier_brand, modifier_geo, modifier_price, tag_method
        FROM search_queries
        WHERE intent_type = 'informational'
        ORDER BY RANDOM()
        LIMIT ?
    """, (sample_n // 2,)).fetchall()

    print(f"  modifier_home_type populated : {len(ht_rows):>6,} sample rows")
    print(f"  intent_type = commercial     : {len(com_rows):>6,} sample rows")
    print(f"  intent_type = informational  : {len(info_rows):>6,} sample rows")

    # Print a few terminal examples
    if ht_rows:
        print(f"\n  modifier_home_type examples:")
        for r in ht_rows[:8]:
            print(f"    [{r['modifier_home_type']:<18}] '{str(r['query_text'])[:60]}'")

    if com_rows:
        print(f"\n  commercial intent examples:")
        for r in com_rows[:8]:
            print(f"    [{str(r['intent_action'] or '?'):<16}] '{str(r['query_text'])[:60]}'")

    if info_rows:
        print(f"\n  informational intent examples:")
        for r in info_rows[:8]:
            print(f"    [{str(r['intent_action'] or '?'):<16}] '{str(r['query_text'])[:60]}'")

    # Write CSVs
    ht_path = OUTPUT_DIR / "new_modifiers_home_type.csv"
    ht_fields = ["query_text", "vertical", "modifier_home_type", "modifier_coverage",
                 "intent_type", "intent_action", "modifier_brand", "tag_method"]
    write_csv(ht_path, ht_rows, ht_fields)
    print(f"\n  Exported: {ht_path.name}  ({len(ht_rows)} rows)")

    intent_path = OUTPUT_DIR / "new_modifiers_intent.csv"
    intent_fields = ["query_text", "vertical", "intent_type", "intent_action",
                     "modifier_brand", "modifier_geo", "modifier_price", "tag_method"]
    combined_intent = list(com_rows) + list(info_rows)
    write_csv(intent_path, combined_intent, intent_fields)
    print(f"  Exported: {intent_path.name}  ({len(combined_intent)} rows)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args    = parse_args()
    sample_n = args.sample
    sections = {s.strip().upper() for s in args.sections.split(",")}
    run_table = args.table

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    sep()
    print("  11_tagging_spot_check.py")
    print(f"  Date     : {date.today()}")
    print(f"  Sections : {', '.join(sorted(sections))}")
    print(f"  Sample N : {sample_n:,} per bucket")
    print(f"  Output   : {OUTPUT_DIR}")
    sep()

    conn   = get_conn()
    cursor = conn.cursor()

    # Verify tables exist
    tables = {r[0] for r in cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    required = {"search_queries", "keyword_identities", "brand_registry"}
    missing  = required - tables
    if missing:
        print(f"\nERROR: Missing tables: {', '.join(missing)}")
        print("Run 07_seed_brand_registry.py, 08_tag_search_queries.py, "
              "and 09_tag_keyword_identities.py first.")
        sys.exit(1)

    # Verify new columns exist
    sq_cols = {r[1] for r in cursor.execute(
        "PRAGMA table_info(search_queries)"
    ).fetchall()}
    ki_cols = {r[1] for r in cursor.execute(
        "PRAGMA table_info(keyword_identities)"
    ).fetchall()}
    new_sq_cols = {"modifier_home_type", "intent_type", "intent_action"}
    new_ki_cols = {"modifier_home_type", "intent_type", "intent_action"}
    missing_sq = new_sq_cols - sq_cols
    missing_ki = new_ki_cols - ki_cols

    if missing_sq:
        print(f"\nWARNING: search_queries missing columns: {missing_sq}")
        print("Run 07_seed_brand_registry.py then re-run 08_tag_search_queries.py --all")
    if missing_ki:
        print(f"\nWARNING: keyword_identities missing columns: {missing_ki}")
        print("Re-run 09_tag_keyword_identities.py")

    summary_lines = []

    if "A" in sections:
        summary_lines = section_a(cursor, sample_n)
        txt_path = OUTPUT_DIR / "spot_check_summary.txt"
        write_txt(txt_path, summary_lines)
        print(f"\n  Summary saved: {txt_path}")

    if "B" in sections:
        section_b(cursor, sample_n)

    if "C" in sections:
        section_c(cursor, sample_n)

    if "D" in sections:
        section_d(cursor, sample_n)

    if "E" in sections:
        section_e(cursor, sample_n)

    if "F" in sections:
        if run_table in ("queries", "both"):
            section_f_queries(cursor, sample_n)
        if run_table in ("keywords", "both"):
            section_f_keywords(cursor, sample_n)

    if "G" in sections:
        section_g(cursor, sample_n)

    conn.close()

    sep()
    print("  DONE")
    print(f"  All exports written to: {OUTPUT_DIR}")
    print()
    print("  Files produced:")
    for f in sorted(OUTPUT_DIR.iterdir()):
        size_kb = f.stat().st_size / 1024
        print(f"    {f.name:<55} {size_kb:>8.1f} KB")
    sep()


if __name__ == "__main__":
    main()
