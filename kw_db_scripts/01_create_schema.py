"""
01_create_schema.py  [v5 — corrected keyword_instances composite key]
-------------------
Creates the SQLite database and all tables for the paid search keyword
tracking system. Safe to run on a fresh database. On an existing database,
drops and rebuilds keyword_instances with the corrected unique constraint
(all other tables are left untouched via IF NOT EXISTS).

Changes from v4:
  - keyword_instances UNIQUE constraint corrected from
      UNIQUE (account_id, criterion_id)
    to
      UNIQUE (account_id, ad_group_id, criterion_id)
    This is the root fix for the last-write-wins campaign attribution bug.
    The composite (ad_group_id, criterion_id) matches the Google Ads API
    resource name format: customers/{cid}/keywordViews/{ad_group_id}~{criterion_id}
  - ad_group_id promoted to NOT NULL (required for composite key integrity)
  - campaign_id column retained and written at sync time by
    03_sync_keyword_performance.py (no longer backfilled post-hoc)
  - Migration pass removed for keyword_instances columns — table is rebuilt
    from scratch so ALTER TABLE additions are no longer needed
  - keyword_performance_daily gains campaign_id column (written at insert
    time by the sync scripts; eliminates need for migrate_kpd_add_campaign_id.py)
  - search_query_performance gains campaign_id column (same pattern;
    eliminates need for migrate_sqp_add_campaign_id.py)

Seeded accounts:
  Home Insurance (8886502147)
  Medicare       (5310820901)

Usage:
    conda activate paid-search
    python kw_db_scripts/01_create_schema.py
"""

import sqlite3
import os
import logging
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Step 1: Tables that are safe to CREATE IF NOT EXISTS
# (accounts, business_units, and all other non-rebuilt tables)
# ---------------------------------------------------------------------------

STABLE_TABLE_STATEMENTS = [

    # ------------------------------------------------------------------
    # business_units
    # Master reference for vertical x domain combinations.
    # Seeded by campaign_db_scripts/00_seed_business_units.py.
    # ------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS business_units (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        vertical    TEXT NOT NULL,
        domain      TEXT NOT NULL,
        label       TEXT NOT NULL,
        UNIQUE (vertical, domain)
    )
    """,

    # ------------------------------------------------------------------
    # accounts
    # Master list of every ad account across all platforms.
    # business_unit_id wired by campaign_db_scripts/00_seed_business_units.py.
    # ------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS accounts (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        account_name        TEXT    NOT NULL,
        platform_account_id TEXT    NOT NULL,
        platform            TEXT    NOT NULL DEFAULT 'google',
        mcc_id              TEXT,
        business_unit_id    INTEGER REFERENCES business_units(id),
        UNIQUE (platform_account_id, platform)
    )
    """,

    # ------------------------------------------------------------------
    # keyword_identities
    # Cross-account, cross-platform identity layer.
    # One row per unique (normalized_text + match_type) combination
    # regardless of account, platform, campaign, or ad group.
    #
    # This is the master aggregation level. "Home insurance quote" EXACT
    # has exactly one identity row regardless of how many accounts,
    # campaigns, or ad groups deploy it. All tagging (vertical,
    # brand_category, modifier_brand) lives here because those attributes
    # belong to the keyword text itself, not any specific deployment.
    #
    # Tagging columns are added by 09_tag_keyword_identities.py.
    # ------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS keyword_identities (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        normalized_text TEXT    NOT NULL,
        match_type      TEXT    NOT NULL,
        identity_key    TEXT    NOT NULL UNIQUE
        -- Format: "normalized_text::MATCH_TYPE"
        -- e.g.   "home insurance quote::EXACT"
    )
    """,

    # ------------------------------------------------------------------
    # negative_keywords
    # ------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS negative_keywords (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id          INTEGER NOT NULL REFERENCES accounts(id),
        keyword_text        TEXT    NOT NULL,
        match_type          TEXT    NOT NULL,
        level               TEXT    NOT NULL,
        campaign_name       TEXT    NOT NULL,
        ad_group_name       TEXT,
        first_seen_date     TEXT,
        last_seen_date      TEXT,
        platform            TEXT    NOT NULL DEFAULT 'google',

        UNIQUE (account_id, platform, level, campaign_name,
                ad_group_name, keyword_text, match_type)
    )
    """,

    # ------------------------------------------------------------------
    # conversion_actions
    # Per-account registry. Drives conversion routing in 03 and 05.
    #
    # include_mode values:
    #   "primary"          store count + value in primary_conv_*
    #   "secondary_count"  store count only in secondary_conv_count
    #   "monitor"          all_conv_* only
    #   "excluded"         ignore entirely
    # ------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS conversion_actions (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id          INTEGER NOT NULL REFERENCES accounts(id),
        conversion_name     TEXT    NOT NULL,
        include_mode        TEXT    NOT NULL,
        notes               TEXT,
        UNIQUE (account_id, conversion_name)
    )
    """,

    # ------------------------------------------------------------------
    # geo_target_constants
    # Shared dimension for geo decoding across keyword and campaign tables.
    # ------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS geo_target_constants (
        criterion_id        INTEGER PRIMARY KEY,
        name                TEXT    NOT NULL,
        canonical_name      TEXT,
        country_code        TEXT,
        target_type         TEXT
    )
    """,

    # ------------------------------------------------------------------
    # search_queries
    # Identity table: one row per unique normalized query text,
    # cross-platform and cross-account.
    # ------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS search_queries (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        query_text          TEXT    NOT NULL,
        normalized_text     TEXT    NOT NULL UNIQUE,
        canonical_query_id  INTEGER REFERENCES search_queries(id),
        theme               TEXT,
        theme_confidence    TEXT    DEFAULT 'untagged',
        first_seen_date     TEXT,
        last_seen_date      TEXT,
        first_seen_platform TEXT,
        notes               TEXT
    )
    """,
]


# ---------------------------------------------------------------------------
# Step 2: Drop and rebuild keyword_instances with corrected composite key
# ---------------------------------------------------------------------------
# This table is rebuilt from scratch on every run of this script to ensure
# the UNIQUE constraint is correct. All downstream tables that reference
# keyword_instances.id (keyword_performance_daily, search_query_performance)
# are also dropped and rebuilt to preserve referential integrity.
#
# WHY THE CONSTRAINT CHANGED
# --------------------------
# Google Ads API identifies keywords via a composite key:
#   customers/{customer_id}/keywordViews/{ad_group_id}~{criterion_id}
# criterion_id alone is NOT globally unique within an account — the same
# criterion_id can appear in multiple ad groups (e.g. "Home-Generic-Core"
# runs in TIER TWO, Florida CPA, NC, and 100+ state campaigns).
# The old UNIQUE (account_id, criterion_id) caused last-write-wins
# attribution: whichever campaign synced last overwrote the campaign_id
# for that criterion_id on all other campaigns.
# The corrected UNIQUE (account_id, ad_group_id, criterion_id) gives each
# campaign deployment its own row with its own correct campaign_id.
# ---------------------------------------------------------------------------

REBUILD_DROPS = [
    # Drop in FK-safe order: fact tables before keyword_instances
    "DROP TABLE IF EXISTS search_query_performance",
    "DROP TABLE IF EXISTS keyword_performance_daily",
    "DROP TABLE IF EXISTS keyword_instances",
]

REBUILD_CREATES = [

    # ------------------------------------------------------------------
    # keyword_instances  [v5 — corrected composite key]
    # One row per keyword deployment: a specific (ad_group_id, criterion_id)
    # in a specific account.
    #
    # Multiple instances share the same identity_id — that is the design
    # intent. "Home insurance quote" EXACT is one identity but may have
    # many instances across accounts, campaigns, and ad groups.
    #
    # ad_group_id: NOT NULL — required for the composite unique key.
    #   Google Ads: ad_group.id from the API (stored as TEXT to avoid
    #   integer overflow; Google IDs exceed SQLite safe integer range).
    #   Microsoft: ad group ID from the export CSV.
    #
    # campaign_id: still TEXT (Google campaign.id / MS campaign ID).
    #   Join key to campaigns.campaign_id in campaign_db_scripts.
    #   Written at sync time by 02_sync_keyword_structure.py.
    #   No longer backfilled post-hoc.
    #
    # criterion_id sources:
    #   Google:    ad_group_criterion.criterion_id from the API
    #   Microsoft: SHA-256 hash of (account_id+campaign+adgroup+text+match),
    #              first 16 chars
    # ------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS keyword_instances (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id          INTEGER NOT NULL REFERENCES accounts(id),
        identity_id         INTEGER NOT NULL REFERENCES keyword_identities(id),

        criterion_id        TEXT    NOT NULL,
        keyword_text        TEXT    NOT NULL,
        match_type          TEXT    NOT NULL,

        -- Campaign bridge columns (Google + Microsoft)
        -- Join to campaign_db_scripts tables via:
        --   JOIN campaigns c  ON ki.campaign_id  = c.campaign_id
        --   JOIN ad_groups ag ON ki.ad_group_id  = ag.ad_group_id
        campaign_id         TEXT,
        ad_group_id         TEXT    NOT NULL,

        -- Denormalized names for readability without a join
        campaign_name       TEXT    NOT NULL,
        ad_group_name       TEXT    NOT NULL,

        status              TEXT,
        cpc_bid_micros      REAL,
        quality_score       INTEGER,

        first_seen_date     TEXT,
        last_seen_date      TEXT,
        platform            TEXT    NOT NULL DEFAULT 'google',

        -- Corrected composite key: matches Google Ads API resource name
        -- customers/{cid}/keywordViews/{ad_group_id}~{criterion_id}
        UNIQUE (account_id, ad_group_id, criterion_id)
    )
    """,

    # ------------------------------------------------------------------
    # keyword_performance_daily
    # Time-series core. One row per (instance x date x device x geo).
    #
    # campaign_id is written at insert time by 03_sync_keyword_performance.py
    # and sync_keyword_perf_by_campaign.py. Since keyword_instances now has
    # one correct row per (account, ad_group, criterion) deployment, the
    # instance_id -> campaign_id mapping is reliable by construction.
    # The migrate_kpd_add_campaign_id.py and fix_kpd_tier_two_campaign_id.py
    # workaround scripts are no longer needed.
    #
    # Segmentation sentinel values:
    #   device='ALL', geo_state='ALL'   clean daily total (use for revenue)
    #   device='MOBILE' etc             device breakdown
    # NEVER SUM across segmentation types.
    # ------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS keyword_performance_daily (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        instance_id             INTEGER NOT NULL REFERENCES keyword_instances(id),
        campaign_id             TEXT,
        report_date             TEXT    NOT NULL,
        device                  TEXT    NOT NULL DEFAULT 'ALL',
        geo_state               TEXT    NOT NULL DEFAULT 'ALL',
        geo_target_constant_id  INTEGER,

        clicks                  INTEGER NOT NULL DEFAULT 0,
        impressions             INTEGER NOT NULL DEFAULT 0,
        cost_micros             REAL    NOT NULL DEFAULT 0,

        -- Primary revenue metric (account-specific routing via conversion_actions)
        primary_conv_count      REAL    NOT NULL DEFAULT 0,
        primary_conv_value      REAL    NOT NULL DEFAULT 0,

        -- Count-only engagement signal (e.g. zip submit)
        secondary_conv_count    REAL    NOT NULL DEFAULT 0,

        -- Raw Google all_conversions total (UI sanity check only)
        all_conv_count          REAL    NOT NULL DEFAULT 0,
        all_conv_value          REAL    NOT NULL DEFAULT 0,

        UNIQUE (instance_id, report_date, device, geo_state)
    )
    """,

    # ------------------------------------------------------------------
    # search_query_performance
    # Fact table: daily grain per (query x matched_keyword x date x device).
    #
    # campaign_id written at insert time by sync_search_queries_by_campaign.py.
    # Eliminates need for migrate_sqp_add_campaign_id.py.
    # ------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS search_query_performance (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        query_id             INTEGER NOT NULL REFERENCES search_queries(id),
        instance_id          INTEGER NOT NULL REFERENCES keyword_instances(id),
        campaign_id          TEXT,
        platform             TEXT    NOT NULL,
        report_date          TEXT    NOT NULL,
        device               TEXT    NOT NULL,
        match_type_served    TEXT,
        impressions          INTEGER DEFAULT 0,
        clicks               INTEGER DEFAULT 0,
        cost_micros          REAL    DEFAULT 0,
        primary_conv_count   REAL    DEFAULT 0,
        primary_conv_value   REAL    DEFAULT 0,
        secondary_conv_count REAL    DEFAULT 0,
        all_conv_count       REAL    DEFAULT 0,
        all_conv_value       REAL    DEFAULT 0,
        UNIQUE (query_id, instance_id, report_date, device)
    )
    """,
]


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------

INDEX_STATEMENTS = [
    # keyword_identities
    "CREATE INDEX IF NOT EXISTS idx_identity_key            ON keyword_identities(identity_key)",

    # keyword_instances — composite key components individually indexed
    # for efficient filtering by any single dimension
    "CREATE INDEX IF NOT EXISTS idx_instances_identity      ON keyword_instances(identity_id)",
    "CREATE INDEX IF NOT EXISTS idx_instances_account       ON keyword_instances(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_instances_campaign      ON keyword_instances(campaign_id)",
    "CREATE INDEX IF NOT EXISTS idx_instances_adgroup       ON keyword_instances(ad_group_id)",
    "CREATE INDEX IF NOT EXISTS idx_instances_criterion     ON keyword_instances(criterion_id)",

    # keyword_performance_daily
    "CREATE INDEX IF NOT EXISTS idx_kpd_instance_date      ON keyword_performance_daily(instance_id, report_date)",
    "CREATE INDEX IF NOT EXISTS idx_kpd_campaign_date      ON keyword_performance_daily(campaign_id, report_date)",
    "CREATE INDEX IF NOT EXISTS idx_kpd_date               ON keyword_performance_daily(report_date)",
    "CREATE INDEX IF NOT EXISTS idx_kpd_device             ON keyword_performance_daily(device)",
    "CREATE INDEX IF NOT EXISTS idx_kpd_geo                ON keyword_performance_daily(geo_state)",

    # negative_keywords
    "CREATE INDEX IF NOT EXISTS idx_negatives_account      ON negative_keywords(account_id)",

    # accounts
    "CREATE INDEX IF NOT EXISTS idx_accounts_business_unit ON accounts(business_unit_id)",

    # search_queries
    "CREATE INDEX IF NOT EXISTS idx_sq_normalized          ON search_queries(normalized_text)",
    "CREATE INDEX IF NOT EXISTS idx_sq_canonical           ON search_queries(canonical_query_id)",

    # search_query_performance
    "CREATE INDEX IF NOT EXISTS idx_sqp_query_date         ON search_query_performance(query_id, report_date)",
    "CREATE INDEX IF NOT EXISTS idx_sqp_campaign_date      ON search_query_performance(campaign_id, report_date)",
    "CREATE INDEX IF NOT EXISTS idx_sqp_platform_date      ON search_query_performance(platform, report_date)",
    "CREATE INDEX IF NOT EXISTS idx_sqp_instance_date      ON search_query_performance(instance_id, report_date)",
    "CREATE INDEX IF NOT EXISTS idx_sqp_date_device        ON search_query_performance(report_date, device)",
]


# ---------------------------------------------------------------------------
# Accounts migration
# Adds business_unit_id to accounts if not already present.
# Safe no-op if the column already exists.
# ---------------------------------------------------------------------------

ACCOUNTS_MIGRATION = ("accounts", "business_unit_id",
                      "INTEGER REFERENCES business_units(id)")


def apply_accounts_migration(conn):
    cursor = conn.cursor()
    existing = {r[1] for r in cursor.execute("PRAGMA table_info(accounts)")}
    if ACCOUNTS_MIGRATION[1] not in existing:
        cursor.execute(
            f"ALTER TABLE accounts ADD COLUMN "
            f"{ACCOUNTS_MIGRATION[1]} {ACCOUNTS_MIGRATION[2]}"
        )
        conn.commit()
        return True
    return False


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------

SEED_STATEMENTS = [

    # ==========================================================================
    # HOME INSURANCE (8886502147) — MCC 6983050143
    # ==========================================================================
    """
    INSERT OR IGNORE INTO accounts
        (account_name, platform_account_id, platform, mcc_id)
    VALUES ('Home Insurance', '8886502147', 'google', '6983050143')
    """,

    # PRIMARY: QW Home Offline Conversions — $3,551,385 total. Fires 98% of days.
    """
    INSERT OR IGNORE INTO conversion_actions (account_id, conversion_name, include_mode, notes)
    VALUES (
        (SELECT id FROM accounts WHERE platform_account_id='8886502147' AND platform='google'),
        'QW Home Offline Conversions', 'primary',
        'Primary revenue KPI. $3,551,385 total. Fires 98% of days. QW offline upload pipeline.'
    )
    """,

    # SECONDARY COUNT: Zip Submit
    """
    INSERT OR IGNORE INTO conversion_actions (account_id, conversion_name, include_mode, notes)
    VALUES (
        (SELECT id FROM accounts WHERE platform_account_id='8886502147' AND platform='google'),
        'Zip Submit', 'secondary_count',
        'Top-of-funnel count signal. $0 value. Open Q H2: should value be assigned?'
    )
    """,

    # MONITOR: EQ Home Conversions with Value
    """
    INSERT OR IGNORE INTO conversion_actions (account_id, conversion_name, include_mode, notes)
    VALUES (
        (SELECT id FROM accounts WHERE platform_account_id='8886502147' AND platform='google'),
        'EQ Home Conversions with Value', 'monitor',
        'Nearly dormant. $4,908 total, fired 1 day only. Open Q H1: EverQuote integration active?'
    )
    """,

    # EXCLUDED: QW Home GBRAIDS
    """
    INSERT OR IGNORE INTO conversion_actions (account_id, conversion_name, include_mode, notes)
    VALUES (
        (SELECT id FROM accounts WHERE platform_account_id='8886502147' AND platform='google'),
        'QW Home GBRAIDS', 'excluded',
        'Bidding signal only. $53,552 total. DO NOT REMOVE — removing disrupts Smart Bidding.'
    )
    """,

    # EXCLUDED: QW Submit Lead
    """
    INSERT OR IGNORE INTO conversion_actions (account_id, conversion_name, include_mode, notes)
    VALUES (
        (SELECT id FROM accounts WHERE platform_account_id='8886502147' AND platform='google'),
        'QW Submit Lead', 'excluded',
        'Dead action. $0 total, never fired in audit range.'
    )
    """,

    # ==========================================================================
    # MEDICARE (5310820901) — MCC 6983050143
    # Conversion routing provisional pending M1-M4 Ringba resolution.
    # Update include_mode values after M1-M4 confirmed, then re-run
    # 03_sync_keyword_performance.py --backfill for the Medicare account.
    # ==========================================================================
    """
    INSERT OR IGNORE INTO accounts
        (account_name, platform_account_id, platform, mcc_id)
    VALUES ('Medicare', '5310820901', 'google', '6983050143')
    """,

    # PRIMARY: RB-Ringba - Medicare Advantage
    """
    INSERT OR IGNORE INTO conversion_actions (account_id, conversion_name, include_mode, notes)
    VALUES (
        (SELECT id FROM accounts WHERE platform_account_id='5310820901' AND platform='google'),
        'RB-Ringba - Medicare Advantage', 'primary',
        'Provisional primary. $4,762,130 total. 68% of days. Pending M1: confirm not double-counted with RB-Ringba.'
    )
    """,

    # MONITOR: RB-Ringba
    """
    INSERT OR IGNORE INTO conversion_actions (account_id, conversion_name, include_mode, notes)
    VALUES (
        (SELECT id FROM accounts WHERE platform_account_id='5310820901' AND platform='google'),
        'RB-Ringba', 'monitor',
        'Possible duplicate of primary. $2,983,943 total. Fires same days. Open Q M1.'
    )
    """,

    # MONITOR: Phone Call
    """
    INSERT OR IGNORE INTO conversion_actions (account_id, conversion_name, include_mode, notes)
    VALUES (
        (SELECT id FROM accounts WHERE platform_account_id='5310820901' AND platform='google'),
        'Phone Call', 'monitor',
        'Triple-counting risk. $2,604,090 total. Open Q M2: Google-native or same Ringba calls?'
    )
    """,

    # MONITOR: RB-DNI_Calls
    """
    INSERT OR IGNORE INTO conversion_actions (account_id, conversion_name, include_mode, notes)
    VALUES (
        (SELECT id FROM accounts WHERE platform_account_id='5310820901' AND platform='google'),
        'RB-DNI_Calls', 'monitor',
        'Likely website visitor calls. $525,848 total. Open Q M3.'
    )
    """,

    # MONITOR: Policy Sale 30 Min
    """
    INSERT OR IGNORE INTO conversion_actions (account_id, conversion_name, include_mode, notes)
    VALUES (
        (SELECT id FROM accounts WHERE platform_account_id='5310820901' AND platform='google'),
        'RB-RB - Medicare Advantage - Policy Sale - 30 Min', 'monitor',
        'Call quality threshold. $11,898 total. 30-min filter on Ringba pool. Open Q M4.'
    )
    """,

    # MONITOR: Policy Sales 40+ Min
    """
    INSERT OR IGNORE INTO conversion_actions (account_id, conversion_name, include_mode, notes)
    VALUES (
        (SELECT id FROM accounts WHERE platform_account_id='5310820901' AND platform='google'),
        'Policy Sales Conversions (40+ Min Ringba Calls)', 'monitor',
        'Highest quality call signal. $5,250 total. 40-min filter. Open Q M4.'
    )
    """,

    # MONITOR: Billable Calls
    """
    INSERT OR IGNORE INTO conversion_actions (account_id, conversion_name, include_mode, notes)
    VALUES (
        (SELECT id FROM accounts WHERE platform_account_id='5310820901' AND platform='google'),
        'Billable Calls', 'monitor',
        'Unclear threshold. $7,501 total. Open Q M4.'
    )
    """,

    # MONITOR: Ringba DNI - Medicare Advantage
    """
    INSERT OR IGNORE INTO conversion_actions (account_id, conversion_name, include_mode, notes)
    VALUES (
        (SELECT id FROM accounts WHERE platform_account_id='5310820901' AND platform='google'),
        'Ringba DNI - Medicare Advantage', 'monitor',
        'Low activity. $14,619 total. Fires 2% of days.'
    )
    """,
]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def create_schema():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    log.info(f"Database : {DB_PATH}")
    log.info("")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = OFF")   # OFF during rebuild only
    conn.execute("PRAGMA journal_mode = WAL")
    cursor = conn.cursor()

    # ── Step 1: Stable tables (IF NOT EXISTS — safe on existing DB) ───
    log.info("── Step 1: Stable tables (CREATE IF NOT EXISTS) ──────────────")
    for stmt in STABLE_TABLE_STATEMENTS:
        tokens = [w for w in stmt.split()
                  if w.upper() not in ("CREATE", "TABLE", "IF", "NOT", "EXISTS", "")]
        name = tokens[0] if tokens else "unknown"
        cursor.execute(stmt)
        log.info(f"  OK  {name}")

    # ── Step 2: accounts migration ────────────────────────────────────
    log.info("")
    log.info("── Step 2: accounts migration ────────────────────────────────")
    added = apply_accounts_migration(conn)
    if added:
        log.info("  ADDED  accounts.business_unit_id")
    else:
        log.info("  OK     accounts.business_unit_id already present")

    # ── Step 3: Drop and rebuild keyword_instances + fact tables ──────
    log.info("")
    log.info("── Step 3: Rebuild keyword_instances + fact tables ───────────")
    log.info("  Dropping search_query_performance, keyword_performance_daily,")
    log.info("  keyword_instances (FK-safe order) ...")

    for stmt in REBUILD_DROPS:
        table = stmt.split()[-1]
        cursor.execute(stmt)
        log.info(f"  DROPPED  {table}")

    log.info("")
    log.info("  Creating keyword_instances with corrected composite key ...")
    log.info("  UNIQUE (account_id, ad_group_id, criterion_id)")
    log.info("")

    for stmt in REBUILD_CREATES:
        tokens = [w for w in stmt.split()
                  if w.upper() not in ("CREATE", "TABLE", "IF", "NOT", "EXISTS", "")]
        name = tokens[0] if tokens else "unknown"
        cursor.execute(stmt)
        log.info(f"  CREATED  {name}")

    conn.commit()

    # ── Step 4: Indexes ───────────────────────────────────────────────
    log.info("")
    log.info("── Step 4: Indexes ───────────────────────────────────────────")
    for stmt in INDEX_STATEMENTS:
        name = stmt.split()[5]
        cursor.execute(stmt)
        log.info(f"  OK  {name}")

    # ── Step 5: Seed reference data ───────────────────────────────────
    log.info("")
    log.info("── Step 5: Seed accounts + conversion_actions ────────────────")
    conn.execute("PRAGMA foreign_keys = ON")   # re-enable for seed inserts
    for stmt in SEED_STATEMENTS:
        cursor.execute(stmt)
    conn.commit()
    log.info("  OK  Home Insurance (8886502147) seeded")
    log.info("  OK  Medicare (5310820901) seeded")

    # ── Step 6: Verification ──────────────────────────────────────────
    log.info("")
    log.info("── Step 6: Table row counts ──────────────────────────────────")
    tables = [
        "business_units",
        "accounts",
        "conversion_actions",
        "keyword_identities",
        "keyword_instances",
        "keyword_performance_daily",
        "negative_keywords",
        "geo_target_constants",
        "search_queries",
        "search_query_performance",
    ]
    for table in tables:
        n = cursor.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        log.info(f"  {table:<35} {n:>6} rows")

    # Confirm new constraint is in place
    log.info("")
    log.info("── Step 7: Confirm keyword_instances constraint ───────────────")
    idx_rows = cursor.execute("""
        SELECT name, sql FROM sqlite_master
        WHERE type = 'index'
          AND tbl_name = 'keyword_instances'
    """).fetchall()
    for name, sql in idx_rows:
        log.info(f"  {name}")
    ki_sql = cursor.execute("""
        SELECT sql FROM sqlite_master
        WHERE type = 'table' AND name = 'keyword_instances'
    """).fetchone()
    if ki_sql and "ad_group_id, criterion_id" in ki_sql[0]:
        log.info("  UNIQUE (account_id, ad_group_id, criterion_id) — CONFIRMED")
    else:
        log.warning("  WARNING: composite key not found — check DDL above")

    log.info("")
    log.info("── Seeded conversion actions ──────────────────────────────────")
    rows = cursor.execute("""
        SELECT a.account_name, ca.conversion_name, ca.include_mode
        FROM   conversion_actions ca
        JOIN   accounts a ON ca.account_id = a.id
        ORDER  BY a.account_name, ca.include_mode, ca.conversion_name
    """).fetchall()
    current_account = None
    for acct_name, conv_name, mode in rows:
        if acct_name != current_account:
            log.info(f"  [{acct_name}]")
            current_account = acct_name
        log.info(f"    [{mode:<16}]  {conv_name}")

    conn.close()

    size_kb = os.path.getsize(DB_PATH) / 1024
    log.info("")
    log.info(f"Database size : {size_kb:.1f} KB")
    log.info(f"Database path : {DB_PATH}")
    log.info("")
    log.info("Schema v5 complete. Next step:")
    log.info("  python campaign_db_scripts/01_create_campaign_tables.py")


if __name__ == "__main__":
    create_schema()
