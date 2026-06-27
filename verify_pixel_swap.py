"""
verify_pixel_swap.py
====================
Post-backfill verification: confirms that loaders 09 and 04 are now
storing pixel-only conversions in the primary count columns, while
keeping All-revenue in the conversion_value / primary_conv_value
columns.

Run AFTER backfilling with the updated loaders:

    conda activate paid-search
    python verify_pixel_swap.py

EXPECTED RESULTS for Texas, May 4-10, 2026:
  Campaign tables:
    conversions      = ~30  (pixel count from CSV "Conversions")
    conversion_value = ~$2,758  (All revenue)
    all_conversions  = ~68  (preserved offline-inclusive count)
    Reconciliation against Microsoft UI "Conv." column should now match.

  Keyword table:
    primary_conv_count  = pixel count (matches campaign-level conversions)
    primary_conv_value  = All revenue (matches campaign-level conv_value)
    secondary_conv_count = offline-only count (= all - pixel)
    all_conv_count       = full Microsoft total

  Note: small drift (<10%) from UI is expected due to Microsoft's
  ongoing offline-conversion attribution after the original export.

The script also prints a snapshot of Google data totals. The Microsoft
loaders never touch Google rows (every operation is scoped to Microsoft
account_ids 2 and 54), so these numbers should be unchanged between
pre-backfill and post-backfill runs. If you want to prove this:
  1. Run this script BEFORE the Microsoft backfill, save the Google
     snapshot section
  2. Run the Microsoft backfill (loaders 09 and 04)
  3. Run this script AFTER, compare the Google snapshot — should be
     bit-for-bit identical
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
ACCOUNT_ID = 2  # Microsoft Home Insurance
CAMPAIGN_NAME = "HOME INSURANCE-TEXAS-SEARCH-MOBILE-USA-STE"
START_DATE = "2026-05-04"
END_DATE = "2026-05-10"

# UI values from Microsoft Ads (pulled previously)
UI_PIXEL_CONV_SEARCH = 27       # UI "Conv." Microsoft sites + AOL
UI_PIXEL_CONV_AUDIENCE = 3      # UI "Conv." Audience
UI_ALL_CONV_SEARCH = 58         # UI "All conv." Microsoft sites + AOL
UI_ALL_CONV_AUDIENCE = 10       # UI "All conv." Audience
UI_REVENUE_SEARCH = 2261.38     # UI "All conv. revenue" search
UI_REVENUE_AUDIENCE = 496.87    # UI "All conv. revenue" audience


def banner(text, char="="):
    print(char * 78)
    print(text)
    print(char * 78)


def main():
    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}")
        return 1

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Resolve campaign
    camp = conn.execute(
        "SELECT campaign_id FROM campaigns WHERE account_id = ? AND campaign_name = ?",
        (ACCOUNT_ID, CAMPAIGN_NAME),
    ).fetchone()
    if camp is None:
        print(f"ERROR: campaign {CAMPAIGN_NAME!r} not found for account {ACCOUNT_ID}")
        return 1
    campaign_id = camp["campaign_id"]

    # ── Campaign-level checks ──────────────────────────────────────────
    banner(f"CAMPAIGN-LEVEL VERIFICATION (Texas, {START_DATE} to {END_DATE})")
    print()

    # Detect columns
    cpd_cols = {r["name"] for r in conn.execute("PRAGMA table_info(campaign_performance_daily)").fetchall()}
    has_all_conv = "all_conversions" in cpd_cols

    search = conn.execute(
        f"""
        SELECT
            SUM(cost_micros) / 1.0e6   AS spend,
            SUM(conversions)            AS conv,
            SUM(conversion_value)       AS rev{', SUM(all_conversions) AS all_conv' if has_all_conv else ''}
        FROM campaign_performance_daily
        WHERE campaign_id = ?
          AND date BETWEEN ? AND ?
          AND device = 'ALL'
        """,
        (campaign_id, START_DATE, END_DATE),
    ).fetchone()

    audience = conn.execute(
        f"""
        SELECT
            SUM(cost_micros) / 1.0e6   AS spend,
            SUM(conversions)            AS conv,
            SUM(conversion_value)       AS rev{', SUM(all_conversions) AS all_conv' if has_all_conv else ''}
        FROM campaign_performance_network_daily
        WHERE campaign_id = ?
          AND date BETWEEN ? AND ?
          AND device = 'ALL'
          AND network = 'Audience'
        """,
        (campaign_id, START_DATE, END_DATE),
    ).fetchone()

    ui_pixel_total = UI_PIXEL_CONV_SEARCH + UI_PIXEL_CONV_AUDIENCE
    ui_all_total = UI_ALL_CONV_SEARCH + UI_ALL_CONV_AUDIENCE
    ui_rev_total = UI_REVENUE_SEARCH + UI_REVENUE_AUDIENCE

    db_conv_total = (search["conv"] or 0) + (audience["conv"] or 0)
    db_rev_total = (search["rev"] or 0) + (audience["rev"] or 0)
    db_all_total = (
        ((search["all_conv"] or 0) + (audience["all_conv"] or 0))
        if has_all_conv else None
    )

    print(f"  {'Metric':<40} {'UI':>12} {'DB':>12} {'Δ':>10}")
    print(f"  {'-'*40} {'-'*12} {'-'*12} {'-'*10}")
    print(f"  {'conversions vs UI Conv. (pixel)':<40} {ui_pixel_total:>12} "
          f"{int(db_conv_total):>12} {int(db_conv_total) - ui_pixel_total:>+10}")
    if has_all_conv:
        print(f"  {'all_conversions vs UI All conv.':<40} {ui_all_total:>12} "
              f"{int(db_all_total):>12} {int(db_all_total) - ui_all_total:>+10}")
    print(f"  {'conversion_value vs UI All conv. rev':<40} ${ui_rev_total:>10,.2f} "
          f"${db_rev_total:>10,.2f} {db_rev_total - ui_rev_total:>+10,.2f}")
    print()

    pixel_match = abs(int(db_conv_total) - ui_pixel_total) <= 5  # allow drift
    print(f"  Verdict:")
    print(f"    pixel conv count matches UI Conv. (±5)    : {'YES' if pixel_match else 'NO  ⚠'}")
    if has_all_conv:
        all_match = abs(int(db_all_total) - ui_all_total) <= 5
        print(f"    all_conv count matches UI All conv. (±5)  : {'YES' if all_match else 'NO  ⚠'}")
    print()

    # ── Keyword-level checks ───────────────────────────────────────────
    banner(f"KEYWORD-LEVEL VERIFICATION (Texas, {START_DATE} to {END_DATE})")
    print()

    kpd_cols = {r["name"] for r in conn.execute("PRAGMA table_info(keyword_performance_daily)").fetchall()}

    sql = """
        SELECT
            SUM(kpd.cost_micros) / 1.0e6        AS spend,
            SUM(kpd.primary_conv_count)          AS primary_count,
            SUM(kpd.primary_conv_value)          AS rev,
            SUM(kpd.secondary_conv_count)        AS secondary,
            SUM(kpd.all_conv_count)              AS all_conv
        FROM keyword_performance_daily kpd
        JOIN keyword_instances ki ON ki.id = kpd.instance_id
        WHERE ki.account_id = ?
          AND ki.campaign_id = ?
          AND kpd.report_date BETWEEN ? AND ?
    """
    kw = conn.execute(sql, (ACCOUNT_ID, campaign_id, START_DATE, END_DATE)).fetchone()

    # Keyword table currently has no device='ALL' (per our earlier finding).
    # Sum across all per-device rows.
    print(f"  Keyword aggregates (all devices, all rows for Texas in window):")
    print(f"    spend                : ${(kw['spend'] or 0):>10,.2f}")
    print(f"    primary_conv_count   : {(kw['primary_count'] or 0):>10}  (expected after swap: pixel-only, matches campaign-level conversions)")
    print(f"    primary_conv_value   : ${(kw['rev'] or 0):>10,.2f}  (expected: All revenue, matches campaign-level conversion_value)")
    print(f"    secondary_conv_count : {(kw['secondary'] or 0):>10}  (expected after swap: offline-only, matches All - pixel)")
    print(f"    all_conv_count       : {(kw['all_conv'] or 0):>10}  (expected: full Microsoft total)")
    print()

    # Cross-check: keyword primary should match campaign-level pixel conv
    # within whatever the existing rollup-vs-keyword drift is.
    kw_primary = kw["primary_count"] or 0
    print(f"  Cross-grain reconciliation:")
    print(f"    Campaign conversions (pixel)   : {int(db_conv_total):>8}")
    print(f"    Keyword primary_conv_count     : {int(kw_primary):>8}")
    delta = int(kw_primary) - int(db_conv_total)
    print(f"    Δ keyword - campaign            : {delta:>+8}")
    if abs(delta) < 5:
        print(f"    → Reasonably aligned")
    else:
        print(f"    → ⚠ Drift larger than expected; investigate")
    print()

    # Secondary count = should equal all_conv - primary
    # (these come from the loader's "offline_conv_count = all - conversions" computation)
    expected_secondary = (kw["all_conv"] or 0) - (kw["primary_count"] or 0)
    print(f"  Internal consistency check:")
    print(f"    all_conv_count                  : {(kw['all_conv'] or 0):>8}")
    print(f"    primary_conv_count (pixel)      : {(kw['primary_count'] or 0):>8}")
    print(f"    Computed: all - pixel           : {int(expected_secondary):>8}")
    print(f"    secondary_conv_count (stored)   : {int(kw['secondary'] or 0):>8}")
    if int(kw['secondary'] or 0) == int(expected_secondary):
        print(f"    → secondary = all - primary (consistent)")
    else:
        print(f"    → ⚠ secondary != all - primary (loader may need re-run)")
    print()

    # ── Google data integrity snapshot ─────────────────────────────────
    banner(f"GOOGLE DATA INTEGRITY SNAPSHOT")
    print()
    print("  Google rows are never touched by the Microsoft loaders.")
    print("  This snapshot exists so you can prove zero drift: run this")
    print("  script before AND after the Microsoft backfill — the Google")
    print("  numbers below should be bit-for-bit identical.")
    print()

    google_accounts = conn.execute(
        """
        SELECT id, account_name, platform_account_id
        FROM accounts
        WHERE platform = 'google'
        ORDER BY id
        """
    ).fetchall()

    print(f"  {'acct_id':>7} {'account':<20} {'platform_id':<14} "
          f"{'camp_rows':>12} {'kw_rows':>12} {'cost_micros_total':>20}")
    print(f"  {'-'*7} {'-'*20} {'-'*14} {'-'*12} {'-'*12} {'-'*20}")

    for acct in google_accounts:
        acct_id = acct["id"]

        # Campaign performance row count + total cost_micros
        camp = conn.execute(
            """
            SELECT COUNT(*) AS n, COALESCE(SUM(cost_micros), 0) AS total_cost
            FROM campaign_performance_daily
            WHERE account_id = ?
            """,
            (acct_id,),
        ).fetchone()

        # Keyword performance row count (joined via keyword_instances)
        kw_row = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM keyword_performance_daily kpd
            JOIN keyword_instances ki ON ki.id = kpd.instance_id
            WHERE ki.account_id = ?
            """,
            (acct_id,),
        ).fetchone()

        print(f"  {acct_id:>7} {acct['account_name']:<20} "
              f"{acct['platform_account_id']:<14} "
              f"{camp['n']:>12,} {kw_row['n']:>12,} "
              f"{int(camp['total_cost'] or 0):>20,}")

    print()
    print("  → If you saved a 'before' snapshot of these numbers, compare now.")
    print("  → Any change indicates the Microsoft loaders accidentally touched")
    print("    Google data (which would be a bug worth investigating).")
    print()

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
