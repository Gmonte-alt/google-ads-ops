"""
diag_campaign_perf_sync.py
===========================
Campaign Structure Analytics — Quote.com Google Ads
Folder: C:\\Projects\\google-ads-ops\\campaign_db_scripts\\

Diagnoses why campaign_performance_daily shows zero spend for Medicare
campaigns that clearly had spend during AEP 2025 and last 90 days.

Runs three checks:
  [1] How much campaign performance data exists for Medicare?
  [2] Do campaign_ids in performance match campaign_ids in campaigns table?
  [3] Are there performance rows for campaign_ids not in the campaigns table?

Usage:
  python diag_campaign_perf_sync.py
"""

import sqlite3
import logging
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

DB_PATH             = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
MEDICARE_ACCOUNT_ID = "5310820901"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


def main():
    log.info(f"Database: {DB_PATH}")
    log.info(f"Account : Medicare ({MEDICARE_ACCOUNT_ID})")
    conn = get_db_connection()

    # ── Check 1: Overall campaign_performance_daily coverage ──────────────────
    log.info("")
    log.info("=" * 65)
    log.info("[1] campaign_performance_daily — Medicare row counts")
    log.info("=" * 65)

    row = conn.execute("""
        SELECT
            COUNT(*)                    AS total_rows,
            MIN("date")                 AS earliest_date,
            MAX("date")                 AS latest_date,
            COUNT(DISTINCT campaign_id) AS distinct_campaigns,
            SUM(cost_micros) / 1e6      AS total_spend_dollars,
            SUM(conversions)            AS total_conversions
        FROM campaign_performance_daily
        WHERE account_id = ?
    """, (MEDICARE_ACCOUNT_ID,)).fetchone()

    log.info(f"  Total rows              : {row['total_rows']:,}")
    log.info(f"  Date range              : {row['earliest_date']} → {row['latest_date']}")
    log.info(f"  Distinct campaign_ids   : {row['distinct_campaigns']:,}")
    log.info(f"  Total spend             : ${row['total_spend_dollars']:,.2f}" if row['total_spend_dollars'] else "  Total spend             : $0")
    log.info(f"  Total conversions       : {row['total_conversions']:,.1f}" if row['total_conversions'] else "  Total conversions       : 0")

    # Breakdown by device to show data isn't all in one device bucket
    log.info("")
    log.info("  Row count by device:")
    device_rows = conn.execute("""
        SELECT device, COUNT(*) AS cnt, SUM(cost_micros)/1e6 AS spend
        FROM campaign_performance_daily
        WHERE account_id = ?
        GROUP BY device
        ORDER BY cnt DESC
    """, (MEDICARE_ACCOUNT_ID,)).fetchall()

    if device_rows:
        for r in device_rows:
            log.info(f"    {r['device']:<12} {r['cnt']:>8,} rows   ${r['spend']:>12,.2f} spend")
    else:
        log.warning("  No rows found at all for this account_id.")

    # ── Check 2: Campaign ID match between campaigns and performance tables ────
    log.info("")
    log.info("=" * 65)
    log.info("[2] Campaign ID match — campaigns table vs performance table")
    log.info("=" * 65)

    # ENABLED campaigns in campaigns table
    enabled = conn.execute("""
        SELECT campaign_id, campaign_name, status
        FROM campaigns
        WHERE account_id = ?
          AND status = 'ENABLED'
        ORDER BY campaign_name
    """, (MEDICARE_ACCOUNT_ID,)).fetchall()

    log.info(f"  ENABLED campaigns in campaigns table: {len(enabled)}")

    # For each ENABLED campaign, check if performance rows exist
    log.info("")
    log.info(f"  {'campaign_name':<55} {'perf_rows':>10} {'spend':>12}")
    log.info(f"  {'─'*55} {'─'*10} {'─'*12}")

    no_perf = []
    for c in enabled:
        perf = conn.execute("""
            SELECT COUNT(*) AS cnt, COALESCE(SUM(cost_micros)/1e6, 0) AS spend
            FROM campaign_performance_daily
            WHERE campaign_id = ?
        """, (c["campaign_id"],)).fetchone()

        status = "✓" if perf["cnt"] > 0 else "✗ NO PERF DATA"
        log.info(
            f"  {c['campaign_name'][:55]:<55} "
            f"{perf['cnt']:>10,} "
            f"${perf['spend']:>11,.2f}  {status}"
        )
        if perf["cnt"] == 0:
            no_perf.append(c["campaign_name"])

    if no_perf:
        log.warning("")
        log.warning(f"  {len(no_perf)} ENABLED campaign(s) have zero performance rows.")
    else:
        log.info("")
        log.info("  ✓ All ENABLED campaigns have performance data.")

    # Also check all non-REMOVED campaigns
    all_camps = conn.execute("""
        SELECT COUNT(*) AS cnt FROM campaigns
        WHERE account_id = ? AND status != 'REMOVED'
    """, (MEDICARE_ACCOUNT_ID,)).fetchone()
    log.info(f"\n  Total non-REMOVED campaigns in campaigns table: {all_camps['cnt']}")

    # ── Check 3: Performance rows with no matching campaign in campaigns table ─
    log.info("")
    log.info("=" * 65)
    log.info("[3] Performance rows with NO matching row in campaigns table")
    log.info("=" * 65)
    log.info("    (These campaign_ids exist in performance but not in campaigns)")

    orphan_rows = conn.execute("""
        SELECT
            p.campaign_id,
            MIN(p."date")          AS first_date,
            MAX(p."date")          AS last_date,
            COUNT(*)               AS perf_rows,
            SUM(p.cost_micros)/1e6 AS spend,
            SUM(p.conversions)     AS conversions
        FROM campaign_performance_daily p
        WHERE p.account_id = ?
          AND NOT EXISTS (
              SELECT 1 FROM campaigns c
              WHERE c.campaign_id = p.campaign_id
          )
        GROUP BY p.campaign_id
        ORDER BY spend DESC
    """, (MEDICARE_ACCOUNT_ID,)).fetchall()

    if orphan_rows:
        log.warning(f"  {len(orphan_rows)} campaign_id(s) in performance have no campaigns row:")
        log.info("")
        log.info(f"  {'campaign_id':<22} {'first_date':<12} {'last_date':<12} {'rows':>6} {'spend':>12} {'conv':>8}")
        log.info(f"  {'─'*22} {'─'*12} {'─'*12} {'─'*6} {'─'*12} {'─'*8}")
        for r in orphan_rows:
            log.info(
                f"  {r['campaign_id']:<22} "
                f"{r['first_date']:<12} "
                f"{r['last_date']:<12} "
                f"{r['perf_rows']:>6,} "
                f"${r['spend']:>11,.2f} "
                f"{r['conversions']:>8,.1f}"
            )
    else:
        log.info("  ✓ All performance campaign_ids have a matching campaigns row.")

    # ── Check 4: Spot-check AEP date range specifically ───────────────────────
    log.info("")
    log.info("=" * 65)
    log.info("[4] AEP 2025 spot check (Oct 15 – Dec 7, 2025)")
    log.info("=" * 65)

    aep_row = conn.execute("""
        SELECT
            COUNT(*)                    AS rows,
            COUNT(DISTINCT campaign_id) AS campaigns,
            SUM(cost_micros)/1e6        AS spend,
            SUM(conversions)            AS conversions
        FROM campaign_performance_daily
        WHERE account_id = ?
          AND "date" BETWEEN '2025-10-15' AND '2025-12-07'
    """, (MEDICARE_ACCOUNT_ID,)).fetchone()

    log.info(f"  Rows in AEP window      : {aep_row['rows']:,}")
    log.info(f"  Distinct campaigns      : {aep_row['campaigns']:,}")
    log.info(f"  Total spend             : ${aep_row['spend']:,.2f}" if aep_row['spend'] else "  Total spend             : $0")
    log.info(f"  Total conversions       : {aep_row['conversions']:,.1f}" if aep_row['conversions'] else "  Total conversions       : 0")

    # ── Check 5: campaign_performance_geo_daily coverage ─────────────────────
    log.info("")
    log.info("=" * 65)
    log.info("[5] campaign_performance_geo_daily — Medicare row counts")
    log.info("=" * 65)

    geo_row = conn.execute("""
        SELECT
            COUNT(*)                    AS total_rows,
            MIN("date")                 AS earliest_date,
            MAX("date")                 AS latest_date,
            COUNT(DISTINCT campaign_id) AS distinct_campaigns,
            SUM(cost_micros)/1e6        AS total_spend
        FROM campaign_performance_geo_daily
        WHERE account_id = ?
    """, (MEDICARE_ACCOUNT_ID,)).fetchone()

    log.info(f"  Total rows              : {geo_row['total_rows']:,}")
    log.info(f"  Date range              : {geo_row['earliest_date']} → {geo_row['latest_date']}")
    log.info(f"  Distinct campaign_ids   : {geo_row['distinct_campaigns']:,}")
    log.info(f"  Total spend             : ${geo_row['total_spend']:,.2f}" if geo_row['total_spend'] else "  Total spend             : $0")

    # Cross-check: do geo campaign_ids match campaigns table?
    geo_orphans = conn.execute("""
        SELECT COUNT(DISTINCT p.campaign_id) AS cnt
        FROM campaign_performance_geo_daily p
        WHERE p.account_id = ?
          AND NOT EXISTS (
              SELECT 1 FROM campaigns c WHERE c.campaign_id = p.campaign_id
          )
    """, (MEDICARE_ACCOUNT_ID,)).fetchone()

    if geo_orphans["cnt"] > 0:
        log.warning(f"  {geo_orphans['cnt']} campaign_id(s) in geo table have no campaigns row.")
    else:
        log.info("  ✓ All geo campaign_ids match the campaigns table.")

    # ── Verdict ───────────────────────────────────────────────────────────────
    log.info("")
    log.info("=" * 65)
    log.info("VERDICT")
    log.info("=" * 65)

    total_perf_rows  = conn.execute(
        "SELECT COUNT(*) FROM campaign_performance_daily WHERE account_id = ?",
        (MEDICARE_ACCOUNT_ID,)
    ).fetchone()[0]

    total_camp_rows  = conn.execute(
        "SELECT COUNT(*) FROM campaigns WHERE account_id = ?",
        (MEDICARE_ACCOUNT_ID,)
    ).fetchone()[0]

    if total_perf_rows == 0:
        log.warning("  ✗ campaign_performance_daily has ZERO rows for Medicare.")
        log.warning("    → The performance backfill has not been run for this account.")
        log.warning("    → Fix: python 03_sync_campaign_performance.py --daily-start 2025-01-01 --daily-only")
    elif len(orphan_rows) > 0 and total_camp_rows == 0:
        log.warning("  ✗ Performance data exists but campaigns table is empty.")
        log.warning("    → Fix: python 02_sync_campaigns_adgroups.py")
    elif len(orphan_rows) > 0:
        log.warning(f"  ✗ {len(orphan_rows)} campaign_ids in performance have no campaigns row.")
        log.warning("    → The campaigns table may have been repopulated with new IDs after the perf sync.")
        log.warning("    → Fix: python 03_sync_campaign_performance.py --daily-start 2025-01-01 --daily-only")
    elif len(no_perf) > 0:
        log.warning(f"  ✗ {len(no_perf)} ENABLED campaigns have no performance data.")
        log.warning("    → Fix: python 03_sync_campaign_performance.py --daily-start 2025-01-01 --daily-only")
    else:
        log.info("  ✓ Campaign IDs match across tables.")
        log.info("  ✓ All ENABLED campaigns have performance data.")
        log.info("  → Data sync looks correct. The analysis scripts may have a query issue.")

    conn.close()
    log.info("")
    log.info("Done.")


if __name__ == "__main__":
    main()
