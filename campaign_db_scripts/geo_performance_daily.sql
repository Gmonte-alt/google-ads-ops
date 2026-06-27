-- geo_performance_daily.sql
-- One-time schema creation for Microsoft Ads geo performance daily table.
-- Run this against C:\Projects\google-ads-ops\db\paid_search.db before first
-- invocation of 10_load_microsoft_geo.py.

CREATE TABLE IF NOT EXISTS geo_performance_daily (
    account_id                       INTEGER NOT NULL REFERENCES accounts(id),
    campaign_id                      TEXT    NOT NULL REFERENCES campaigns(campaign_id),

    -- Dimensions (natural key)
    date                             TEXT    NOT NULL,
    device                           TEXT    NOT NULL,
    network                          TEXT    NOT NULL,
    geo_state                        TEXT    NOT NULL,

    -- Metrics
    impressions                      INTEGER,
    clicks                           INTEGER,
    cost_micros                      INTEGER,
    conversions                      REAL,    -- pixel-only (CSV Conversions)
    conversion_value                 REAL,    -- offline-inclusive (CSV All revenue)
    all_conversions                  REAL,    -- offline-inclusive count (CSV All conversions)
    all_conversion_value             REAL,    -- offline-inclusive revenue (CSV All revenue)

    -- Snapshot fields (approximated from current campaigns table values;
    -- to be backfilled when change-history ingestion lands)
    budget_micros_that_day           INTEGER,
    bidding_strategy_type_that_day   TEXT,
    bidding_strategy_id_that_day     TEXT,
    target_cpa_micros_that_day       INTEGER,
    target_roas_that_day             REAL,

    -- Provenance
    last_synced_at                   TEXT    NOT NULL,

    PRIMARY KEY (campaign_id, date, device, network, geo_state)
);

CREATE INDEX IF NOT EXISTS idx_geo_perf_account_date
    ON geo_performance_daily (account_id, date);

CREATE INDEX IF NOT EXISTS idx_geo_perf_date
    ON geo_performance_daily (date);

CREATE INDEX IF NOT EXISTS idx_geo_perf_state
    ON geo_performance_daily (geo_state);

CREATE INDEX IF NOT EXISTS idx_geo_perf_network_date
    ON geo_performance_daily (network, date);
