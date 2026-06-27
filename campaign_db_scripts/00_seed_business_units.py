"""
00_seed_business_units.py  [v2 — Microsoft account wired]
==========================
Campaign Structure Analytics — Quote.com Google Ads
Folder: C:\\Projects\\google-ads-ops\\campaign_db_scripts\\

Fully idempotent — safe to re-run at any time.

What this script does:
  1. Creates the business_units table (IF NOT EXISTS)
  2. Adds business_unit_id column to accounts (IF NOT EXISTS)
  3. Seeds all known business units
  4. Wires business_unit_id on every accounts row in ACCOUNT_WIRING

Changes from v1:
  - Microsoft Home Insurance account (F149SN63, platform='microsoft')
    added to ACCOUNT_WIRING, wired to home / quote.com (business_unit_id=1)
  - Next step pointer updated to 02_sync_campaigns_adgroups.py (campaign
    tables are now created by 01_create_campaign_tables.py before this runs)
  - Note on run order: this script should run AFTER 01_create_campaign_tables.py
    so that all tables exist before the first sync

────────────────────────────────────────────────────────────────────────────────
BUSINESS UNIT MODEL

A business_unit represents one vertical × domain combination — a distinct
property that runs paid search independently, with its own conversion pipeline,
keyword strategy, and potentially its own P&L.

    vertical  : the insurance product (home / auto / medicare / term_life / ...)
    domain    : the web property traffic is attributed to
    label     : human-readable display name for reports and dashboards

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
    2. Add a row to ACCOUNT_WIRING below if an account already exists
    3. Re-run this script — it is fully idempotent

────────────────────────────────────────────────────────────────────────────────
HOW OTHER SCRIPTS USE THIS

After this script runs, any analysis query can filter semantically:

    -- All home insurance accounts, any platform:
    JOIN business_units bu ON a.business_unit_id = bu.id
    WHERE bu.vertical = 'home'

    -- All quote.com properties:
    WHERE bu.domain = 'quote.com'

    -- Cross-platform keyword aggregation (Google + Microsoft same identity):
    SELECT ki_id.normalized_text, a.platform, SUM(kpd.cost_micros)/1e6
    FROM keyword_identities ki_id
    JOIN keyword_instances ki  ON ki.identity_id = ki_id.id
    JOIN accounts a            ON a.id = ki.account_id
    JOIN keyword_performance_daily kpd ON kpd.instance_id = ki.id
    JOIN business_units bu     ON bu.id = a.business_unit_id
    WHERE bu.vertical = 'home' AND kpd.device = 'ALL'
    GROUP BY ki_id.id, a.platform

────────────────────────────────────────────────────────────────────────────────
"""

import sqlite3
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
# INSERT OR IGNORE on UNIQUE (vertical, domain) — re-runs never overwrite.

BUSINESS_UNITS_SEED = [
    # (vertical,    domain,               label)
    ("home",       "quote.com",           "Home Insurance — quote.com"),
    ("auto",       "autoinsurance.org",   "Auto Insurance — autoinsurance.org"),
    ("auto",       "auto.quote.com",      "Auto Insurance — auto.quote.com"),
    ("medicare",   "quote.com",           "Medicare — quote.com"),
    ("term_life",  "quote.com",           "Term Life — quote.com"),
]

# Maps (platform_account_id, platform) → (vertical, domain) for wiring.
# platform disambiguates Google vs Microsoft accounts.
# Add new accounts here as they are created in the accounts table.
ACCOUNT_WIRING = [
    # ── Google — MCC-managed (MCC 6983050143) ─────────────────────────
    ("8886502147",  "google",    "home",      "quote.com"),
    ("3863660890",  "google",    "auto",      "autoinsurance.org"),
    ("5310820901",  "google",    "medicare",  "quote.com"),
    ("1671155176",  "google",    "term_life", "quote.com"),

    # ── Google — standalone (own login_customer_id, not under MCC) ────
    ("1006729211",  "google",    "auto",      "auto.quote.com"),

    # ── Microsoft ─────────────────────────────────────────────────────
    # Home Insurance Microsoft account — wired to home / quote.com.
    # Keyword data loaded via kw_db_scripts/04_load_microsoft_csv.py.
    # Campaign sync not yet available (no Microsoft Ads API integration).
    ("F149SN63",    "microsoft", "home",      "quote.com"),
]


# ── Database connection ────────────────────────────────────────────────────────

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


# ── Step 1: Create business_units table ───────────────────────────────────────

CREATE_BUSINESS_UNITS = """
    CREATE TABLE IF NOT EXISTS business_units (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        vertical    TEXT NOT NULL,
        domain      TEXT NOT NULL,
        label       TEXT NOT NULL,
        UNIQUE (vertical, domain)
    )
"""

def create_business_units_table(conn):
    conn.execute(CREATE_BUSINESS_UNITS)
    conn.commit()
    log.info("  business_units table ready (created or already existed)")


# ── Step 2: Add business_unit_id column to accounts ───────────────────────────

def add_business_unit_id_to_accounts(conn):
    cursor = conn.cursor()
    existing_cols = [
        row[1] for row in cursor.execute("PRAGMA table_info(accounts)").fetchall()
    ]
    if "business_unit_id" in existing_cols:
        log.info("  accounts.business_unit_id already exists — skipping ALTER TABLE")
        return
    conn.execute("""
        ALTER TABLE accounts
        ADD COLUMN business_unit_id INTEGER REFERENCES business_units(id)
    """)
    conn.commit()
    log.info("  Added business_unit_id column to accounts table")


# ── Step 3: Seed business_units rows ──────────────────────────────────────────

def seed_business_units(conn) -> int:
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
    Returns summary: wired count, unmatched BU entries, missing account entries.
    """
    cursor = conn.cursor()
    results = {"wired": 0, "no_bu_match": [], "no_acct": []}

    for platform_account_id, platform, vertical, domain in ACCOUNT_WIRING:

        bu_row = cursor.execute("""
            SELECT id FROM business_units
            WHERE vertical = ? AND domain = ?
        """, (vertical, domain)).fetchone()

        if not bu_row:
            results["no_bu_match"].append((platform_account_id, vertical, domain))
            continue

        acct_row = cursor.execute("""
            SELECT id FROM accounts
            WHERE platform_account_id = ? AND platform = ?
        """, (platform_account_id, platform)).fetchone()

        if not acct_row:
            # Account not yet in DB — will wire on next run after account is added
            results["no_acct"].append((platform_account_id, platform))
            continue

        cursor.execute("""
            UPDATE accounts
            SET business_unit_id = ?
            WHERE id = ?
        """, (bu_row["id"], acct_row["id"]))
        results["wired"] += 1

    conn.commit()
    return results


# ── Verification ──────────────────────────────────────────────────────────────

def print_verification(conn):
    cursor = conn.cursor()

    log.info("")
    log.info("── Verification: accounts × business_units ───────────────────")

    rows = cursor.execute("""
        SELECT
            a.id                    AS acct_id,
            a.platform_account_id,
            a.platform,
            a.account_name,
            bu.vertical,
            bu.domain,
            a.business_unit_id
        FROM accounts a
        LEFT JOIN business_units bu ON a.business_unit_id = bu.id
        ORDER BY bu.vertical NULLS LAST, a.platform, a.platform_account_id
    """).fetchall()

    log.info(f"  {'acct_id':<8} {'platform_account_id':<20} {'platform':<12} "
             f"{'vertical':<12} {'domain':<22} {'bu_id':<6} account_name")
    log.info(f"  {'─'*8} {'─'*20} {'─'*12} {'─'*12} {'─'*22} {'─'*6} {'─'*25}")

    unwired = []
    for r in rows:
        bu_id_str = str(r["business_unit_id"]) if r["business_unit_id"] else "⚠ NULL"
        log.info(
            f"  {r['acct_id']:<8} "
            f"{r['platform_account_id']:<20} "
            f"{r['platform']:<12} "
            f"{(r['vertical'] or ''):<12} "
            f"{(r['domain'] or ''):<22} "
            f"{bu_id_str:<6} "
            f"{r['account_name'] or ''}"
        )
        if not r["business_unit_id"]:
            unwired.append((r["platform_account_id"], r["platform"]))

    log.info("")

    if unwired:
        log.warning(f"  {len(unwired)} account(s) still unwired — "
                    f"add to ACCOUNT_WIRING and re-run:")
        for pid, plat in unwired:
            log.warning(f"    platform_account_id={pid}  platform={plat}")
    else:
        log.info("  All accounts wired to a business unit.")

    log.info("")
    log.info("── business_units table ──────────────────────────────────────")
    bus = cursor.execute(
        "SELECT id, vertical, domain, label FROM business_units ORDER BY id"
    ).fetchall()
    for bu in bus:
        log.info(f"  [{bu['id']}] {bu['vertical']:<12} {bu['domain']:<25} {bu['label']}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info(f"Database : {DB_PATH}")
    log.info("")

    conn = get_db_connection()

    log.info("Step 1 — Create business_units table")
    create_business_units_table(conn)

    log.info("Step 2 — Add business_unit_id column to accounts")
    add_business_unit_id_to_accounts(conn)

    log.info("Step 3 — Seed business_units rows")
    n_inserted = seed_business_units(conn)
    total_bus = conn.execute("SELECT COUNT(*) FROM business_units").fetchone()[0]
    log.info(f"  {n_inserted} new row(s) inserted  |  {total_bus} total business units")

    log.info("Step 4 — Wire business_unit_id on accounts")
    results = wire_accounts(conn)
    log.info(f"  {results['wired']} account(s) wired")

    if results["no_bu_match"]:
        log.warning("  No business_units row found for:")
        for pid, v, d in results["no_bu_match"]:
            log.warning(f"    account={pid}  vertical={v}  domain={d}"
                        f"  → Add to BUSINESS_UNITS_SEED and re-run")

    if results["no_acct"]:
        log.info("  Accounts in ACCOUNT_WIRING not yet in DB (will wire on next run):")
        for pid, plat in results["no_acct"]:
            log.info(f"    platform_account_id={pid}  platform={plat}")

    print_verification(conn)

    conn.close()
    log.info("")
    log.info("Done.")
    log.info("Next step:")
    log.info("  python campaign_db_scripts/02_sync_campaigns_adgroups.py")


if __name__ == "__main__":
    main()
