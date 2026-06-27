"""
diagnose_brand_tagging.py
--------------------------
Diagnose mis-tagged keyword_identities: rows where modifier_brand is NULL
or brand_category = '0', that contain a known brand name in normalized_text.

Run BEFORE re-tagging to see the scope of the problem, and AFTER to verify
the fix eliminated all mis-tags.

Usage:
    conda activate paid-search
    python kw_db_scripts/diagnose_brand_tagging.py
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def run(conn):
    cur = conn.cursor()

    # ------------------------------------------------------------------ #
    # 1. Overall mis-tag count
    # ------------------------------------------------------------------ #
    total = cur.execute("SELECT COUNT(*) FROM keyword_identities").fetchone()[0]
    mistag = cur.execute("""
        SELECT COUNT(*) FROM keyword_identities
        WHERE modifier_brand IS NULL
           OR brand_category = '0'
           OR CAST(brand_category AS TEXT) = '0'
    """).fetchone()[0]
    print(f"Total keyword_identities : {total:,}")
    print(f"Mis-tagged (no brand)    : {mistag:,}  ({mistag/total*100:.1f}%)")

    # ------------------------------------------------------------------ #
    # 2. Which mis-tagged rows contain a known brand name?
    # ------------------------------------------------------------------ #
    print("\n--- Mis-tagged rows that contain a known brand variant ---")
    rows = cur.execute("""
        SELECT ki.normalized_text,
               ki.match_type,
               ki.modifier_brand,
               ki.brand_category,
               ki.tag_method,
               brk.normalized   AS matched_variant,
               br.brand_name    AS expected_brand,
               br.brand_category AS expected_cat
        FROM keyword_identities ki
        JOIN brand_registry_keywords brk
          ON ki.normalized_text LIKE '%' || brk.normalized || '%'
        JOIN brand_registry br ON brk.brand_id = br.id
        WHERE (ki.modifier_brand IS NULL
               OR ki.brand_category = '0'
               OR CAST(ki.brand_category AS TEXT) = '0')
          AND br.normalized_name != 'unattributed_negative_keyword'
          AND LENGTH(brk.normalized) >= 3
        ORDER BY br.brand_name, ki.normalized_text
        LIMIT 200
    """).fetchall()

    if not rows:
        print("  ✓ None found — all brand keywords appear correctly tagged.")
    else:
        print(f"  {'Keyword':<45} {'MT':<7} {'Variant':<20} {'Expected Brand':<20} {'Expected Cat'}")
        print(f"  {'-'*45} {'-'*7} {'-'*20} {'-'*20} {'-'*25}")
        for r in rows:
            print(f"  {r['normalized_text']:<45} {r['match_type']:<7} "
                  f"{r['matched_variant']:<20} {r['expected_brand']:<20} "
                  f"{r['expected_cat'] or '(none)'}")

    # ------------------------------------------------------------------ #
    # 3. Confirm the brand variants exist in brand_registry_keywords
    # ------------------------------------------------------------------ #
    print("\n--- Brand variants in brand_registry_keywords for problem brands ---")
    problem_brands = ['geico', 'kin', 'lemonade', 'hippo']
    for brand in problem_brands:
        variants = cur.execute("""
            SELECT brk.normalized, brk.match_type, LENGTH(brk.normalized) AS len
            FROM brand_registry_keywords brk
            JOIN brand_registry br ON brk.brand_id = br.id
            WHERE br.normalized_name = ?
            ORDER BY len
        """, (brand,)).fetchall()
        if variants:
            print(f"\n  {brand}: {len(variants)} variant(s)")
            for v in variants[:10]:
                print(f"    len={v['len']:2d}  '{v['normalized']}'")
        else:
            print(f"\n  {brand}: NOT FOUND in brand_registry_keywords ← seed issue")

    # ------------------------------------------------------------------ #
    # 4. brand_category value distribution (catch integer '0' default)
    # ------------------------------------------------------------------ #
    print("\n--- brand_category value distribution ---")
    dist = cur.execute("""
        SELECT COALESCE(CAST(brand_category AS TEXT), 'NULL') AS val,
               COUNT(*) AS cnt
        FROM keyword_identities
        GROUP BY val
        ORDER BY cnt DESC
        LIMIT 20
    """).fetchall()
    for r in dist:
        print(f"  {r['val']:<40} {r['cnt']:>8,}")


def main():
    print("=" * 65)
    print("diagnose_brand_tagging.py")
    print("=" * 65)
    conn = get_conn()
    run(conn)
    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
