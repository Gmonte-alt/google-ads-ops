"""
00_precheck_microsoft_load.py
=============================
Pre-flight check before running the Microsoft campaign loader sequence.
Folder: C:\\Projects\\google-ads-ops\\campaign_db_scripts\\

Read-only. Does not modify the database. Run this before:
    01b_extend_campaign_tables.py
    08_load_microsoft_campaigns.py
    09_load_microsoft_campaign_performance.py

WHAT THIS CHECKS

  1. accounts row for Microsoft Home Insurance exists and has the
     expected platform_account_id F149SN63 at an integer id.
     (Expected from prior state: id = 2.)

  2. business_unit_id is wired on the Microsoft account.
     (Expected: 1 — home / quote.com, seeded by
     00_seed_business_units.py.)

  3. Required campaign tables exist:
        campaigns
        ad_groups
        campaign_performance_daily
     (Created by 01_create_campaign_tables.py.)

  4. campaign_performance_network_daily — should NOT exist before
     01b has been run. Existence is fine; it just means 01b already
     ran previously and that script is a no-op this run.

  5. Current Microsoft row counts in each table. Expected on first
     real run: zero across all three. If any are non-zero, it means
     Microsoft campaign data was loaded previously and this run will
     overwrite via INSERT OR REPLACE — not destructive but worth
     knowing.

  6. Google row counts for comparison — confirms the Google sync is
     intact and the database isn't empty.

WHAT THIS DOES NOT CHECK

  - CSV file presence or format (the loader scripts validate those)
  - business_units table contents (assumed already seeded)
  - Google Ads API credentials (not used by Microsoft loaders)

Usage:
    conda activate paid-search
    python campaign_db_scripts/00_precheck_microsoft_load.py

Exit code 0 on all-green. Exit code 1 if any BLOCKER issue is found.
Warnings do not block — they print and continue.
"""

import sqlite3
import sys
from pathlib import Path


DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")

MS_PLATFORM         = "microsoft"
MS_ACCOUNT_NUMBER   = "F149SN63"
EXPECTED_ACCOUNT_ID = 2                # per the schema dump you shared earlier
EXPECTED_BU_ID      = 1                # home / quote.com


# ── Formatting helpers ────────────────────────────────────────────────────────

GREEN = "[OK]    "
WARN  = "[WARN]  "
FAIL  = "[FAIL]  "
INFO  = "[INFO]  "


def line(ch="─", n=65):
    print(ch * n)


def section(title: str):
    print()
    line()
    print(f"  {title}")
    line()


# ── Checks ────────────────────────────────────────────────────────────────────

def check_db_exists() -> bool:
    if not DB_PATH.exists():
        print(f"{FAIL}Database file not found: {DB_PATH}")
        print(f"        Expected an existing paid_search.db. Nothing to check.")
        return False
    print(f"{GREEN}Database file found: {DB_PATH}")
    return True


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def check_microsoft_account(conn) -> tuple[bool, int | None]:
    """
    Confirms accounts row for F149SN63 / microsoft exists.
    Returns (is_blocker_clear, account_id_or_None).
    """
    rows = conn.execute("""
        SELECT id, account_name, platform_account_id, platform,
               mcc_id, business_unit_id
        FROM accounts
        WHERE platform = ?
    """, (MS_PLATFORM,)).fetchall()

    if not rows:
        print(f"{FAIL}No Microsoft account rows found in accounts table.")
        print(f"        Run kw_db_scripts/04_load_microsoft_csv.py once first")
        print(f"        to seed the account row.")
        return False, None

    ms_row = None
    for r in rows:
        marker = "  <- target" if r["platform_account_id"] == MS_ACCOUNT_NUMBER else ""
        print(f"{INFO}  id={r['id']}  "
              f"{r['platform_account_id']}  "
              f"{r['account_name']}  "
              f"bu_id={r['business_unit_id']}{marker}")
        if r["platform_account_id"] == MS_ACCOUNT_NUMBER:
            ms_row = r

    if ms_row is None:
        print(f"{FAIL}No accounts row with platform_account_id={MS_ACCOUNT_NUMBER}")
        return False, None

    # Warning (not blocker) if the id isn't what we expected
    if ms_row["id"] != EXPECTED_ACCOUNT_ID:
        print(f"{WARN}Microsoft accounts.id = {ms_row['id']}, "
              f"expected {EXPECTED_ACCOUNT_ID}.")
        print(f"        The loaders resolve account_id dynamically so this")
        print(f"        won't cause failures, but any hard-coded references")
        print(f"        elsewhere may need updating.")
    else:
        print(f"{GREEN}Microsoft accounts.id = {ms_row['id']} (matches expected)")

    # Warning if business_unit_id isn't set — purely informational, does not
    # affect the loaders. Analysis queries that filter by business_unit will
    # silently drop Microsoft rows if this is NULL.
    if ms_row["business_unit_id"] is None:
        print(f"{WARN}business_unit_id is NULL on the Microsoft account.")
        print(f"        Run 00_seed_business_units.py to wire it.")
    elif ms_row["business_unit_id"] != EXPECTED_BU_ID:
        print(f"{WARN}business_unit_id = {ms_row['business_unit_id']}, "
              f"expected {EXPECTED_BU_ID} (home / quote.com).")
    else:
        print(f"{GREEN}business_unit_id = {ms_row['business_unit_id']} "
              f"(home / quote.com)")

    return True, ms_row["id"]


def check_required_tables(conn) -> bool:
    """All tables the loaders write to must exist. 01_create_campaign_tables.py
    creates the first three; 01b creates the fourth."""
    required = {
        "accounts":                   True,   # blocker if missing
        "campaigns":                  True,
        "ad_groups":                  True,
        "campaign_performance_daily": True,
    }
    network_table = "campaign_performance_network_daily"

    ok = True
    for tbl, is_blocker in required.items():
        exists = conn.execute("""
            SELECT 1 FROM sqlite_master WHERE type='table' AND name=?
        """, (tbl,)).fetchone() is not None
        if exists:
            print(f"{GREEN}{tbl}")
        else:
            tag = FAIL if is_blocker else WARN
            print(f"{tag}{tbl} — does not exist")
            if is_blocker:
                ok = False

    # Network table is expected to NOT exist on first run. Either outcome is
    # fine — just report which.
    net_exists = conn.execute("""
        SELECT 1 FROM sqlite_master WHERE type='table' AND name=?
    """, (network_table,)).fetchone() is not None
    if net_exists:
        print(f"{INFO}{network_table} — already exists "
              f"(01b has been run before; it will be a no-op this run)")
    else:
        print(f"{INFO}{network_table} — does not exist yet "
              f"(01b will create it)")

    return ok


def check_current_microsoft_rows(conn, account_id: int):
    """Row counts per Microsoft-writable table. All zero on first run
    is ideal; non-zero is informational only (INSERT OR REPLACE handles
    re-runs cleanly)."""
    tables = [
        "campaigns",
        "ad_groups",
        "campaign_performance_daily",
    ]
    # Network table is conditional
    if conn.execute("""
        SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='campaign_performance_network_daily'
    """).fetchone():
        tables.append("campaign_performance_network_daily")

    any_nonzero = False
    for tbl in tables:
        n = conn.execute(
            f"SELECT COUNT(*) FROM {tbl} WHERE account_id = ?", (account_id,)
        ).fetchone()[0]
        if n == 0:
            print(f"{GREEN}{tbl:<40} {n:>10,}  (clean slate)")
        else:
            print(f"{INFO}{tbl:<40} {n:>10,}  (will upsert via INSERT OR REPLACE)")
            any_nonzero = True

    if any_nonzero:
        print()
        print(f"{INFO}Non-zero Microsoft rows found. This is not a blocker —")
        print(f"        the loaders use INSERT OR REPLACE and will cleanly")
        print(f"        overwrite existing rows. If you want a clean slate")
        print(f"        instead, delete Microsoft rows with:")
        print(f"          DELETE FROM <table> WHERE account_id = {account_id};")


def check_google_rows_intact(conn):
    """Sanity check that Google data is present — confirms the database
    itself isn't empty or corrupt. Not strictly a precondition for the
    Microsoft loaders, but a good sign that we're pointing at the right DB."""
    rows = conn.execute("""
        SELECT a.platform_account_id, a.account_name,
               (SELECT COUNT(*) FROM campaigns c WHERE c.account_id = a.id) AS n_camp,
               (SELECT COUNT(*) FROM ad_groups ag WHERE ag.account_id = a.id) AS n_ag
        FROM accounts a
        WHERE a.platform = 'google'
        ORDER BY a.platform_account_id
    """).fetchall()

    if not rows:
        print(f"{WARN}No Google accounts found — is this the right database?")
        return

    print(f"{INFO}Google account row counts (for sanity):")
    for r in rows:
        print(f"        {r['platform_account_id']:<12} "
              f"{r['account_name'][:25]:<26} "
              f"campaigns={r['n_camp']:>5,}  ad_groups={r['n_ag']:>7,}")


def print_next_steps(account_id: int | None):
    print()
    line()
    print("  NEXT STEPS (assuming no BLOCKERs above)")
    line()
    if account_id is None:
        print("  Resolve the BLOCKER issues above before proceeding.")
        return
    print()
    print("  1. Create the network table (idempotent — safe if already ran):")
    print("       python campaign_db_scripts/01b_extend_campaign_tables.py")
    print()
    print("  2. Dry-run the campaigns loader (three CSVs):")
    print("       python campaign_db_scripts/08_load_microsoft_campaigns.py \\")
    print("           --campaigns-csv      input/microsoft-home-campaigns-YYYYMMDD.csv \\")
    print("           --campaigns-grid-csv input/microsoft-home-campaigns-grid-YYYYMMDD.csv \\")
    print("           --adgroups-csv       input/microsoft-home-adgroups-YYYYMMDD.csv \\")
    print("           --dry-run")
    print()
    print("  3. If dry-run looks clean, run live (same command, no --dry-run).")
    print()
    print("  4. Dry-run the performance loader:")
    print("       python campaign_db_scripts/09_load_microsoft_campaign_performance.py \\")
    print("           --file input/microsoft-home-camp-perf-YYYYMMDD.csv \\")
    print("           --dry-run")
    print()
    print("  5. If dry-run looks clean, run live.")
    print()
    print("  6. Refresh keyword performance (existing script, unchanged):")
    print("       python kw_db_scripts/04_load_microsoft_csv.py --latest")
    print()
    print("  7. Verify the final state:")
    print("       python campaign_db_scripts/check_sync_network_addition.py")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    print()
    line("═")
    print("  Microsoft loader pre-check")
    print(f"  Database: {DB_PATH}")
    line("═")

    if not check_db_exists():
        return 1

    conn = get_connection()
    blocker = False

    section("1. Microsoft account row")
    ok, account_id = check_microsoft_account(conn)
    blocker = blocker or not ok

    section("2. Required tables exist")
    ok = check_required_tables(conn)
    blocker = blocker or not ok

    if account_id is not None:
        section("3. Current Microsoft row counts (per-table)")
        check_current_microsoft_rows(conn, account_id)

    section("4. Google data sanity check")
    check_google_rows_intact(conn)

    conn.close()

    if blocker:
        print()
        line("═")
        print(f"  {FAIL.strip()}: One or more BLOCKER issues found above.")
        print(f"  Resolve them before running 01b / 08 / 09.")
        line("═")
        return 1

    print_next_steps(account_id)
    print()
    line("═")
    print(f"  {GREEN.strip()}: All checks passed.")
    line("═")
    return 0


if __name__ == "__main__":
    sys.exit(main())
