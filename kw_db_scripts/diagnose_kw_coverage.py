"""
diagnose_kw_coverage.py  — read-only: will the 2025 sqp backfill skip rows?
===========================================================================
A search-term row is only WRITTEN if its (account_id, ad_group_id, criterion_id)
resolves to a keyword_instances row. So "no skipped queries" depends on whether
keyword_instances still holds the keywords that served in the backfill window.

This script measures coverage WITHOUT pulling the API or writing anything:

  A. keyword_instances column list + status mix for Home (does it retain
     PAUSED/REMOVED, i.e. historical keywords?).
  B. keyword_performance_daily historical depth for Home, by year — kpd.instance_id
     is a FK into keyword_instances, so any keyword with a 2025 kpd row PROVABLY
     has an instance. Good 2025 kpd depth ⇒ low 2025 sqp skip risk.
  C. search_query_performance current coverage by month — shows the 2026 gap and
     the 2025 absence at a glance.

READ-ONLY. Safe to run anytime.

    python diagnose_kw_coverage.py
    python diagnose_kw_coverage.py --account-id 1
"""

import argparse
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")


def cols(conn, table):
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--account-id", type=int, default=1)
    args = p.parse_args()
    acct = args.account_id

    if not DB_PATH.exists():
        sys.exit(f"DB not found: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    print("=" * 72)
    print(f"  keyword coverage diagnostic (read-only)  account_id={acct}")
    print("=" * 72)

    ki_cols  = cols(conn, "keyword_instances")
    kpd_cols = cols(conn, "keyword_performance_daily")

    # ── A. keyword_instances ──────────────────────────────────────────────────
    print("\n  A. keyword_instances")
    print("  " + "-" * 68)
    print(f"    columns: {', '.join(ki_cols)}")
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM keyword_instances WHERE account_id = ?",
        (acct,)
    ).fetchone()["n"]
    print(f"    total instances (account {acct}): {total:,}")

    if "status" in ki_cols:
        print("    status mix:")
        for r in conn.execute("""
            SELECT status, COUNT(*) AS n
            FROM keyword_instances WHERE account_id = ?
            GROUP BY status ORDER BY n DESC
        """, (acct,)).fetchall():
            print(f"      {str(r['status']):<12} {r['n']:>8,}")
        print("    (retaining PAUSED/REMOVED ⇒ historical keywords are present)")
    else:
        print("    NOTE: no 'status' column — can't assess status retention here.")

    # ── B. keyword_performance_daily depth ────────────────────────────────────
    print("\n  B. keyword_performance_daily depth (FK proxy for instance coverage)")
    print("  " + "-" * 68)
    dev = " AND kpd.device = 'ALL'" if "device" in kpd_cols else ""
    rng = conn.execute(f"""
        SELECT MIN(report_date) AS lo, MAX(report_date) AS hi
        FROM keyword_performance_daily kpd
        JOIN keyword_instances ki ON ki.id = kpd.instance_id
        WHERE ki.account_id = ?{dev}
    """, (acct,)).fetchone()
    print(f"    report_date range: {rng['lo']} → {rng['hi']}")
    print("    by year:")
    print(f"      {'year':<6} {'rows':>12} {'distinct instances':>20}")
    for r in conn.execute(f"""
        SELECT substr(report_date,1,4) AS yr,
               COUNT(*) AS rows,
               COUNT(DISTINCT kpd.instance_id) AS insts
        FROM keyword_performance_daily kpd
        JOIN keyword_instances ki ON ki.id = kpd.instance_id
        WHERE ki.account_id = ?{dev}
        GROUP BY yr ORDER BY yr
    """, (acct,)).fetchall():
        print(f"      {r['yr']:<6} {r['rows']:>12,} {r['insts']:>20,}")
    print("    => solid 2025 rows/instances here ⇒ 2025 sqp skips should be LOW")

    # ── C. search_query_performance current coverage by month ─────────────────
    print("\n  C. search_query_performance coverage by month (Home)")
    print("  " + "-" * 68)
    sdev = " AND sqp.device = 'ALL'" if "device" in cols(conn, "search_query_performance") else ""
    rows = conn.execute(f"""
        SELECT substr(report_date,1,7) AS ym, COUNT(*) AS n
        FROM search_query_performance sqp
        JOIN keyword_instances ki ON ki.id = sqp.instance_id
        WHERE ki.account_id = ?{sdev}
        GROUP BY ym ORDER BY ym
    """, (acct,)).fetchall()
    if not rows:
        print("    (no sqp rows yet)")
    else:
        for r in rows:
            bar = "#" * min(40, max(1, r["n"] // 500))
            print(f"      {r['ym']}  {r['n']:>8,}  {bar}")
        print("    => look for missing months (the 2026 gap) and absent 2025")

    conn.close()
    print("\n" + "=" * 72)
    print("  Read B to decide: good 2025 kpd depth ⇒ backfill 2025 sqp directly.")
    print("  Thin/empty 2025 kpd ⇒ backfill keyword_instances history first.")
    print("=" * 72)


if __name__ == "__main__":
    main()
