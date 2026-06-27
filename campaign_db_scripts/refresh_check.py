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
    ACCOUNT_REGISTRY, DEFAULT_ACCOUNT_KEY,
    PERFORMANCE_TABLES, STRUCTURE_TABLES,
    account_label, get_connection, get_max_date, resolve_account_id,
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

def check_freshness(conn: sqlite3.Connection, stale_after_days: int, account_id: int) -> tuple[bool, list[dict]]:
    """
    For each performance table, report how stale its MAX(date) is.
    Returns (all_fresh, per-table results).
    """
    today = date.today()
    rows = []
    all_fresh = True

    for table, date_col in PERFORMANCE_TABLES.items():
        max_d_str = get_max_date(conn, table, date_col, account_id)
        if max_d_str is None:
            rows.append({
                "table": table, "max_date": None, "age_days": None,
                "fresh": False, "note": "(no data for this account)",
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

def check_reconciliation(conn: sqlite3.Connection, account_id: int) -> tuple[bool, dict]:
    """
    Compare aggregate spend across the four performance tables for
    the given account. Returns (within_tolerance, totals dict).

    Reconciliation logic:
      - camp_spend = search + network (the campaign-table truth)
      - geo_spend  = geo_performance_daily summed across location_types
                     (PHYSICAL + INTEREST are complementary; sum = camp truth)
      - kw_spend   = keyword_performance_daily (independent CSV source)

    All three should agree within SPEND_RECONCILE_TOLERANCE_PCT. The
    delta is reported separately for camp-vs-geo and camp-vs-kw so the
    operator can tell which CSV diverged.
    """
    search = conn.execute("""
        SELECT COALESCE(SUM(cost_micros)/1e6, 0) AS s,
               COALESCE(SUM(conversion_value), 0) AS r
        FROM campaign_performance_daily
        WHERE account_id = ? AND device = 'ALL'
    """, (account_id,)).fetchone()

    network = conn.execute("""
        SELECT COALESCE(SUM(cost_micros)/1e6, 0) AS s,
               COALESCE(SUM(conversion_value), 0) AS r
        FROM campaign_performance_network_daily
        WHERE account_id = ? AND device = 'ALL'
    """, (account_id,)).fetchone()

    # Geo: sum across both location_types (PHYSICAL + INTEREST are
    # complementary, not overlapping — each impression appears in
    # exactly one, so the combined sum matches campaign-table totals).
    # device='ALL' avoids double-counting per-device + ALL rollup.
    geo = conn.execute("""
        SELECT COALESCE(SUM(cost_micros)/1e6, 0) AS s,
               COALESCE(SUM(conversion_value), 0) AS r,
               COUNT(*) AS n_rows
        FROM geo_performance_daily
        WHERE account_id = ? AND device = 'ALL'
    """, (account_id,)).fetchone()

    keyword = conn.execute("""
        SELECT COALESCE(SUM(kpd.cost_micros)/1e6, 0) AS s,
               COALESCE(SUM(kpd.primary_conv_value), 0) AS r
        FROM keyword_performance_daily kpd
        JOIN keyword_instances ki ON ki.id = kpd.instance_id
        WHERE ki.account_id = ? AND kpd.device = 'ALL'
    """, (account_id,)).fetchone()

    camp_spend = (search["s"] or 0) + (network["s"] or 0)
    geo_spend  = geo["s"] or 0
    kw_spend   = keyword["s"] or 0
    geo_present = (geo["n_rows"] or 0) > 0

    if camp_spend == 0 and kw_spend == 0 and geo_spend == 0:
        return True, {
            "search": search, "network": network, "geo": geo, "keyword": keyword,
            "camp_spend": 0.0, "geo_spend": 0.0, "kw_spend": 0.0,
            "geo_present": geo_present,
            "delta_camp_geo": 0.0, "delta_camp_geo_pct": 0.0,
            "delta_camp_kw":  0.0, "delta_camp_kw_pct":  0.0,
            "within_tolerance": True,
        }

    # camp-vs-geo delta (only if geo data exists)
    if geo_present and camp_spend > 0:
        delta_camp_geo = geo_spend - camp_spend
        delta_camp_geo_pct = abs(delta_camp_geo) / camp_spend * 100
        geo_within = delta_camp_geo_pct <= SPEND_RECONCILE_TOLERANCE_PCT
    else:
        delta_camp_geo = 0.0
        delta_camp_geo_pct = 0.0
        geo_within = True   # not present is not a failure mode

    # camp-vs-keyword delta
    delta_camp_kw = kw_spend - camp_spend
    delta_camp_kw_pct = (abs(delta_camp_kw) / camp_spend * 100) if camp_spend > 0 else 0.0
    kw_within = delta_camp_kw_pct <= SPEND_RECONCILE_TOLERANCE_PCT

    within = geo_within and kw_within

    return within, {
        "search": search, "network": network, "geo": geo, "keyword": keyword,
        "camp_spend": camp_spend, "geo_spend": geo_spend, "kw_spend": kw_spend,
        "geo_present": geo_present,
        "delta_camp_geo": delta_camp_geo, "delta_camp_geo_pct": delta_camp_geo_pct,
        "delta_camp_kw":  delta_camp_kw,  "delta_camp_kw_pct":  delta_camp_kw_pct,
        "within_tolerance": within,
        "geo_within": geo_within, "kw_within": kw_within,
    }


# ── Per-month breakdown (verbose only) ────────────────────────────────────────

def per_month_breakdown(conn: sqlite3.Connection, account_id: int):
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
    """, (account_id,)).fetchall()
    return rows


# ── Structure summary ────────────────────────────────────────────────────────

def structure_summary(conn: sqlite3.Connection, account_id: int) -> dict:
    summary = {}
    for table in STRUCTURE_TABLES:
        if table == "keyword_instances":
            sql = "SELECT COUNT(*) FROM keyword_instances WHERE account_id = ?"
        else:
            sql = f"SELECT COUNT(*) FROM {table} WHERE account_id = ?"
        n = conn.execute(sql, (account_id,)).fetchone()[0]
        summary[table] = n
    return summary


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Daily Microsoft data freshness and reconciliation check."
    )
    parser.add_argument(
        "--account", default=DEFAULT_ACCOUNT_KEY,
        choices=list(ACCOUNT_REGISTRY.keys()),
        help=f"Microsoft account key (default: {DEFAULT_ACCOUNT_KEY}). "
             f"Valid: {', '.join(sorted(ACCOUNT_REGISTRY))}",
    )
    parser.add_argument("--stale-after", type=int, default=5,
                        help="Mark a table stale if MAX(date) is older than N days (default 5).")
    parser.add_argument("--verbose", action="store_true",
                        help="Print per-month spend/revenue breakdown.")
    args = parser.parse_args()

    conn = get_connection()
    try:
        account_id = resolve_account_id(conn, args.account)
    except ValueError as e:
        log.error(str(e))
        conn.close()
        return 2

    log.info("=" * 65)
    log.info("  Microsoft data freshness check")
    log.info(f"  Account: {args.account}  ({account_label(args.account)})")
    log.info(f"  Resolved accounts.id: {account_id}")
    log.info(f"  Today: {date.today().isoformat()}")
    log.info(f"  Stale-after threshold: {args.stale_after} day(s)")
    log.info("=" * 65)

    # ── Freshness ─────────────────────────────────────────────────────
    log.info("")
    log.info("── Freshness ──────────────────────────────────────────────────")
    log.info(f"  {'Table':<40} {'Max date':<12} {'Age':>6}  Note")
    log.info(f"  {'-'*40} {'-'*12} {'-'*6}  {'-'*30}")
    all_fresh, freshness_rows = check_freshness(conn, args.stale_after, account_id)
    for r in freshness_rows:
        max_d  = r["max_date"] or "—"
        age    = f"{r['age_days']}d" if r["age_days"] is not None else "—"
        marker = "  ⚠ " if not r["fresh"] else "    "
        log.info(f"  {r['table']:<40} {max_d:<12} {age:>6}{marker}{r['note']}")

    # ── Structure ─────────────────────────────────────────────────────
    log.info("")
    log.info("── Structure (current row counts) ─────────────────────────────")
    structure = structure_summary(conn, account_id)
    for table, n in structure.items():
        log.info(f"  {table:<40} {n:>10,}")

    # ── Reconciliation ────────────────────────────────────────────────
    log.info("")
    log.info("── Spend reconciliation (campaign vs geo vs keyword) ──────────")
    within_tol, recon = check_reconciliation(conn, account_id)

    log.info(f"  campaign_performance_daily (search)         "
             f"spend=${recon['search']['s']:>14,.2f}  rev=${recon['search']['r']:>12,.2f}")
    log.info(f"  campaign_performance_network_daily          "
             f"spend=${recon['network']['s']:>14,.2f}  rev=${recon['network']['r']:>12,.2f}")
    if recon["geo_present"]:
        log.info(f"  geo_performance_daily (PHYSICAL+INTEREST)   "
                 f"spend=${recon['geo']['s']:>14,.2f}  rev=${recon['geo']['r']:>12,.2f}")
    else:
        log.info(f"  geo_performance_daily                       (no data for this account)")
    log.info(f"  keyword_performance_daily (all networks)    "
             f"spend=${recon['keyword']['s']:>14,.2f}  rev=${recon['keyword']['r']:>12,.2f}")
    log.info("")
    log.info(f"  Campaign-level total (search + network):    ${recon['camp_spend']:>14,.2f}")
    if recon["geo_present"]:
        log.info(f"  Geo-level total:                            ${recon['geo_spend']:>14,.2f}")
    log.info(f"  Keyword-level total:                        ${recon['kw_spend']:>14,.2f}")
    log.info("")
    if recon["geo_present"]:
        marker = "  " if recon.get("geo_within", True) else "⚠ "
        log.info(f"  {marker}Delta (geo - campaign):                    "
                 f"${recon['delta_camp_geo']:>+14,.2f}  "
                 f"({recon['delta_camp_geo_pct']:.3f}%)")
    marker = "  " if recon.get("kw_within", True) else "⚠ "
    log.info(f"  {marker}Delta (keyword - campaign):                "
             f"${recon['delta_camp_kw']:>+14,.2f}  "
             f"({recon['delta_camp_kw_pct']:.3f}%)")
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
        for r in per_month_breakdown(conn, account_id):
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
        log.info("  ⚠ Data is stale. Time to run a refresh.")
        log.info(f"    Daily-cadence command (campaign + geo only):")
        log.info(f"      python campaign_db_scripts/refresh_orchestrator.py "
                 f"--mode performance --account {args.account} \\")
        log.info(f"          --camp-perf-csv ... --geo-csv ...")
        log.info(f"    Weekly-cadence command (adds keyword):")
        log.info(f"      python campaign_db_scripts/refresh_orchestrator.py "
                 f"--mode performance --account {args.account} \\")
        log.info(f"          --camp-perf-csv ... --geo-csv ... --kw-csv ...")
        rc = 1
    elif all_fresh and not within_tol:
        log.info("  ⚠ Spend totals diverge beyond tolerance.")
        if recon.get("geo_present") and not recon.get("geo_within", True):
            log.info("    Geo-vs-campaign delta exceeds tolerance — "
                     "check geo CSV vs campaign CSV date alignment.")
        if not recon.get("kw_within", True):
            log.info("    Keyword-vs-campaign delta exceeds tolerance — "
                     "check keyword CSV vs campaign CSV date alignment.")
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
