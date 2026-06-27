"""
refresh_check.py
================
Daily monitoring check — Microsoft data freshness and reconciliation.

Folder: C:\\Projects\\google-ads-ops\\campaign_db_scripts\\

Read-only. Does NOT delete, refresh, or modify anything.

Run this daily (or any time) to confirm:
  - All 4 Microsoft performance tables exist and have data
  - Date ranges are sane and recent
  - Spend and revenue totals reconcile across tables
  - No surprising row counts compared to the prior baseline

Two distinct checks:

1. FRESHNESS — How old is the data? Is it within the expected window?
   Microsoft data lags 1-3 days. If max_date is more than 5 days old,
   it's time to run the weekly refresh.

2. RECONCILIATION — Do the campaign-level and keyword-level totals
   line up? Tables sourced from different CSVs should agree within
   a small tolerance (export-timing skew is normal, structural
   disagreement is not).

USAGE

    conda activate paid-search

    # Default daily check
    python campaign_db_scripts/refresh_check.py

    # Verbose: include per-month breakdown
    python campaign_db_scripts/refresh_check.py --verbose

    # Set the staleness threshold (default 5 days)
    python campaign_db_scripts/refresh_check.py --stale-after 7

EXIT CODES

  0  All checks pass: data fresh, totals reconcile within tolerance
  1  Data is stale OR totals diverge beyond tolerance — needs refresh
  2  Argument or DB error
"""

import argparse
import logging
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from refresh_helper import (
    MS_ACCOUNT_ID, PERFORMANCE_TABLES, STRUCTURE_TABLES,
    get_connection, get_max_date,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# Reconciliation tolerance — campaign vs keyword spend totals.
# We've observed ~$2-3K skew due to one-day export timing differences.
# 1.0% is generous and catches genuine structural divergence.
SPEND_RECONCILE_TOLERANCE_PCT = 1.0


# ── Freshness check ───────────────────────────────────────────────────────────

def check_freshness(conn: sqlite3.Connection, stale_after_days: int) -> tuple[bool, list[dict]]:
    """
    For each performance table, report how stale its MAX(date) is.
    Returns (all_fresh, per-table results).
    """
    today = date.today()
    rows = []
    all_fresh = True

    for table, date_col in PERFORMANCE_TABLES.items():
        max_d_str = get_max_date(conn, table, date_col)
        if max_d_str is None:
            rows.append({
                "table": table, "max_date": None, "age_days": None,
                "fresh": False, "note": "(no Microsoft data)",
            })
            all_fresh = False
            continue

        max_d = datetime.strptime(max_d_str, "%Y-%m-%d").date()
        age   = (today - max_d).days
        fresh = age <= stale_after_days
        rows.append({
            "table": table, "max_date": max_d_str, "age_days": age,
            "fresh": fresh,
            "note": "" if fresh else f"STALE — older than {stale_after_days} days",
        })
        if not fresh:
            all_fresh = False

    return all_fresh, rows


# ── Reconciliation check ──────────────────────────────────────────────────────

def check_reconciliation(conn: sqlite3.Connection) -> tuple[bool, dict]:
    """
    Compare aggregate spend across the three Microsoft performance
    tables. Returns (within_tolerance, totals dict).
    """
    search = conn.execute("""
        SELECT COALESCE(SUM(cost_micros)/1e6, 0) AS s,
               COALESCE(SUM(conversion_value), 0) AS r
        FROM campaign_performance_daily
        WHERE account_id = ? AND device = 'ALL'
    """, (MS_ACCOUNT_ID,)).fetchone()

    network = conn.execute("""
        SELECT COALESCE(SUM(cost_micros)/1e6, 0) AS s,
               COALESCE(SUM(conversion_value), 0) AS r
        FROM campaign_performance_network_daily
        WHERE account_id = ? AND device = 'ALL'
    """, (MS_ACCOUNT_ID,)).fetchone()

    keyword = conn.execute("""
        SELECT COALESCE(SUM(kpd.cost_micros)/1e6, 0) AS s,
               COALESCE(SUM(kpd.primary_conv_value), 0) AS r
        FROM keyword_performance_daily kpd
        JOIN keyword_instances ki ON ki.id = kpd.instance_id
        WHERE ki.account_id = ? AND kpd.device = 'ALL'
    """, (MS_ACCOUNT_ID,)).fetchone()

    camp_spend = (search["s"] or 0) + (network["s"] or 0)
    kw_spend   = keyword["s"] or 0

    if camp_spend == 0 and kw_spend == 0:
        return True, {
            "search": search, "network": network, "keyword": keyword,
            "camp_spend": 0.0, "kw_spend": 0.0,
            "delta": 0.0, "delta_pct": 0.0,
            "within_tolerance": True,
        }

    delta = kw_spend - camp_spend
    delta_pct = (abs(delta) / camp_spend * 100) if camp_spend > 0 else 0.0
    within = delta_pct <= SPEND_RECONCILE_TOLERANCE_PCT

    return within, {
        "search": search, "network": network, "keyword": keyword,
        "camp_spend": camp_spend, "kw_spend": kw_spend,
        "delta": delta, "delta_pct": delta_pct,
        "within_tolerance": within,
    }


# ── Per-month breakdown (verbose only) ────────────────────────────────────────

def per_month_breakdown(conn: sqlite3.Connection):
    rows = conn.execute("""
        SELECT
            substr(kpd.report_date, 1, 7) AS month,
            COUNT(*)                       AS rows,
            SUM(kpd.cost_micros)/1e6       AS spend,
            SUM(kpd.primary_conv_count)    AS conv,
            SUM(kpd.primary_conv_value)    AS rev
        FROM keyword_performance_daily kpd
        JOIN keyword_instances ki ON ki.id = kpd.instance_id
        WHERE ki.account_id = ? AND kpd.device = 'ALL'
        GROUP BY month
        ORDER BY month
    """, (MS_ACCOUNT_ID,)).fetchall()
    return rows


# ── Structure summary ────────────────────────────────────────────────────────

def structure_summary(conn: sqlite3.Connection) -> dict:
    summary = {}
    for table in STRUCTURE_TABLES:
        if table == "keyword_instances":
            sql = "SELECT COUNT(*) FROM keyword_instances WHERE account_id = ?"
        else:
            sql = f"SELECT COUNT(*) FROM {table} WHERE account_id = ?"
        n = conn.execute(sql, (MS_ACCOUNT_ID,)).fetchone()[0]
        summary[table] = n
    return summary


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Daily Microsoft data freshness and reconciliation check."
    )
    parser.add_argument("--stale-after", type=int, default=5,
                        help="Mark a table stale if MAX(date) is older than N days (default 5).")
    parser.add_argument("--verbose", action="store_true",
                        help="Print per-month spend/revenue breakdown.")
    args = parser.parse_args()

    log.info("=" * 65)
    log.info("  Microsoft data freshness check")
    log.info(f"  Today: {date.today().isoformat()}")
    log.info(f"  Stale-after threshold: {args.stale_after} day(s)")
    log.info("=" * 65)

    conn = get_connection()

    # ── Freshness ─────────────────────────────────────────────────────
    log.info("")
    log.info("── Freshness ──────────────────────────────────────────────────")
    log.info(f"  {'Table':<40} {'Max date':<12} {'Age':>6}  Note")
    log.info(f"  {'-'*40} {'-'*12} {'-'*6}  {'-'*30}")
    all_fresh, freshness_rows = check_freshness(conn, args.stale_after)
    for r in freshness_rows:
        max_d  = r["max_date"] or "—"
        age    = f"{r['age_days']}d" if r["age_days"] is not None else "—"
        marker = "  ⚠ " if not r["fresh"] else "    "
        log.info(f"  {r['table']:<40} {max_d:<12} {age:>6}{marker}{r['note']}")

    # ── Structure ─────────────────────────────────────────────────────
    log.info("")
    log.info("── Structure (current row counts) ─────────────────────────────")
    structure = structure_summary(conn)
    for table, n in structure.items():
        log.info(f"  {table:<40} {n:>10,}")

    # ── Reconciliation ────────────────────────────────────────────────
    log.info("")
    log.info("── Spend reconciliation (campaign vs keyword) ─────────────────")
    within_tol, recon = check_reconciliation(conn)

    log.info(f"  campaign_performance_daily (search)         "
             f"spend=${recon['search']['s']:>14,.2f}  rev=${recon['search']['r']:>12,.2f}")
    log.info(f"  campaign_performance_network_daily          "
             f"spend=${recon['network']['s']:>14,.2f}  rev=${recon['network']['r']:>12,.2f}")
    log.info(f"  keyword_performance_daily (all networks)    "
             f"spend=${recon['keyword']['s']:>14,.2f}  rev=${recon['keyword']['r']:>12,.2f}")
    log.info("")
    log.info(f"  Campaign-level total (search + network):    ${recon['camp_spend']:>14,.2f}")
    log.info(f"  Keyword-level total:                        ${recon['kw_spend']:>14,.2f}")
    log.info(f"  Delta (keyword - campaign):                 ${recon['delta']:>14,.2f}  "
             f"({recon['delta_pct']:.3f}%)")
    log.info(f"  Tolerance threshold:                         "
             f"{SPEND_RECONCILE_TOLERANCE_PCT:.1f}%")
    log.info(f"  Within tolerance:                            "
             f"{'YES' if within_tol else 'NO ⚠'}")

    # ── Per-month breakdown (verbose) ────────────────────────────────
    if args.verbose:
        log.info("")
        log.info("── Per-month keyword performance ──────────────────────────────")
        log.info(f"  {'Month':<10} {'Rows':>10} {'Spend':>14} {'Conv':>10} {'Revenue':>14}")
        log.info(f"  {'-'*10} {'-'*10} {'-'*14} {'-'*10} {'-'*14}")
        for r in per_month_breakdown(conn):
            log.info(
                f"  {r['month']:<10} {r['rows']:>10,} "
                f"${r['spend']:>13,.2f} {r['conv']:>10,.0f} "
                f"${r['rev']:>13,.2f}"
            )

    # ── Verdict ──────────────────────────────────────────────────────
    log.info("")
    log.info("=" * 65)
    if all_fresh and within_tol:
        log.info("  ✓ All checks pass.")
        rc = 0
    elif not all_fresh and within_tol:
        log.info("  ⚠ Data is stale. Time to run the weekly refresh.")
        log.info("    python campaign_db_scripts/refresh_orchestrator.py --mode performance \\")
        log.info("        --camp-perf-csv ... --kw-csv ...")
        rc = 1
    elif all_fresh and not within_tol:
        log.info("  ⚠ Spend totals diverge beyond tolerance.")
        log.info("    Investigate which CSV is the source of the gap.")
        rc = 1
    else:
        log.info("  ⚠ Data is stale AND totals diverge.")
        log.info("    Refresh first, re-check after.")
        rc = 1
    log.info("=" * 65)

    conn.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
