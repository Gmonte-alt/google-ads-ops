"""
00_seed_business_units.py
==========================
Campaign Structure Analytics — Quote.com Google Ads
Folder: C:\\Projects\\google-ads-ops\\campaign_db_scripts\\

Run this FIRST — before 01_create_campaign_tables.py.
Also run in kw_db_scripts context if you want keyword queries to use
business_unit_id filters (the table lives in the shared paid_search.db).

What this script does:
  1. Creates the business_units table (IF NOT EXISTS — safe to re-run)
  2. Adds business_unit_id column to the existing accounts table (safe if already present)
  3. Seeds all known business units from the current account universe
  4. Wires business_unit_id on every existing accounts row

────────────────────────────────────────────────────────────────────────────────
BUSINESS UNIT MODEL

A business_unit represents one vertical × domain combination — a distinct
property that runs paid search independently, with its own conversion pipeline,
keyword strategy, and potentially its own P&L.

    vertical  : the insurance product (home / auto / medicare / term_life / ...)
    domain    : the web property traffic is attributed to
    label     : human-readable display name for reports and dashboards

Two accounts in the same vertical but different domains are separate business
units (Option A). Example: autoinsurance.org and auto.quote.com are both
vertical='auto' but distinct business units — different domains, different
creative strategies, potentially different conversion pipelines.

Current business unit universe:

    ID  vertical    domain                label
    ──  ─────────── ───────────────────── ──────────────────────────────────
     1  home        quote.com             Home Insurance — quote.com
     2  auto        autoinsurance.org     Auto Insurance — autoinsurance.org
     3  auto        auto.quote.com        Auto Insurance — auto.quote.com
     4  medicare    quote.com             Medicare — quote.com
     5  term_life   quote.com             Term Life — quote.com

Adding a new business unit in the future:
    1. Add a row to BUSINESS_UNITS_SEED below
    2. Add a row to ACCOUNT_WIRING below if a Google/Microsoft account already exists
    3. Re-run this script — it is fully idempotent

────────────────────────────────────────────────────────────────────────────────
HOW OTHER SCRIPTS USE THIS

After this script runs, any analysis query can filter semantically instead of
hardcoding platform_account_id lists:

    -- All home insurance accounts, any platform, any domain:
    JOIN business_units bu ON a.business_unit_id = bu.id
    WHERE bu.vertical = 'home'

    -- All quote.com properties:
    WHERE bu.domain = 'quote.com'

    -- One specific business unit:
    WHERE bu.vertical = 'auto' AND bu.domain = 'autoinsurance.org'

The existing keyword system joins (JOIN accounts a ON ki.account_id = a.id)
are unchanged. business_unit_id is an additive column on accounts — no
existing FK relationships are modified.

────────────────────────────────────────────────────────────────────────────────
"""

import sqlite3
import os
import logging
from pathlib import Path
from dotenv import load_dotenv

# ── Configuration ─────────────────────────────────────────────────────────────
load_dotenv()

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Business unit seed data ───────────────────────────────────────────────────
# Add new rows here as new domains / verticals launch.
# Re-running this script is safe — INSERT OR IGNORE on the UNIQUE (vertical, domain) key.

BUSINESS_UNITS_SEED = [
    # (vertical,    domain,               label)
    ("home",       "quote.com",           "Home Insurance — quote.com"),
    ("auto",       "autoinsurance.org",   "Auto Insurance — autoinsurance.org"),
    ("auto",       "auto.quote.com",      "Auto Insurance — auto.quote.com"),
    ("medicare",   "quote.com",           "Medicare — quote.com"),
    ("term_life",  "quote.com",           "Term Life — quote.com"),
]

# Maps platform_account_id → (vertical, domain) for wiring business_unit_id
# on existing accounts rows. Add new accounts here as they are created.
# platform is included to disambiguate Google vs Microsoft when the same
# platform_account_id string could theoretically exist on both.
ACCOUNT_WIRING = [
    # Google — MCC-managed
    ("8886502147",  "google",    "home",      "quote.com"),
    ("3863660890",  "google",    "auto",      "autoinsurance.org"),
    ("5310820901",  "google",    "medicare",  "quote.com"),
    ("1671155176",  "google",    "term_life", "quote.com"),

    # Google — standalone (own login_customer_id, not under MCC)
    ("1006729211",  "google",    "auto",      "auto.quote.com"),

    # Microsoft — add platform_account_id values here when Microsoft accounts
    # are loaded into the accounts table via kw_db_scripts/04_load_microsoft_csv.py
    # Example (uncomment and update with real account IDs when ready):
    # ("F149SN63",  "microsoft", "home",      "quote.com"),
]


# ── Database connection ────────────────────────────────────────────────────────

def get_db_connection():
    """Matches the connection helper pattern across all kw_db_scripts."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


# ── Step 1: Create business_units table ───────────────────────────────────────

CREATE_BUSINESS_UNITS = """
    CREATE TABLE IF NOT EXISTS business_units (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,

        -- The insurance vertical this unit operates in.
        -- Values: home / auto / medicare / term_life / (others as added)
        vertical    TEXT NOT NULL,

        -- The web property paid search traffic is attributed to.
        -- e.g. quote.com, autoinsurance.org, auto.quote.com
        domain      TEXT NOT NULL,

        -- Human-readable display label for reports and dashboards.
        label       TEXT NOT NULL,

        -- One row per vertical + domain combination.
        UNIQUE (vertical, domain)
    )
"""

def create_business_units_table(conn):
    conn.execute(CREATE_BUSINESS_UNITS)
    conn.commit()
    log.info("  business_units table ready (created or already existed).")


# ── Step 2: Add business_unit_id column to accounts ───────────────────────────

def add_business_unit_id_to_accounts(conn):
    """
    Adds business_unit_id column to the existing accounts table.

    SQLite does not support IF NOT EXISTS on ALTER TABLE ADD COLUMN, so we
    check the existing columns first and skip if already present.
    This keeps the script fully idempotent.
    """
    cursor = conn.cursor()
    existing_cols = [
        row[1] for row in cursor.execute("PRAGMA table_info(accounts)").fetchall()
    ]
    if "business_unit_id" in existing_cols:
        log.info("  accounts.business_unit_id already exists — skipping ALTER TABLE.")
        return

    conn.execute("""
        ALTER TABLE accounts
        ADD COLUMN business_unit_id INTEGER REFERENCES business_units(id)
    """)
    conn.commit()
    log.info("  Added business_unit_id column to accounts table.")


# ── Step 3: Seed business_units rows ──────────────────────────────────────────

def seed_business_units(conn) -> int:
    """
    Inserts business unit rows. INSERT OR IGNORE on UNIQUE (vertical, domain)
    means re-runs are safe and existing rows are never overwritten.
    Returns count of newly inserted rows.
    """
    cursor = conn.cursor()
    inserted = 0

    for vertical, domain, label in BUSINESS_UNITS_SEED:
        cursor.execute("""
            INSERT OR IGNORE INTO business_units (vertical, domain, label)
            VALUES (?, ?, ?)
        """, (vertical, domain, label))
        if cursor.rowcount > 0:
            inserted += 1

    conn.commit()
    return inserted


# ── Step 4: Wire business_unit_id on accounts rows ────────────────────────────

def wire_accounts(conn) -> dict:
    """
    Updates accounts.business_unit_id for each entry in ACCOUNT_WIRING.

    Looks up the business_unit_id by (vertical, domain), then updates the
    accounts row identified by (platform_account_id, platform).

    Returns a summary dict:
      {
        "wired":       count of accounts successfully updated,
        "no_bu_match": list of (platform_account_id, vertical, domain) with no BU row,
        "no_acct":     list of (platform_account_id, platform) not in accounts table,
      }
    """
    cursor = conn.cursor()
    results = {"wired": 0, "no_bu_match": [], "no_acct": []}

    for platform_account_id, platform, vertical, domain in ACCOUNT_WIRING:

        # Look up the business_unit id
        bu_row = cursor.execute("""
            SELECT id FROM business_units
            WHERE vertical = ? AND domain = ?
        """, (vertical, domain)).fetchone()

        if not bu_row:
            results["no_bu_match"].append((platform_account_id, vertical, domain))
            continue

        # Look up the accounts row
        acct_row = cursor.execute("""
            SELECT id FROM accounts
            WHERE platform_account_id = ? AND platform = ?
        """, (platform_account_id, platform)).fetchone()

        if not acct_row:
            results["no_acct"].append((platform_account_id, platform))
            continue

        # Wire the FK
        cursor.execute("""
            UPDATE accounts
            SET business_unit_id = ?
            WHERE id = ?
        """, (bu_row["id"], acct_row["id"]))
        results["wired"] += 1

    conn.commit()
    return results


# ── Verification report ────────────────────────────────────────────────────────

def print_verification(conn):
    """
    Prints the full accounts × business_units join for visual confirmation.
    Any account with business_unit_id = NULL after wiring is flagged.
    """
    cursor = conn.cursor()

    log.info("")
    log.info("── Verification: accounts × business_units ───────────────────────")

    rows = cursor.execute("""
        SELECT
            a.platform_account_id,
            a.platform,
            a.account_name,
            a.mcc_id,
            bu.vertical,
            bu.domain,
            bu.label,
            a.business_unit_id
        FROM accounts a
        LEFT JOIN business_units bu ON a.business_unit_id = bu.id
        ORDER BY bu.vertical, a.platform, a.platform_account_id
    """).fetchall()

    header = f"  {'platform_account_id':<18} {'platform':<12} {'account_name':<25} {'vertical':<12} {'domain':<22} {'bu_id'}"
    log.info(header)
    log.info("  " + "─" * (len(header) - 2))

    unwired = []
    for r in rows:
        bu_id_str = str(r["business_unit_id"]) if r["business_unit_id"] else "⚠ NULL"
        log.info(
            f"  {r['platform_account_id']:<18} "
            f"{r['platform']:<12} "
            f"{(r['account_name'] or '')[:24]:<25} "
            f"{(r['vertical'] or ''):<12} "
            f"{(r['domain'] or ''):<22} "
            f"{bu_id_str}"
        )
        if not r["business_unit_id"]:
            unwired.append(r["platform_account_id"])

    log.info("")

    if unwired:
        log.warning(
            f"  {len(unwired)} account(s) have no business_unit_id — "
            f"add them to ACCOUNT_WIRING and re-run:"
        )
        for pid in unwired:
            log.warning(f"    {pid}")
    else:
        log.info("  All accounts are wired to a business unit.")

    # Print business_units table for reference
    log.info("")
    log.info("── business_units reference ──────────────────────────────────────")
    bus = cursor.execute(
        "SELECT id, vertical, domain, label FROM business_units ORDER BY id"
    ).fetchall()
    for bu in bus:
        log.info(f"  [{bu['id']}] {bu['vertical']:<12} {bu['domain']:<25} {bu['label']}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info(f"Database: {DB_PATH}")
    log.info("")

    conn = get_db_connection()

    log.info("Step 1 — Create business_units table")
    create_business_units_table(conn)

    log.info("Step 2 — Add business_unit_id column to accounts")
    add_business_unit_id_to_accounts(conn)

    log.info("Step 3 — Seed business_units rows")
    n_inserted = seed_business_units(conn)
    total_bus = conn.execute("SELECT COUNT(*) FROM business_units").fetchone()[0]
    log.info(f"  {n_inserted} new row(s) inserted. {total_bus} total business units.")

    log.info("Step 4 — Wire business_unit_id on accounts")
    results = wire_accounts(conn)
    log.info(f"  {results['wired']} account(s) wired.")

    if results["no_bu_match"]:
        log.warning("  No business_units row found for:")
        for pid, v, d in results["no_bu_match"]:
            log.warning(f"    account={pid}  vertical={v}  domain={d}  "
                        f"→ Add to BUSINESS_UNITS_SEED and re-run.")

    if results["no_acct"]:
        log.warning("  No accounts row found for:")
        for pid, plat in results["no_acct"]:
            log.warning(f"    platform_account_id={pid}  platform={plat}  "
                        f"→ Account not yet in DB; will wire on next run after account is added.")

    print_verification(conn)

    conn.close()
    log.info("Done.")
    log.info("Next step: python 01_create_campaign_tables.py")


if __name__ == "__main__":
    main()
