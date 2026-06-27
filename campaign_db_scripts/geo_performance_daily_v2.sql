-- geo_performance_daily_v2.sql
-- Migration: add location_type column and extend primary key.
--
-- Why: Microsoft's Geographic Location Report emits two row types per
-- (campaign, date, device, network, state) tuple: one with
-- Location type = 'Physical location' (where the user actually was) and
-- one with Location type = 'Location of interest' (the geo signal the
-- auction matched on, when physical wasn't resolvable). The two types are
-- COMPLEMENTARY, not overlapping — each impression appears in exactly one.
-- Filtering out Interest rows undercounts spend and impressions for
-- historical periods when the campaign manager had location-of-interest
-- targeting enabled (2025 on Home). Loading both, distinguished by a
-- location_type column, preserves reconciliation against campaign tables.
--
-- Common analysis: filter WHERE location_type = 'PHYSICAL' for
-- physical-state analyses. Don't filter for reconciliation against
-- campaign_performance_*_daily totals.
--
-- This script drops and recreates the table. Any data currently in
-- geo_performance_daily for any account will be lost — re-run the loader
-- with --backfill after applying this migration.

DROP TABLE IF EXISTS geo_performance_daily;

CREATE TABLE geo_performance_daily (
    account_id                       INTEGER NOT NULL REFERENCES accounts(id),
    campaign_id                      TEXT    NOT NULL REFERENCES campaigns(campaign_id),

    -- Dimensions (natural key)
    date                             TEXT    NOT NULL,
    device                           TEXT    NOT NULL,
    network                          TEXT    NOT NULL,
    geo_state                        TEXT    NOT NULL,
    location_type                    TEXT    NOT NULL,  -- 'PHYSICAL' or 'INTEREST'

    -- Metrics
    impressions                      INTEGER,
    clicks                           INTEGER,
    cost_micros                      INTEGER,
    conversions                      REAL,
    conversion_value                 REAL,
    all_conversions                  REAL,
    all_conversion_value             REAL,

    -- Snapshot fields (approximated from current campaigns table values;
    -- to be backfilled when change-history ingestion lands)
    budget_micros_that_day           INTEGER,
    bidding_strategy_type_that_day   TEXT,
    bidding_strategy_id_that_day     TEXT,
    target_cpa_micros_that_day       INTEGER,
    target_roas_that_day             REAL,

    -- Provenance
    last_synced_at                   TEXT    NOT NULL,

    PRIMARY KEY (campaign_id, date, device, network, geo_state, location_type)
);

CREATE INDEX IF NOT EXISTS idx_geo_perf_account_date
    ON geo_performance_daily (account_id, date);

CREATE INDEX IF NOT EXISTS idx_geo_perf_date
    ON geo_performance_daily (date);

CREATE INDEX IF NOT EXISTS idx_geo_perf_state
    ON geo_performance_daily (geo_state);

CREATE INDEX IF NOT EXISTS idx_geo_perf_network_date
    ON geo_performance_daily (network, date);

CREATE INDEX IF NOT EXISTS idx_geo_perf_loctype_date
    ON geo_performance_daily (location_type, date);
