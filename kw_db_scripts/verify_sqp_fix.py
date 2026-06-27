"""
verify_sqp_fix.py  — read-only sqp verification for any account window
======================================================================
Checks a date window (optionally a single campaign) on search_query_performance:

  Check 1 (value check): primary routing populated — non-zero primary_conv /
          primary_rev, and all_conv >= primary_conv.
  Check 2 (subset ceiling): sqp spend <= kpd spend for the same window/scope
          (search terms are a subset of keyword traffic). Auto-detects whether
          kpd has a campaign_id column.
  Check 3 (skips & dates): row count, MIN/MAX(report_date), and the UNKNOWN-
          match-type row count (conversion-only rows with no matching traffic).

READ-ONLY. No writes.

    # whole account, a backfill segment:
    python verify_sqp_fix.py --start 2025-04-01 --end 2025-06-30

    # single campaign (original use):
    python verify_sqp_fix.py --campaign-id 23496334752 --start 2026-06-01 --end 2026-06-07
"""

import argparse
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")


def parse_args():
    p = argparse.ArgumentParser(description="Verify sqp for an account window (read-only).")
    p.add_argument("--account-id", type=int, default=1)
    p.add_argument("--campaign-id", default=None, help="Omit to check the whole account.")
    p.add_argument("--start", default="2026-06-01")
    p.add_argument("--end",   default="2026-06-07")
    return p.parse_args()


def kpd_has_campaign_id(conn) -> bool:
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(keyword_performance_daily)").fetchall()]
    return "campaign_id" in cols


def main():
    a = parse_args()
    if not DB_PATH.exists():
        sys.exit(f"DB not found: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    acct, camp, start, end = a.account_id, a.campaign_id, a.start, a.end

    camp_pred = " AND sqp.campaign_id = ?" if camp else ""
    camp_args = (camp,) if camp else ()

    print("=" * 72)
    print("  sqp verification (read-only)")
    print(f"  account_id={acct}  campaign={camp or 'ALL'}  window={start} → {end}")
    print("=" * 72)

    # ── Check 1: value check ──────────────────────────────────────────────────
    row = conn.execute(f"""
        SELECT COUNT(*)                          AS rows,
               ROUND(SUM(cost_micros)/1e6, 2)    AS spend,
               ROUND(SUM(primary_conv_value), 2) AS primary_rev,
               ROUND(SUM(primary_conv_count), 2) AS primary_conv,
               ROUND(SUM(secondary_conv_count),2)AS secondary_conv,
               ROUND(SUM(all_conv_count), 2)     AS all_conv
        FROM search_query_performance sqp
        JOIN keyword_instances ki ON ki.id = sqp.instance_id
        WHERE ki.account_id = ? AND sqp.report_date BETWEEN ? AND ?
          AND sqp.device = 'ALL'{camp_pred}
    """, (acct, start, end) + camp_args).fetchone()

    pc, pr, ac = row["primary_conv"] or 0, row["primary_rev"] or 0, row["all_conv"] or 0
    ok1 = pc > 0 and pr > 0 and ac >= pc
    print("\n  CHECK 1 — value check")
    print(f"    rows={row['rows']:,}  spend=${row['spend'] or 0:,.2f}  "
          f"primary_conv={pc:,.1f}  primary_rev=${pr:,.2f}  "
          f"secondary_conv={row['secondary_conv'] or 0:,.1f}  all_conv={ac:,.1f}")
    print(f"    => CHECK 1 {'PASS' if ok1 else 'REVIEW'}")

    # ── Check 2: subset ceiling ───────────────────────────────────────────────
    sqp_spend = conn.execute(f"""
        SELECT ROUND(SUM(sqp.cost_micros)/1e6, 2) AS s
        FROM search_query_performance sqp
        JOIN keyword_instances ki ON ki.id = sqp.instance_id
        WHERE ki.account_id = ? AND sqp.report_date BETWEEN ? AND ?
          AND sqp.device = 'ALL'{camp_pred}
    """, (acct, start, end) + camp_args).fetchone()["s"] or 0.0

    if camp and kpd_has_campaign_id(conn):
        kpd_spend = conn.execute("""
            SELECT ROUND(SUM(kpd.cost_micros)/1e6, 2) AS s
            FROM keyword_performance_daily kpd
            JOIN keyword_instances ki ON ki.id = kpd.instance_id
            WHERE ki.account_id = ? AND kpd.campaign_id = ?
              AND kpd.report_date BETWEEN ? AND ? AND kpd.device = 'ALL'
        """, (acct, camp, start, end)).fetchone()["s"] or 0.0
        scope = "kpd.campaign_id"
    else:
        kpd_spend = conn.execute("""
            SELECT ROUND(SUM(kpd.cost_micros)/1e6, 2) AS s
            FROM keyword_performance_daily kpd
            JOIN keyword_instances ki ON ki.id = kpd.instance_id
            WHERE ki.account_id = ? AND kpd.report_date BETWEEN ? AND ? AND kpd.device = 'ALL'
        """, (acct, start, end)).fetchone()["s"] or 0.0
        scope = "account-level" + ("" if not camp else " (kpd has no campaign_id)")

    ok2 = sqp_spend <= kpd_spend + 0.01
    print("\n  CHECK 2 — subset ceiling (sqp <= kpd)")
    print(f"    scope={scope}  sqp=${sqp_spend:,.2f}  kpd=${kpd_spend:,.2f}"
          + (f"  ({100.0*sqp_spend/kpd_spend:.1f}%)" if kpd_spend else ""))
    print(f"    => CHECK 2 {'PASS' if ok2 else 'REVIEW (sqp exceeds kpd)'}")

    # ── Check 3: dates + UNKNOWN rows ─────────────────────────────────────────
    d = conn.execute(f"""
        SELECT MIN(report_date) AS lo, MAX(report_date) AS hi,
               SUM(CASE WHEN match_type_served='UNKNOWN' THEN 1 ELSE 0 END) AS unk
        FROM search_query_performance sqp
        JOIN keyword_instances ki ON ki.id = sqp.instance_id
        WHERE ki.account_id = ? AND sqp.report_date BETWEEN ? AND ?
          AND sqp.device = 'ALL'{camp_pred}
    """, (acct, start, end) + camp_args).fetchone()
    print("\n  CHECK 3 — dates & UNKNOWN rows")
    print(f"    report_date range: {d['lo']} → {d['hi']}")
    print(f"    UNKNOWN-match-type rows (conv-only, no traffic): {d['unk'] or 0:,}")

    print("\n" + "=" * 72)
    print(f"  OVERALL: {'PASS' if (ok1 and ok2) else 'REVIEW — see flags'}")
    print("=" * 72)
    conn.close()


if __name__ == "__main__":
    main()
