"""
01_create_campaign_tables.py  [v2 — INTEGER account_id, consistent join pattern]
==============================
Campaign Structure Analytics — Quote.com Google Ads
Folder: C:\\Projects\\google-ads-ops\\campaign_db_scripts\\

Creates (or rebuilds) five campaign tables in the existing paid_search.db.

Changes from v1:
  - account_id changed from TEXT (platform_account_id string) to INTEGER
    referencing accounts(id). This aligns the campaign schema with the
    keyword tracking schema, where all tables join to accounts via the
    integer PK. Both schemas now use the same join pattern:
        JOIN accounts a ON <table>.account_id = a.id
    All sync scripts resolve the integer account_id once per run with:
        SELECT id FROM accounts
        WHERE platform_account_id = ? AND platform = ?
  - All five tables are dropped and rebuilt on every run of this script
    to ensure the account_id type change takes effect. Reference tables
    (accounts, business_units, etc.) are untouched.
  - Diagnostic checkpoint printed at end: row counts and date ranges
    for any existing data retained from the previous schema (will show
    0 rows on first run after rebuild).

Tables created:
  campaigns                      -- campaign structure, budget, bidding strategy
  ad_groups                      -- ad group structure, cpc bids, bid override flag
  campaign_performance_daily     -- day x device grain; IS + bid snapshots
  campaign_performance_hourly    -- day x hour x device grain; dayparting analysis
  campaign_performance_geo_daily -- day x device x state grain; geographic breakdown

Geo grain design notes (unchanged from v1):
  - Source: geographic_view resource (NOT campaign resource)
  - IS not available at geo grain (Google Ads API constraint)
  - Bid strategy snapshots not stored at geo grain (campaign-level only)
  - Totals slightly lower than campaign_performance_daily (expected)

Usage:
    conda activate paid-search
    python campaign_db_scripts/01_create_campaign_tables.py
"""

import sqlite3
import logging
from datetime import datetime
from pathlib import Path

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Drop order — FK-safe: child tables before parent tables
# ad_groups references campaigns, so ad_groups drops first.
# Performance tables reference campaigns implicitly via campaign_id TEXT
# (no declared FK) but we drop them first for cleanliness.
# ---------------------------------------------------------------------------

DROP_STATEMENTS = [
    "DROP TABLE IF EXISTS campaign_performance_geo_daily",
    "DROP TABLE IF EXISTS campaign_performance_hourly",
    "DROP TABLE IF EXISTS campaign_performance_daily",
    "DROP TABLE IF EXISTS ad_groups",
    "DROP TABLE IF EXISTS campaigns",
]


# ---------------------------------------------------------------------------
# Table definitions — account_id is now INTEGER REFERENCES accounts(id)
# ---------------------------------------------------------------------------

TABLE_STATEMENTS = [

    # ── campaigns ──────────────────────────────────────────────────────────
    # account_id INTEGER references accounts(id) — consistent with kw schema.
    # Sync scripts resolve the integer via:
    #   SELECT id FROM accounts WHERE platform_account_id = ? AND platform = ?
    # campaign_id remains TEXT (Google Ads campaign IDs as strings — safe
    # against integer overflow for large Google numeric IDs).
    """
    CREATE TABLE IF NOT EXISTS campaigns (
        campaign_id                  TEXT    PRIMARY KEY,
        account_id                   INTEGER NOT NULL REFERENCES accounts(id),
        campaign_name                TEXT    NOT NULL,

        status                       TEXT    NOT NULL,
        campaign_type                TEXT,
        advertising_channel_sub_type TEXT,

        budget_id                    TEXT,
        budget_name                  TEXT,
        budget_type                  TEXT,
        daily_budget_micros          INTEGER,
        is_shared_budget             INTEGER NOT NULL DEFAULT 0,

        -- Current bidding state (historical snapshots on campaign_performance_daily)
        bidding_strategy_type        TEXT,
        bidding_strategy_id          TEXT,
        bidding_strategy_name        TEXT,
        target_cpa_micros            INTEGER,
        target_roas                  REAL,

        -- Populated by 04_parse_campaign_names.py
        parsed_product_type          TEXT,
        parsed_geo_state             TEXT,
        parsed_match_tier            TEXT,
        parsed_intent_tier           TEXT,
        naming_convention_flag       INTEGER DEFAULT 0,

        -- JSON array string e.g. '["label_a","label_b"]'
        labels                       TEXT,

        first_seen_date              TEXT,
        last_synced_at               TEXT    NOT NULL
    )
    """,

    # ── ad_groups ───────────────────────────────────────────────────────────
    # bid_override_flag = 1 means target_cpa_micros or target_roas is non-null
    # at the ad group level (structural anomaly — should always be null per
    # account design). Flag surfaces drift for investigation.
    """
    CREATE TABLE IF NOT EXISTS ad_groups (
        ad_group_id              TEXT    PRIMARY KEY,
        campaign_id              TEXT    NOT NULL REFERENCES campaigns(campaign_id),
        account_id               INTEGER NOT NULL REFERENCES accounts(id),
        ad_group_name            TEXT    NOT NULL,

        status                   TEXT    NOT NULL,
        ad_group_type            TEXT,

        cpc_bid_micros           INTEGER,
        target_cpa_micros        INTEGER,
        target_roas              REAL,

        -- 1 if target_cpa_micros or target_roas is non-null
        bid_override_flag        INTEGER NOT NULL DEFAULT 0,

        -- Populated by 04_parse_campaign_names.py
        parsed_theme             TEXT,
        parsed_match_tier        TEXT,
        parsed_intent_tier       TEXT,
        naming_convention_flag   INTEGER DEFAULT 0,

        first_seen_date          TEXT,
        last_synced_at           TEXT    NOT NULL
    )
    """,

    # ── campaign_performance_daily ──────────────────────────────────────────
    # Grain: one row per campaign x date x device.
    #
    # device sentinel values:
    #   device='ALL'     clean daily total (generated by summing MOBILE +
    #                    DESKTOP + TABLET at sync time; use for revenue analysis)
    #   device='MOBILE'  mobile breakdown
    #   device='DESKTOP' desktop breakdown
    #   device='TABLET'  tablet breakdown
    # NEVER SUM across device values.
    #
    # Impression share stored as 0.0–1.0 (e.g. 0.72 = 72% IS).
    # NULL = Google threshold not met (< ~10 impressions that day).
    # IS NOT available at hourly or geo grain.
    #
    # Snapshot columns (budget_micros_that_day, bidding_strategy_*_that_day):
    #   Stored per row because budgets and targets change over time.
    #   Joining to campaigns.daily_budget_micros at query time would return
    #   the CURRENT value, not what was active on each historical date.
    #
    # bidding_strategy_id_that_day:
    #   NULL  = campaign-level (non-portfolio) strategy active on this date
    #   value = portfolio strategy ID active on this date
    """
    CREATE TABLE IF NOT EXISTS campaign_performance_daily (
        campaign_id                      TEXT    NOT NULL REFERENCES campaigns(campaign_id),
        account_id                       INTEGER NOT NULL REFERENCES accounts(id),
        date                             TEXT    NOT NULL,
        device                           TEXT    NOT NULL,

        impressions                      INTEGER NOT NULL DEFAULT 0,
        clicks                           INTEGER NOT NULL DEFAULT 0,
        cost_micros                      INTEGER NOT NULL DEFAULT 0,
        conversions                      REAL    NOT NULL DEFAULT 0,
        conversion_value                 REAL    NOT NULL DEFAULT 0,
        view_through_conversions         INTEGER          DEFAULT 0,
        all_conversions                  REAL             DEFAULT 0,
        all_conversion_value             REAL             DEFAULT 0,

        -- Impression share (NULL = below reporting threshold)
        -- NOT available at hourly or geo grain
        search_impression_share          REAL,
        search_budget_lost_is            REAL,
        search_rank_lost_is              REAL,
        search_exact_match_is            REAL,

        -- Budget snapshot: daily budget active on this date
        budget_micros_that_day           INTEGER,

        -- Bid strategy snapshots: active strategy on this date
        bidding_strategy_type_that_day   TEXT,
        bidding_strategy_id_that_day     TEXT,
        target_cpa_micros_that_day       INTEGER,
        target_roas_that_day             REAL,

        last_synced_at                   TEXT    NOT NULL,

        PRIMARY KEY (campaign_id, date, device)
    )
    """,

    # ── campaign_performance_hourly ─────────────────────────────────────────
    # Grain: one row per campaign x date x hour x device.
    # hour is 0–23 in account local time (Google Ads API).
    #
    # IS fields NOT available at hourly grain — including them in a GAQL
    # query with segments.hour raises:
    #   GoogleAdsException: CANNOT_USE_DURING_SEGMENT (search_impression_share)
    # IS analysis must use campaign_performance_daily exclusively.
    #
    # Bid strategy snapshots not stored — targets don't change intra-day.
    # Join to campaign_performance_daily on (campaign_id, date) for strategy.
    """
    CREATE TABLE IF NOT EXISTS campaign_performance_hourly (
        campaign_id          TEXT    NOT NULL REFERENCES campaigns(campaign_id),
        account_id           INTEGER NOT NULL REFERENCES accounts(id),
        date                 TEXT    NOT NULL,
        hour                 INTEGER NOT NULL,
        device               TEXT    NOT NULL,

        impressions          INTEGER NOT NULL DEFAULT 0,
        clicks               INTEGER NOT NULL DEFAULT 0,
        cost_micros          INTEGER NOT NULL DEFAULT 0,
        conversions          REAL    NOT NULL DEFAULT 0,
        conversion_value     REAL    NOT NULL DEFAULT 0,
        all_conversions      REAL             DEFAULT 0,
        all_conversion_value REAL             DEFAULT 0,

        last_synced_at       TEXT    NOT NULL,

        PRIMARY KEY (campaign_id, date, hour, device)
    )
    """,

    # ── campaign_performance_geo_daily ──────────────────────────────────────
    # Grain: one row per campaign x date x device x state.
    # Source: geographic_view resource (NOT campaign resource — geo segments
    # cause a GAQL error when used with FROM campaign).
    #
    # geo_target_constant_id: integer extracted from "geoTargetConstants/NNNNN"
    #   string returned by the API. Joins to geo_target_constants.criterion_id
    #   for state name decoding.
    # geo_state: denormalized state name (e.g. "California") stored directly
    #   to avoid a join on every analysis query.
    #
    # IS and bid strategy snapshots NOT stored:
    #   IS: not available from geographic_view (API constraint)
    #   Strategy: campaign-level, join to campaign_performance_daily if needed
    #
    # Totals will be slightly lower than campaign_performance_daily because
    # geographic_view omits impressions with no geo association. Expected.
    """
    CREATE TABLE IF NOT EXISTS campaign_performance_geo_daily (
        campaign_id              TEXT    NOT NULL REFERENCES campaigns(campaign_id),
        account_id               INTEGER NOT NULL REFERENCES accounts(id),
        date                     TEXT    NOT NULL,
        device                   TEXT    NOT NULL,

        geo_target_constant_id   INTEGER NOT NULL
                                         REFERENCES geo_target_constants(criterion_id),
        geo_state                TEXT,

        impressions              INTEGER NOT NULL DEFAULT 0,
        clicks                   INTEGER NOT NULL DEFAULT 0,
        cost_micros              INTEGER NOT NULL DEFAULT 0,
        conversions              REAL    NOT NULL DEFAULT 0,
        conversion_value         REAL    NOT NULL DEFAULT 0,
        all_conversions          REAL             DEFAULT 0,
        all_conversion_value     REAL             DEFAULT 0,

        last_synced_at           TEXT    NOT NULL,

        PRIMARY KEY (campaign_id, date, device, geo_target_constant_id)
    )
    """,
]

INDEX_STATEMENTS = [
    # campaigns
    "CREATE INDEX IF NOT EXISTS idx_campaigns_account  ON campaigns(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_campaigns_status   ON campaigns(status)",
    "CREATE INDEX IF NOT EXISTS idx_campaigns_product  ON campaigns(parsed_product_type)",
    "CREATE INDEX IF NOT EXISTS idx_campaigns_geo      ON campaigns(parsed_geo_state)",

    # ad_groups
    "CREATE INDEX IF NOT EXISTS idx_adgroups_campaign  ON ad_groups(campaign_id)",
    "CREATE INDEX IF NOT EXISTS idx_adgroups_account   ON ad_groups(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_adgroups_status    ON ad_groups(status)",
    "CREATE INDEX IF NOT EXISTS idx_adgroups_bid_flag  ON ad_groups(bid_override_flag)",

    # campaign_performance_daily
    "CREATE INDEX IF NOT EXISTS idx_cpd_date           ON campaign_performance_daily(date)",
    "CREATE INDEX IF NOT EXISTS idx_cpd_campaign_date  ON campaign_performance_daily(campaign_id, date)",
    "CREATE INDEX IF NOT EXISTS idx_cpd_account_date   ON campaign_performance_daily(account_id, date)",

    # campaign_performance_hourly
    "CREATE INDEX IF NOT EXISTS idx_cph_campaign_date  ON campaign_performance_hourly(campaign_id, date)",
    "CREATE INDEX IF NOT EXISTS idx_cph_account_date   ON campaign_performance_hourly(account_id, date)",
    "CREATE INDEX IF NOT EXISTS idx_cph_hour           ON campaign_performance_hourly(hour)",
    "CREATE INDEX IF NOT EXISTS idx_cph_date_hour      ON campaign_performance_hourly(date, hour)",

    # campaign_performance_geo_daily
    "CREATE INDEX IF NOT EXISTS idx_cgd_date           ON campaign_performance_geo_daily(date)",
    "CREATE INDEX IF NOT EXISTS idx_cgd_campaign_date  ON campaign_performance_geo_daily(campaign_id, date)",
    "CREATE INDEX IF NOT EXISTS idx_cgd_account_date   ON campaign_performance_geo_daily(account_id, date)",
    "CREATE INDEX IF NOT EXISTS idx_cgd_geo_state      ON campaign_performance_geo_daily(geo_state)",
    "CREATE INDEX IF NOT EXISTS idx_cgd_geo_id         ON campaign_performance_geo_daily(geo_target_constant_id)",
]


# ---------------------------------------------------------------------------
# Database connection
# ---------------------------------------------------------------------------

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Diagnostic checkpoint
# Prints row counts and date ranges per account for all performance tables.
# Run after any sync to confirm data landed correctly.
# ---------------------------------------------------------------------------

def print_diagnostic(conn):
    log.info("")
    log.info("── Diagnostic checkpoint ─────────────────────────────────────")

    cursor = conn.cursor()

    tables = [
        ("campaigns",                   "last_synced_at", None),
        ("ad_groups",                   "last_synced_at", None),
        ("campaign_performance_daily",  "date",           "device = 'ALL'"),
        ("campaign_performance_hourly", "date",           None),
        ("campaign_performance_geo_daily", "date",        None),
    ]

    for table, date_col, where in tables:
        log.info("")
        log.info(f"  {table}")

        where_clause = f"WHERE {where}" if where else ""

        try:
            rows = cursor.execute(f"""
                SELECT
                    a.account_name,
                    a.platform_account_id,
                    COUNT(*)                        AS row_count,
                    MIN(t.{date_col})               AS min_date,
                    MAX(t.{date_col})               AS max_date,
                    SUM(CASE WHEN t.cost_micros IS NOT NULL
                             THEN t.cost_micros ELSE 0 END) / 1e6 AS spend,
                    SUM(CASE WHEN t.impressions IS NOT NULL
                             THEN t.impressions ELSE 0 END)        AS impressions,
                    SUM(CASE WHEN t.clicks IS NOT NULL
                             THEN t.clicks ELSE 0 END)             AS clicks
                FROM {table} t
                JOIN accounts a ON t.account_id = a.id
                {where_clause}
                GROUP BY t.account_id
                ORDER BY a.platform_account_id
            """).fetchall()

            if not rows:
                log.info("    (no data yet)")
                continue

            log.info(f"  {'Account':<28} {'Rows':>8} {'Min date':<12} "
                     f"{'Max date':<12} {'Spend':>12} {'Impr':>10} {'Clicks':>8}")
            log.info(f"  {'─'*28} {'─'*8} {'─'*12} {'─'*12} {'─'*12} {'─'*10} {'─'*8}")

            for r in rows:
                log.info(
                    f"  {r['account_name'][:27]:<28} "
                    f"{r['row_count']:>8,} "
                    f"{str(r['min_date'] or '—'):<12} "
                    f"{str(r['max_date'] or '—'):<12} "
                    f"${r['spend']:>11,.2f} "
                    f"{r['impressions']:>10,} "
                    f"{r['clicks']:>8,}"
                )
        except Exception as e:
            log.warning(f"    Could not query {table}: {e}")

    # keyword_performance_daily — joins via keyword_instances
    log.info("")
    log.info("  keyword_performance_daily  (via keyword_instances)")
    try:
        rows = cursor.execute("""
            SELECT
                a.account_name,
                a.platform_account_id,
                COUNT(*)                        AS row_count,
                MIN(kpd.report_date)            AS min_date,
                MAX(kpd.report_date)            AS max_date,
                SUM(kpd.cost_micros) / 1e6      AS spend,
                SUM(kpd.impressions)            AS impressions,
                SUM(kpd.clicks)                 AS clicks
            FROM keyword_performance_daily kpd
            JOIN keyword_instances ki ON ki.id = kpd.instance_id
            JOIN accounts a           ON a.id  = ki.account_id
            WHERE kpd.device = 'ALL' AND kpd.geo_state = 'ALL'
            GROUP BY ki.account_id
            ORDER BY a.platform_account_id
        """).fetchall()

        if not rows:
            log.info("    (no data yet)")
        else:
            log.info(f"  {'Account':<28} {'Rows':>8} {'Min date':<12} "
                     f"{'Max date':<12} {'Spend':>12} {'Impr':>10} {'Clicks':>8}")
            log.info(f"  {'─'*28} {'─'*8} {'─'*12} {'─'*12} {'─'*12} {'─'*10} {'─'*8}")
            for r in rows:
                log.info(
                    f"  {r['account_name'][:27]:<28} "
                    f"{r['row_count']:>8,} "
                    f"{str(r['min_date'] or '—'):<12} "
                    f"{str(r['max_date'] or '—'):<12} "
                    f"${r['spend']:>11,.2f} "
                    f"{r['impressions']:>10,} "
                    f"{r['clicks']:>8,}"
                )
    except Exception as e:
        log.warning(f"    Could not query keyword_performance_daily: {e}")

    # search_query_performance — joins via keyword_instances
    log.info("")
    log.info("  search_query_performance  (via keyword_instances)")
    try:
        rows = cursor.execute("""
            SELECT
                a.account_name,
                a.platform_account_id,
                COUNT(*)                        AS row_count,
                COUNT(DISTINCT sqp.query_id)    AS unique_queries,
                MIN(sqp.report_date)            AS min_date,
                MAX(sqp.report_date)            AS max_date,
                SUM(sqp.cost_micros) / 1e6      AS spend,
                SUM(sqp.clicks)                 AS clicks
            FROM search_query_performance sqp
            JOIN keyword_instances ki ON ki.id = sqp.instance_id
            JOIN accounts a           ON a.id  = ki.account_id
            WHERE sqp.device = 'ALL'
            GROUP BY ki.account_id
            ORDER BY a.platform_account_id
        """).fetchall()

        if not rows:
            log.info("    (no data yet)")
        else:
            log.info(f"  {'Account':<28} {'Rows':>8} {'Queries':>8} "
                     f"{'Min date':<12} {'Max date':<12} {'Spend':>12} {'Clicks':>8}")
            log.info(f"  {'─'*28} {'─'*8} {'─'*8} {'─'*12} {'─'*12} {'─'*12} {'─'*8}")
            for r in rows:
                log.info(
                    f"  {r['account_name'][:27]:<28} "
                    f"{r['row_count']:>8,} "
                    f"{r['unique_queries']:>8,} "
                    f"{str(r['min_date'] or '—'):<12} "
                    f"{str(r['max_date'] or '—'):<12} "
                    f"${r['spend']:>11,.2f} "
                    f"{r['clicks']:>8,}"
                )
    except Exception as e:
        log.warning(f"    Could not query search_query_performance: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info(f"Database : {DB_PATH}")
    log.info(f"Started  : {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    log.info("")

    conn = get_db_connection()
    conn.execute("PRAGMA foreign_keys = OFF")   # off during drop/rebuild
    cur = conn.cursor()

    # ── Step 1: Drop existing campaign tables ──────────────────────────
    log.info("── Step 1: Drop existing campaign tables ─────────────────────")
    for stmt in DROP_STATEMENTS:
        table = stmt.split()[-1]
        cur.execute(stmt)
        log.info(f"  DROPPED  {table}")

    conn.commit()

    # ── Step 2: Create tables ──────────────────────────────────────────
    log.info("")
    log.info("── Step 2: Create tables (account_id INTEGER) ────────────────")
    for stmt in TABLE_STATEMENTS:
        tokens = [w for w in stmt.split()
                  if w.upper() not in ("CREATE", "TABLE", "IF", "NOT", "EXISTS", "")]
        name = tokens[0] if tokens else "unknown"
        cur.execute(stmt)
        log.info(f"  CREATED  {name}")

    conn.commit()

    # ── Step 3: Create indexes ─────────────────────────────────────────
    log.info("")
    log.info("── Step 3: Indexes ───────────────────────────────────────────")
    for stmt in INDEX_STATEMENTS:
        name = stmt.split()[5]
        cur.execute(stmt)
        log.info(f"  OK  {name}")

    conn.commit()

    # ── Step 4: Verify accounts table is ready ─────────────────────────
    log.info("")
    log.info("── Step 4: Accounts available for wiring ─────────────────────")
    conn.execute("PRAGMA foreign_keys = ON")
    acct_rows = cur.execute("""
        SELECT id, platform_account_id, platform, account_name, business_unit_id
        FROM accounts
        ORDER BY platform, platform_account_id
    """).fetchall()

    log.info(f"  {'id':<5} {'platform_account_id':<20} {'platform':<12} "
             f"{'bu_id':<8} account_name")
    log.info(f"  {'─'*5} {'─'*20} {'─'*12} {'─'*8} {'─'*30}")
    for r in acct_rows:
        bu = str(r['business_unit_id']) if r['business_unit_id'] else '⚠ NULL'
        log.info(f"  {r['id']:<5} {r['platform_account_id']:<20} "
                 f"{r['platform']:<12} {bu:<8} {r['account_name']}")

    unwired = [r for r in acct_rows if not r['business_unit_id']]
    if unwired:
        log.warning("")
        log.warning(f"  {len(unwired)} account(s) have no business_unit_id.")
        log.warning("  Run: python campaign_db_scripts/00_seed_business_units.py")

    # ── Step 5: Diagnostic checkpoint ─────────────────────────────────
    print_diagnostic(conn)

    conn.close()

    log.info("")
    log.info("── Summary ───────────────────────────────────────────────────")
    log.info("  5 campaign tables rebuilt with account_id INTEGER")
    log.info("  21 indexes created")
    log.info("  All tables empty and ready for sync")
    log.info("")
    log.info("Next step:")
    log.info("  python campaign_db_scripts/00_seed_business_units.py")
    log.info("  (if business_unit_id is NULL on any accounts above)")
    log.info("  Then:")
    log.info("  python campaign_db_scripts/02_sync_campaigns_adgroups.py")


if __name__ == "__main__":
    main()
