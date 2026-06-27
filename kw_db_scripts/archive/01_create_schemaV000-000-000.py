"""
01_create_schema.py  [v4 — multi-account, campaign bridge, business units]
-------------------
Creates the SQLite database and all tables for the paid search keyword
tracking system. Safe to run multiple times -- uses IF NOT EXISTS throughout
and an explicit migration pass for columns added after initial creation.

Changes from v3:
  - business_units table defined here (authoritative definition shared with
    campaign_db_scripts; no longer relies on ALTER TABLE from 00_seed_business_units.py)
  - accounts.business_unit_id column present from creation
  - keyword_instances gains campaign_id and ad_group_id TEXT columns,
    bridging to campaigns/ad_groups tables in campaign_db_scripts
  - search_queries and search_query_performance defined here (previously
    only created by 05_sync_search_queries.py ensure_schema())
  - Indexes added for campaign_id and ad_group_id on keyword_instances

Seeded accounts:
  Home Insurance (8886502147)
  Medicare       (5310820901)

Usage:
    conda activate paid-search
    python 01_create_schema.py
"""

import sqlite3
import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")


# ---------------------------------------------------------------------------
# Table definitions
# ---------------------------------------------------------------------------

SCHEMA_STATEMENTS = [

    # ------------------------------------------------------------------
    # business_units
    # Master reference for vertical x domain combinations.
    # Defined here so accounts.business_unit_id FK can exist from day one.
    # Seeded by campaign_db_scripts/00_seed_business_units.py.
    #
    # Analysis filter examples:
    #   WHERE bu.vertical = 'medicare'
    #   WHERE bu.domain   = 'quote.com'
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
    #
    # business_unit_id: wired by campaign_db_scripts/00_seed_business_units.py.
    # NULL until that script runs, but the column exists from creation.
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
    # belong to the text itself, not to any specific deployment.
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
    # keyword_instances
    # One row per keyword deployment: a specific criterion_id in a
    # specific account x campaign x ad group.
    #
    # Multiple instances share the same identity_id -- that is the
    # design intent. "Home insurance quote" EXACT is one identity but
    # may have many instances across accounts and ad groups.
    #
    # campaign_id / ad_group_id:
    #   Google Ads numeric IDs stored as TEXT (matches campaign_db_scripts
    #   convention where campaign_id TEXT is the PK on the campaigns table).
    #   These are the join keys to campaign_db_scripts tables:
    #     JOIN campaigns c  ON ki.campaign_id  = c.campaign_id
    #     JOIN ad_groups ag ON ki.ad_group_id  = ag.ad_group_id
    #   NULL for Microsoft keywords (no campaign table bridge for MS).
    #   Populated and kept current by 02_sync_keyword_structure.py.
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

        -- Campaign bridge (Google only; NULL for Microsoft)
        campaign_id         TEXT,
        ad_group_id         TEXT,

        -- Denormalized names for readability without a join
        campaign_name       TEXT    NOT NULL,
        ad_group_name       TEXT    NOT NULL,

        status              TEXT,
        cpc_bid_micros      REAL,
        quality_score       INTEGER,

        first_seen_date     TEXT,
        last_seen_date      TEXT,
        platform            TEXT    NOT NULL DEFAULT 'google',

        UNIQUE (account_id, criterion_id)
    )
    """,

    # ------------------------------------------------------------------
    # keyword_performance_daily
    # Time-series core. One row per (keyword_instance x date x device x geo).
    #
    # Segmentation sentinel values:
    #   device='ALL', geo_state='ALL'      clean daily total (use for revenue)
    #   device='MOBILE' etc, geo='ALL'     device breakdown
    # NEVER SUM across segmentation types.
    # ------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS keyword_performance_daily (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        instance_id             INTEGER NOT NULL REFERENCES keyword_instances(id),
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
    # Also created by 05_sync_search_queries.py ensure_schema() if missing.
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

    # ------------------------------------------------------------------
    # search_query_performance
    # Fact table: daily grain per (query x matched_keyword x date x device).
    # ------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS search_query_performance (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        query_id             INTEGER NOT NULL REFERENCES search_queries(id),
        instance_id          INTEGER NOT NULL REFERENCES keyword_instances(id),
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
    "CREATE INDEX IF NOT EXISTS idx_identity_key           ON keyword_identities(identity_key)",

    # keyword_instances
    "CREATE INDEX IF NOT EXISTS idx_instances_identity     ON keyword_instances(identity_id)",
    "CREATE INDEX IF NOT EXISTS idx_instances_account      ON keyword_instances(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_instances_campaign     ON keyword_instances(campaign_id)",
    "CREATE INDEX IF NOT EXISTS idx_instances_adgroup      ON keyword_instances(ad_group_id)",

    # keyword_performance_daily
    "CREATE INDEX IF NOT EXISTS idx_perf_instance_date     ON keyword_performance_daily(instance_id, report_date)",
    "CREATE INDEX IF NOT EXISTS idx_perf_date              ON keyword_performance_daily(report_date)",
    "CREATE INDEX IF NOT EXISTS idx_perf_device            ON keyword_performance_daily(device)",
    "CREATE INDEX IF NOT EXISTS idx_perf_geo               ON keyword_performance_daily(geo_state)",

    # negative_keywords
    "CREATE INDEX IF NOT EXISTS idx_negatives_account      ON negative_keywords(account_id)",

    # accounts
    "CREATE INDEX IF NOT EXISTS idx_accounts_business_unit ON accounts(business_unit_id)",

    # search_queries
    "CREATE INDEX IF NOT EXISTS idx_sq_normalized          ON search_queries(normalized_text)",
    "CREATE INDEX IF NOT EXISTS idx_sq_canonical           ON search_queries(canonical_query_id)",

    # search_query_performance
    "CREATE INDEX IF NOT EXISTS idx_sqp_query_date         ON search_query_performance(query_id, report_date)",
    "CREATE INDEX IF NOT EXISTS idx_sqp_platform_date      ON search_query_performance(platform, report_date)",
    "CREATE INDEX IF NOT EXISTS idx_sqp_instance_date      ON search_query_performance(instance_id, report_date)",
    "CREATE INDEX IF NOT EXISTS idx_sqp_date_device        ON search_query_performance(report_date, device)",
]


# ---------------------------------------------------------------------------
# Migration pass
# Adds columns introduced in v4 to existing databases without data loss.
# Safe no-op on fresh databases where the columns already exist.
# ---------------------------------------------------------------------------

MIGRATIONS = [
    # table                  column               definition
    ("accounts",             "business_unit_id",  "INTEGER REFERENCES business_units(id)"),
    ("keyword_instances",    "campaign_id",        "TEXT"),
    ("keyword_instances",    "ad_group_id",        "TEXT"),
]


def apply_migrations(conn):
    cursor = conn.cursor()
    applied = []
    for table, col, defn in MIGRATIONS:
        existing = {r[1] for r in cursor.execute(f"PRAGMA table_info({table})")}
        if col not in existing:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {col} {defn}")
            applied.append(f"{table}.{col}")
    conn.commit()
    return applied


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

    # PRIMARY: QW Home Offline Conversions
    # $3,551,385 total. Fires 98% of days.
    """
    INSERT OR IGNORE INTO conversion_actions (account_id, conversion_name, include_mode, notes)
    VALUES (
        (SELECT id FROM accounts WHERE platform_account_id='8886502147' AND platform='google'),
        'QW Home Offline Conversions', 'primary',
        'Primary revenue KPI. $3,551,385 total. Fires 98% of days. QW offline upload pipeline.'
    )
    """,

    # SECONDARY COUNT: Zip Submit ($0 value assigned, count-only top-of-funnel)
    """
    INSERT OR IGNORE INTO conversion_actions (account_id, conversion_name, include_mode, notes)
    VALUES (
        (SELECT id FROM accounts WHERE platform_account_id='8886502147' AND platform='google'),
        'Zip Submit', 'secondary_count',
        'Top-of-funnel count signal. $0 value. Open Q H2: should value be assigned?'
    )
    """,

    # MONITOR: EQ Home Conversions with Value (nearly dormant)
    """
    INSERT OR IGNORE INTO conversion_actions (account_id, conversion_name, include_mode, notes)
    VALUES (
        (SELECT id FROM accounts WHERE platform_account_id='8886502147' AND platform='google'),
        'EQ Home Conversions with Value', 'monitor',
        'Nearly dormant. $4,908 total, fired 1 day only. Open Q H1: EverQuote integration active?'
    )
    """,

    # EXCLUDED: QW Home GBRAIDS (bidding signal — DO NOT remove from account)
    """
    INSERT OR IGNORE INTO conversion_actions (account_id, conversion_name, include_mode, notes)
    VALUES (
        (SELECT id FROM accounts WHERE platform_account_id='8886502147' AND platform='google'),
        'QW Home GBRAIDS', 'excluded',
        'Bidding signal only. $53,552 total. DO NOT REMOVE — removing disrupts Smart Bidding.'
    )
    """,

    # EXCLUDED: QW Submit Lead (dead action)
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
    # Update include_mode values here after M1-M4 confirmed, then re-run
    # 03_sync_keyword_performance.py --backfill and
    # 05_sync_search_queries.py --backfill for the medicare account.
    # ==========================================================================
    """
    INSERT OR IGNORE INTO accounts
        (account_name, platform_account_id, platform, mcc_id)
    VALUES ('Medicare', '5310820901', 'google', '6983050143')
    """,

    # PRIMARY: RB-Ringba - Medicare Advantage ($4,762,130 / 68% of days)
    """
    INSERT OR IGNORE INTO conversion_actions (account_id, conversion_name, include_mode, notes)
    VALUES (
        (SELECT id FROM accounts WHERE platform_account_id='5310820901' AND platform='google'),
        'RB-Ringba - Medicare Advantage', 'primary',
        'Provisional primary. $4,762,130 total. 68% of days. Pending M1: confirm not double-counted with RB-Ringba.'
    )
    """,

    # MONITOR: RB-Ringba (possible duplicate — M1 unresolved)
    """
    INSERT OR IGNORE INTO conversion_actions (account_id, conversion_name, include_mode, notes)
    VALUES (
        (SELECT id FROM accounts WHERE platform_account_id='5310820901' AND platform='google'),
        'RB-Ringba', 'monitor',
        'Possible duplicate of primary. $2,983,943 total. Fires same days. Open Q M1.'
    )
    """,

    # MONITOR: Phone Call (triple-count risk — M2 unresolved)
    """
    INSERT OR IGNORE INTO conversion_actions (account_id, conversion_name, include_mode, notes)
    VALUES (
        (SELECT id FROM accounts WHERE platform_account_id='5310820901' AND platform='google'),
        'Phone Call', 'monitor',
        'Triple-counting risk. $2,604,090 total. Open Q M2: Google-native or same Ringba calls?'
    )
    """,

    # MONITOR: RB-DNI_Calls (likely separate source — M3 unresolved)
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

    # MONITOR: Billable Calls (unclear threshold — M4 unresolved)
    """
    INSERT OR IGNORE INTO conversion_actions (account_id, conversion_name, include_mode, notes)
    VALUES (
        (SELECT id FROM accounts WHERE platform_account_id='5310820901' AND platform='google'),
        'Billable Calls', 'monitor',
        'Unclear threshold. $7,501 total. Open Q M4.'
    )
    """,

    # MONITOR: Ringba DNI - Medicare Advantage (low activity)
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

    print(f"Connecting to: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    cursor = conn.cursor()

    print("\n--- Creating tables ---")
    for stmt in SCHEMA_STATEMENTS:
        tokens = [w for w in stmt.split()
                  if w.upper() not in ("CREATE", "TABLE", "IF", "NOT", "EXISTS", "")]
        name = tokens[0] if tokens else "unknown"
        cursor.execute(stmt)
        print(f"  OK  {name}")

    print("\n--- Applying v4 migrations (existing DB column additions) ---")
    applied = apply_migrations(conn)
    if applied:
        for col in applied:
            print(f"  ADDED  {col}")
    else:
        print("  No migrations needed (all columns already present)")

    print("\n--- Creating indexes ---")
    for stmt in INDEX_STATEMENTS:
        name = stmt.split()[5]
        cursor.execute(stmt)
        print(f"  OK  {name}")

    print("\n--- Seeding reference data ---")
    for stmt in SEED_STATEMENTS:
        cursor.execute(stmt)
    conn.commit()
    print("  OK  Home Insurance + Medicare seeded")

    print("\n--- Table row counts ---")
    tables = [
        "business_units", "accounts", "conversion_actions",
        "keyword_identities", "keyword_instances",
        "keyword_performance_daily", "negative_keywords",
        "geo_target_constants", "search_queries", "search_query_performance",
    ]
    for table in tables:
        n = cursor.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table:<35} {n:>6} rows")

    print("\n--- Seeded conversion actions ---")
    rows = cursor.execute("""
        SELECT a.account_name, ca.conversion_name, ca.include_mode
        FROM   conversion_actions ca
        JOIN   accounts a ON ca.account_id = a.id
        ORDER  BY a.account_name, ca.include_mode, ca.conversion_name
    """).fetchall()
    current_account = None
    for acct_name, conv_name, mode in rows:
        if acct_name != current_account:
            print(f"\n  [{acct_name}]")
            current_account = acct_name
        print(f"    [{mode:<16}]  {conv_name}")

    conn.close()
    size_kb = os.path.getsize(DB_PATH) / 1024
    print(f"\nDatabase size: {size_kb:.1f} KB  |  Path: {DB_PATH}")
    print("\nNext steps:")
    print("  1. python campaign_db_scripts/00_seed_business_units.py")
    print("  2. python kw_db_scripts/02_sync_keyword_structure.py --account home")
    print("  3. python kw_db_scripts/02_sync_keyword_structure.py --account medicare")


if __name__ == "__main__":
    create_schema()
