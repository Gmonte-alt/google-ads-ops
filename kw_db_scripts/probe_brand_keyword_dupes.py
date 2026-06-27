"""
probe_brand_keyword_dupes.py  — read-only: size the brand_registry_keywords
duplication before the de-dupe + schema-fix migration.
==============================================================================
Two DIFFERENT problems live in this table; they need opposite treatment:

  EXACT DUPLICATES — same (brand_id, normalized, match_type) repeated. Caused by
    the NULL-match_type UNIQUE defeat: every 07 re-run re-inserted the same
    NULL-match_type variants because SQLite treats each NULL as distinct, so
    ON CONFLICT never fired. SAFE to collapse (keep one, drop the rest).

  CROSS-BRAND COLLISIONS — the same (normalized, match_type) mapped to DIFFERENT
    brand_ids. NOT dedupable, and they BLOCK enforcing UNIQUE(normalized,
    match_type) (a term can't point at two brands). Each needs a decision.

This probe reports the DDL, totals, the duplication factor, top exact-dup
offenders (with brand names), the NULL-match_type share, and the full list of
cross-brand collisions — then gives a verdict on whether UNIQUE(normalized,
match_type) can be cleanly enforced after an exact-dup collapse.

READ-ONLY. Writes nothing.

    python probe_brand_keyword_dupes.py
"""

import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")


def main():
    if not DB.exists():
        sys.exit(f"DB not found: {DB}")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    print("=" * 78)
    print("  brand_registry_keywords — duplication probe (read-only)")
    print("=" * 78)

    # ── DDL ───────────────────────────────────────────────────────────────────
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='brand_registry_keywords'").fetchone()
    print("\n  DDL:")
    print("    " + (ddl["sql"] if ddl else "(table not found)").replace("\n", "\n    "))

    # ── Totals ────────────────────────────────────────────────────────────────
    total = conn.execute("SELECT COUNT(*) c FROM brand_registry_keywords").fetchone()["c"]
    nulls = conn.execute(
        "SELECT COUNT(*) c FROM brand_registry_keywords WHERE match_type IS NULL").fetchone()["c"]
    # distinct logical keys treating NULL as '' (post-fix view)
    distinct_keys = conn.execute("""
        SELECT COUNT(*) c FROM (
            SELECT brand_id, normalized, COALESCE(match_type,'') mt
            FROM brand_registry_keywords
            GROUP BY brand_id, normalized, mt)""").fetchone()["c"]
    print(f"\n  Total rows                         : {total:,}")
    print(f"  NULL match_type rows               : {nulls:,}  ({100*nulls/total:.1f}%)" if total else "")
    print(f"  Distinct (brand_id,normalized,mt)  : {distinct_keys:,}")
    print(f"  => EXACT-DUP excess rows to remove  : {total - distinct_keys:,}")
    if distinct_keys:
        print(f"     (table would shrink {100*(total-distinct_keys)/total:.1f}% on exact-dedup)")

    # ── Top exact-dup offenders ───────────────────────────────────────────────
    print("\n  Top exact-dup groups (same brand_id+normalized+match_type, count>1):")
    rows = conn.execute("""
        SELECT k.brand_id, b.brand_name, k.normalized,
               COALESCE(k.match_type,'(null)') mt, COUNT(*) c
        FROM brand_registry_keywords k
        LEFT JOIN brand_registry b ON b.id = k.brand_id
        GROUP BY k.brand_id, k.normalized, COALESCE(k.match_type,'')
        HAVING c > 1
        ORDER BY c DESC, b.brand_name
        LIMIT 20""").fetchall()
    if rows:
        for r in rows:
            print(f"    x{r['c']:<3} {(r['brand_name'] or '?'):<34} '{r['normalized']}' [{r['mt']}]")
    else:
        print("    none")

    dup_groups = conn.execute("""
        SELECT COUNT(*) c FROM (
            SELECT 1 FROM brand_registry_keywords
            GROUP BY brand_id, normalized, COALESCE(match_type,'')
            HAVING COUNT(*) > 1)""").fetchone()["c"]
    print(f"  exact-dup groups (any count>1)     : {dup_groups:,}")

    # ── Cross-brand collisions (the blocker) ──────────────────────────────────
    print("\n  Cross-brand collisions (same normalized+match_type, DIFFERENT brands):")
    collisions = conn.execute("""
        SELECT normalized, COALESCE(match_type,'') mt, COUNT(DISTINCT brand_id) nb
        FROM brand_registry_keywords
        GROUP BY normalized, COALESCE(match_type,'')
        HAVING nb > 1
        ORDER BY nb DESC, normalized
        LIMIT 40""").fetchall()
    n_collisions = conn.execute("""
        SELECT COUNT(*) c FROM (
            SELECT 1 FROM brand_registry_keywords
            GROUP BY normalized, COALESCE(match_type,'')
            HAVING COUNT(DISTINCT brand_id) > 1)""").fetchone()["c"]
    if collisions:
        for r in collisions:
            brs = conn.execute("""
                SELECT DISTINCT b.brand_name FROM brand_registry_keywords k
                JOIN brand_registry b ON b.id = k.brand_id
                WHERE k.normalized = ? AND COALESCE(k.match_type,'') = ?""",
                (r["normalized"], r["mt"])).fetchall()
            names = ", ".join(x["brand_name"] for x in brs)
            print(f"    '{r['normalized']}' [{r['mt'] or 'null'}] → {r['nb']} brands: {names}")
        if n_collisions > len(collisions):
            print(f"    ... and {n_collisions - len(collisions)} more")
    else:
        print("    none")
    print(f"  total cross-brand collisions       : {n_collisions:,}")

    # ── Verdict ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 78)
    print("  VERDICT")
    print("=" * 78)
    print(f"  Exact-dup excess rows to collapse  : {total - distinct_keys:,}")
    print(f"  Cross-brand collisions to resolve  : {n_collisions:,}")
    if n_collisions == 0:
        print("  => Clean path: collapse exact dups, then rebuild with")
        print("     match_type NOT NULL DEFAULT '' + UNIQUE(normalized, match_type).")
    else:
        print("  => Collisions present: these BLOCK a global UNIQUE(normalized,")
        print("     match_type). Either resolve each (reassign/drop the wrong brand)")
        print("     before enforcing it, OR use UNIQUE(brand_id, normalized, match_type)")
        print("     if a term legitimately maps to multiple brands. Decide per collision.")
    conn.close()


if __name__ == "__main__":
    main()
