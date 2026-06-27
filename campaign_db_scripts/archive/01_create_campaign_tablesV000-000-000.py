"""
01_create_campaign_tables.py
=============================
Campaign Structure Analytics — Quote.com Google Ads
Folder: C:\\Projects\\google-ads-ops\\campaign_db_scripts\\

Creates five tables in the existing paid_search.db. Safe to re-run (IF NOT EXISTS).

Alignment with existing keyword tracking schema (kw_db_scripts/01_create_schema.py):
  - accounts table uses:
        id                  INTEGER PRIMARY KEY AUTOINCREMENT
        platform_account_id TEXT    (Google customer ID string e.g. "8886502147")
        platform            TEXT    DEFAULT 'google'
        mcc_id              TEXT
    Our campaign tables reference accounts via platform_account_id TEXT — no FK
    declaration since SQLite cannot enforce a FK to a non-primary-key column.
    This matches the join pattern already used in 10_tagging_diagnostic.py:
        JOIN accounts a ON ki.account_id = a.id ... WHERE a.platform_account_id IN (...)
    Campaign tables store platform_account_id directly in account_id for query
    convenience, avoiding a join through the integer accounts.id for every
    campaign-level filter.

  - PRAGMA journal_mode = WAL is set on every connection (matches kw scripts).
    This allows DB Browser for SQLite to read the file during active sync runs.

Tables created:
  campaigns                      -- campaign structure, budget, bidding strategy
  ad_groups                      -- ad group structure, cpc bids, bid override flag
  campaign_performance_daily     -- day × device grain; includes IS + bid snapshots
  campaign_performance_hourly    -- day × hour × device grain; dayparting analysis
  campaign_performance_geo_daily -- day × device × state grain; geographic breakdown

────────────────────────────────────────────────────────────────────────────────
Geo grain design notes (campaign_performance_geo_daily):

  Source resource: geographic_view (NOT campaign)
    geo performance data is not available on the campaign resource. The API
    returns a query error if any segments.geo_target_* field is used with
    FROM campaign. geographic_view is the correct resource for user-location
    performance data and maps to the "Matched Locations" report in the UI.

  Segment field: segments.geo_target_state
    Returns a resource name string e.g. "geoTargetConstants/21137".
    The trailing integer is extracted and stored as geo_target_constant_id,
    which joins to geo_target_constants.criterion_id (populated by the
    keyword tracking system).

  geo_state denormalization:
    State name (e.g. "California") is stored directly on each row to avoid
    a join to geo_target_constants on every analysis query. Matches the same
    pattern used in keyword_performance_daily.geo_state.

  IS not available at geo grain:
    Google Ads API does not return impression share metrics when
    segments.geo_target_state is present. IS stays exclusively on
    campaign_performance_daily.

  Bid strategy snapshots not stored:
    Strategy targets are campaign-level and don't vary by geo. Join to
    campaign_performance_daily on (campaign_id, date) for strategy context.

  Data completeness note:
    geographic_view omits rows with no geo association, so impression totals
    from this table will be slightly lower than campaign_performance_daily.
    This matches the Google Ads UI "Matched Locations" report behavior.

  campaign.status in SELECT:
    The geographic_view resource requires any field used in WHERE to also
    appear in SELECT. campaign.status is therefore in both clauses in the
    sync query (03_sync_campaign_performance.py).
────────────────────────────────────────────────────────────────────────────────
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")

DDL_STATEMENTS = [

    # ── campaigns ─────────────────────────────────────────────────────────────
    # account_id stores platform_account_id (Google customer ID string).
    # No FK declaration — SQLite cannot FK to a non-PK column.
    # Join to accounts via: JOIN accounts a ON c.account_id = a.platform_account_id
    """
    CREATE TABLE IF NOT EXISTS campaigns (
        campaign_id                  TEXT PRIMARY KEY,
        account_id                   TEXT NOT NULL,
        campaign_name                TEXT NOT NULL,

        status                       TEXT NOT NULL,
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
        last_synced_at               TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_campaigns_account ON campaigns(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_campaigns_status  ON campaigns(status)",
    "CREATE INDEX IF NOT EXISTS idx_campaigns_product ON campaigns(parsed_product_type)",
    "CREATE INDEX IF NOT EXISTS idx_campaigns_geo     ON campaigns(parsed_geo_state)",

    # ── ad_groups ──────────────────────────────────────────────────────────────
    # bid_override_flag = 1 means target_cpa_micros or target_roas is non-null
    # at the ad group level. Per account structure assumptions these should always
    # be null. Flag surfaces structural drift for investigation.
    """
    CREATE TABLE IF NOT EXISTS ad_groups (
        ad_group_id              TEXT PRIMARY KEY,
        campaign_id              TEXT NOT NULL,
        account_id               TEXT NOT NULL,
        ad_group_name            TEXT NOT NULL,

        status                   TEXT NOT NULL,
        ad_group_type            TEXT,

        cpc_bid_micros           INTEGER,
        target_cpa_micros        INTEGER,
        target_roas              REAL,

        -- 1 if target_cpa_micros or target_roas is non-null (structural anomaly)
        bid_override_flag        INTEGER NOT NULL DEFAULT 0,

        -- Populated by 04_parse_campaign_names.py
        parsed_theme             TEXT,
        parsed_match_tier        TEXT,
        parsed_intent_tier       TEXT,
        naming_convention_flag   INTEGER DEFAULT 0,

        first_seen_date          TEXT,
        last_synced_at           TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_adgroups_campaign ON ad_groups(campaign_id)",
    "CREATE INDEX IF NOT EXISTS idx_adgroups_account  ON ad_groups(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_adgroups_status   ON ad_groups(status)",
    "CREATE INDEX IF NOT EXISTS idx_adgroups_bid_flag ON ad_groups(bid_override_flag)",

    # ── campaign_performance_daily ─────────────────────────────────────────────
    # Grain: one row per campaign × date × device.
    #
    # Snapshot columns (budget, bidding strategy):
    #   Stored here because budgets and bid targets change over time.
    #   Joining to campaigns.daily_budget_micros at query time would reflect the
    #   CURRENT value, not what was active on each historical date.
    #
    # bidding_strategy_id_that_day:
    #   NULL  = campaign-level strategy was active on this date
    #   value = portfolio (shared) strategy ID was active on this date
    #
    # Impression share:
    #   Stored as 0.0–1.0 (e.g. 0.72 = 72% IS).
    #   NULL means Google's reporting threshold was not met (< ~10 impressions).
    #   Google returns -1 as a sentinel for below-threshold; synced as NULL by
    #   safe_is() in 03_sync_campaign_performance.py so AVG() is never corrupted.
    #
    # IS is NOT available at hourly or geo grain.
    """
    CREATE TABLE IF NOT EXISTS campaign_performance_daily (
        campaign_id                      TEXT    NOT NULL,
        account_id                       TEXT    NOT NULL,
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

        -- Impression share (NULL = below threshold; NOT available at hourly or geo grain)
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

        last_synced_at                   TEXT NOT NULL,

        PRIMARY KEY (campaign_id, date, device)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_cpd_date          ON campaign_performance_daily(date)",
    "CREATE INDEX IF NOT EXISTS idx_cpd_campaign_date ON campaign_performance_daily(campaign_id, date)",
    "CREATE INDEX IF NOT EXISTS idx_cpd_account_date  ON campaign_performance_daily(account_id, date)",

    # ── campaign_performance_hourly ────────────────────────────────────────────
    # Grain: one row per campaign × date × hour × device.
    # hour is 0–23 in account local time (as returned by the Google Ads API).
    #
    # IMPORTANT: Impression share fields are NOT available at this grain.
    # Including IS fields in a GAQL query with segments.hour causes:
    #   GoogleAdsException: CANNOT_USE_DURING_SEGMENT (search_impression_share)
    # IS analysis must use campaign_performance_daily exclusively.
    #
    # Bid strategy snapshots are also absent — strategy targets don't change
    # intra-day. Use the daily table for any strategy-aware analysis.
    """
    CREATE TABLE IF NOT EXISTS campaign_performance_hourly (
        campaign_id          TEXT    NOT NULL,
        account_id           TEXT    NOT NULL,
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

        last_synced_at       TEXT NOT NULL,

        PRIMARY KEY (campaign_id, date, hour, device)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_cph_campaign_date ON campaign_performance_hourly(campaign_id, date)",
    "CREATE INDEX IF NOT EXISTS idx_cph_account_date  ON campaign_performance_hourly(account_id, date)",
    "CREATE INDEX IF NOT EXISTS idx_cph_hour          ON campaign_performance_hourly(hour)",
    "CREATE INDEX IF NOT EXISTS idx_cph_date_hour     ON campaign_performance_hourly(date, hour)",

    # ── campaign_performance_geo_daily ─────────────────────────────────────────
    # Grain: one row per campaign × date × device × state.
    #
    # Sourced from geographic_view (not campaign resource).
    # segments.geo_target_state returns "geoTargetConstants/NNNNN" strings;
    # the integer criterion ID is extracted and stored here, joining to
    # geo_target_constants.criterion_id for name decoding.
    #
    # geo_state is denormalized (e.g. "California") for query convenience,
    # avoiding a join to geo_target_constants on every analysis query.
    #
    # IS and bid strategy snapshots are NOT stored here:
    #   - IS: not available from geographic_view (API constraint)
    #   - Strategy: campaign-level, doesn't vary by geo — join to
    #     campaign_performance_daily on (campaign_id, date) if needed.
    #
    # Totals from this table will be slightly lower than campaign_performance_daily
    # because geographic_view omits rows with no geo association. This matches
    # the Google Ads UI "Matched Locations" report behavior and is expected.
    """
    CREATE TABLE IF NOT EXISTS campaign_performance_geo_daily (
        campaign_id              TEXT    NOT NULL,
        account_id               TEXT    NOT NULL,
        date                     TEXT    NOT NULL,
        device                   TEXT    NOT NULL,

        -- Integer criterion ID extracted from "geoTargetConstants/NNNNN"
        -- Joins to geo_target_constants.criterion_id
        geo_target_constant_id   INTEGER NOT NULL,

        -- Denormalized state name for query convenience e.g. "California"
        -- NULL if the criterion ID has no match in geo_target_constants
        geo_state                TEXT,

        impressions              INTEGER NOT NULL DEFAULT 0,
        clicks                   INTEGER NOT NULL DEFAULT 0,
        cost_micros              INTEGER NOT NULL DEFAULT 0,
        conversions              REAL    NOT NULL DEFAULT 0,
        conversion_value         REAL    NOT NULL DEFAULT 0,
        all_conversions          REAL             DEFAULT 0,
        all_conversion_value     REAL             DEFAULT 0,

        last_synced_at           TEXT NOT NULL,

        PRIMARY KEY (campaign_id, date, device, geo_target_constant_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_cgd_date          ON campaign_performance_geo_daily(date)",
    "CREATE INDEX IF NOT EXISTS idx_cgd_campaign_date ON campaign_performance_geo_daily(campaign_id, date)",
    "CREATE INDEX IF NOT EXISTS idx_cgd_account_date  ON campaign_performance_geo_daily(account_id, date)",
    "CREATE INDEX IF NOT EXISTS idx_cgd_geo_state     ON campaign_performance_geo_daily(geo_state)",
    "CREATE INDEX IF NOT EXISTS idx_cgd_geo_id        ON campaign_performance_geo_daily(geo_target_constant_id)",
]


def get_db_connection():
    """Matches the connection pattern used across all kw_db_scripts."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def main():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_db_connection()
    cur  = conn.cursor()

    tables  = []
    indexes = []

    for stmt in DDL_STATEMENTS:
        cur.execute(stmt)
        tokens = stmt.strip().split()
        if len(tokens) >= 6:
            kind = tokens[2].upper()
            name = tokens[5]
            if kind == "TABLE":
                tables.append(name)
            elif kind == "INDEX":
                indexes.append(name)

    conn.commit()
    conn.close()

    print("Campaign table DDL complete.")
    print(f"\n  Tables  ({len(tables)}):")
    for t in tables:
        print(f"    {t}")
    print(f"\n  Indexes ({len(indexes)}):")
    for i in indexes:
        print(f"    {i}")
    print(f"\n  Database: {DB_PATH}")
    print("\nNext step: python 02_sync_campaigns_adgroups.py")


if __name__ == "__main__":
    main()
