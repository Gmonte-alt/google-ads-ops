-- ============================================================================
-- 01_create_asset_tables.sql  --  ad_assets/schema/
-- Asset DB layer for paid_search.db (SQLite, WAL mode)
--
-- Built to the PK ruling from the grain probe (probe_asset_grain.py, 2026-07-16):
--   Q1: device segmentation SUPPORTED on asset views
--   Q2: performance is reported per (asset x association) -- same asset_id
--       returns separate metric rows per campaign, and customer-level
--       associations are a separate population from campaign-level
--   Q3: conversions/conversions_value ARE populated at asset grain
--       (asset layer is GP-capable and can join the profit framework)
--
-- HARD REQUIREMENTS FOR THE SYNC (from charter):
--   1. ENUMS: all enum fields (segments.device, asset.type, field_type,
--      association status, performance_label, approval status) must be
--      converted via the client library's enum .name property, e.g.
--      row.segments.device.name -- NEVER a hand-written int->string dict.
--      (A hand-written mapping is what inverted match_type in
--      keyword_instances for months.)
--   2. TRIPLE-PASS SEGMENT SYNC: three passes per day --
--        (device='ALL', click_type='ALL')   unsegmented, authoritative totals
--        (device=X,     click_type='ALL')   device split
--        (device='ALL', click_type=X)       click-type split
--      No device x click_type cross pass in v1 (gate ruling 2026-07-17;
--      revisit only if a real analysis needs it). Consumers filter both
--      ='ALL' by house convention for totals.
--   3. IDENTITY: association_id is always the IMMUTABLE id
--      (customer_id / campaign_id / ad_group_id) -- never a name.
--
-- APPLY VIA apply_asset_schema.py (same directory) -- dry-run default,
-- required --db, backup-first, small-DB tripwire. Never executed by hand.
--
-- Reconciled against the charter 2026-07-16: assets carries the May thematic
-- tag columns; sitelink child uses final_url_params_json; RSA layer
-- (rsa_ads, rsa_ad_assets, asset_text_data, asset_subtype) deferred to
-- Phase 2; no experiments table, no scored views.
-- ============================================================================

PRAGMA foreign_keys = ON;

-- ----------------------------------------------------------------------------
-- assets: one row per asset per account. Identity table.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS assets (
    account_id       INTEGER NOT NULL,           -- internal account id (1 = Google Home)
    asset_id         INTEGER NOT NULL,           -- Google asset.id
    asset_type       TEXT    NOT NULL,           -- AssetType enum .name (e.g. 'SITELINK', 'CALLOUT')
    asset_name       TEXT,                       -- asset.name where populated
    status           TEXT,                       -- asset-level status, enum .name ('ENABLED','REMOVED',...)
    approval_status  TEXT,                       -- policy approval, enum .name -- a disapproved
                                                 -- sitelink silently not serving is a thing this DB
                                                 -- must surface (performance_label deferred to Phase 2)
    -- thematic tags (May design; populated by analysis/tagging, not the API sync;
    -- theme_tag holds ANGLE-FAMILY values -- validates vs creative_angle_taxonomy
    -- dimension='angle_family' (rsa_ads.theme_tag uses dimension='intent_set';
    -- never union theme_tags across tables without translating);
    -- taxonomy must stay consistent with the V11 RSA angle-family taxonomy)
    theme_tag            TEXT,
    usp_category         TEXT,
    buyer_journey_stage  TEXT,
    first_seen_date  TEXT    NOT NULL,           -- ISO yyyy-mm-dd
    last_seen_date   TEXT    NOT NULL,
    last_synced_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (account_id, asset_id)
);

-- ----------------------------------------------------------------------------
-- asset_sitelink_data: type-specific payload for SITELINK assets.
-- One row per sitelink asset. Other asset types get sibling tables later
-- (asset_callout_data, asset_structured_snippet_data, ...) on the same
-- (account_id, asset_id) key pattern.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS asset_sitelink_data (
    account_id    INTEGER NOT NULL,
    asset_id      INTEGER NOT NULL,
    link_text     TEXT    NOT NULL,
    description1  TEXT,
    description2  TEXT,
    final_url     TEXT,
    final_url_mobile TEXT,
    final_url_params_json TEXT,                  -- URL custom params serialized as JSON (May design:
                                                 -- one JSON column, NOT key/value columns)
    last_synced_at TEXT   NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (account_id, asset_id),
    FOREIGN KEY (account_id, asset_id) REFERENCES assets (account_id, asset_id)
);

-- ----------------------------------------------------------------------------
-- asset_associations: where each asset is attached.
-- Grain: (asset x level x container). Rows are RETAINED when an association
-- disappears (removed_date set, not deleted) because performance history
-- outlives the link -- same reasoning as campaign_name_history.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS asset_associations (
    account_id        INTEGER NOT NULL,
    asset_id          INTEGER NOT NULL,
    association_level TEXT    NOT NULL
                      CHECK (association_level IN ('CUSTOMER','CAMPAIGN','AD_GROUP')),
    association_id    INTEGER NOT NULL,          -- customer_id / campaign_id / ad_group_id
    field_type        TEXT,                      -- AssetFieldType enum .name (e.g. 'SITELINK')
    link_status       TEXT,                      -- AssetLinkStatus enum .name ('ENABLED','REMOVED',...)
    first_seen_date   TEXT,
    last_seen_date    TEXT,
    removed_date      TEXT,                      -- set when the association stops appearing
    last_synced_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (account_id, asset_id, association_level, association_id),
    FOREIGN KEY (account_id, asset_id) REFERENCES assets (account_id, asset_id)
);

CREATE INDEX IF NOT EXISTS idx_asset_assoc_container
    ON asset_associations (account_id, association_level, association_id);

-- ----------------------------------------------------------------------------
-- asset_performance_daily: THE ruled grain (gate rulings 2026-07-16/17).
-- PK: (account_id, asset_id, association_level, association_id,
--      device, click_type, report_date)
--
-- device:     'ALL' for unsegmented passes, else Device enum .name
--             ('MOBILE','DESKTOP','TABLET','CONNECTED_TV','OTHER').
-- click_type: 'ALL' for unsegmented passes, else ClickType enum .name.
--             For sitelink assets: 'SITELINKS' = clicks on the sitelink itself
--             (asset engagement); 'URL_CLICKS' = headline clicks while the
--             asset was showing (presence context, NOT asset performance);
--             'AD_IMAGE' = image-asset clicks on the same impressions.
--
-- *** METRIC ADDITIVITY RULES -- READ BEFORE CONSUMING THIS TABLE ***
-- WITHIN one asset's rows (same asset, association, device, date):
--   clicks / conversions / value / cost ARE additive across click_type
--     (they partition; sum across click_type == the 'ALL' row)
--   impressions are NOT additive -- repeated per click_type row, not
--     divided. Authoritative ONLY on click_type='ALL' rows.
-- ACROSS assets: NOTHING is additive. Sitelinks co-serve (~3.3 per
--   impression, per cross-additivity probe 2026-07-17), and every
--   co-shown asset's rows carry the same clicks/conversions. Summing
--   across assets multiplies reality by ~3.3x (this produced impossible
--   >100% share figures before it was caught). Any account-, campaign-,
--   or ad-group-wide total comes from the CAMPAIGN REPORT's click_type
--   partition (FROM campaign, segments.click_type) -- never this table.
-- What asset rows actually measure: PRESENCE-CONDITIONED performance --
--   "activity on impressions where this asset was shown", split by the
--   click type of the click. Not exclusive per-asset credit.
-- Conventions that follow:
--   valid: per-asset trends; relative comparisons between assets
--     (diluted by co-serving, directional only); ratios within one
--     asset's rows (e.g. its SITELINKS CVR vs its URL_CLICKS CVR)
--   invalid: summing any metric across assets; treating an asset's
--     SITELINKS conversions as ITS conversions; blending SITELINKS
--     and URL_CLICKS into one credited metric
--
-- No FK to asset_associations: performance rows must survive association
-- removal. FK to assets only.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS asset_performance_daily (
    account_id            INTEGER NOT NULL,
    asset_id              INTEGER NOT NULL,
    association_level     TEXT    NOT NULL
                          CHECK (association_level IN ('CUSTOMER','CAMPAIGN','AD_GROUP')),
    association_id        INTEGER NOT NULL,
    device                TEXT    NOT NULL,      -- 'ALL' or Device enum .name
    click_type            TEXT    NOT NULL DEFAULT 'ALL',  -- 'ALL' or ClickType enum .name
    report_date           TEXT    NOT NULL,      -- ISO yyyy-mm-dd
    impressions           INTEGER NOT NULL DEFAULT 0,
    clicks                INTEGER NOT NULL DEFAULT 0,
    cost_micros           INTEGER NOT NULL DEFAULT 0,
    conversions           REAL    NOT NULL DEFAULT 0,
    conversions_value     REAL    NOT NULL DEFAULT 0,
    all_conversions       REAL    NOT NULL DEFAULT 0,
    all_conversions_value REAL    NOT NULL DEFAULT 0,
    last_synced_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (account_id, asset_id, association_level, association_id, device, click_type, report_date),
    FOREIGN KEY (account_id, asset_id) REFERENCES assets (account_id, asset_id)
);

CREATE INDEX IF NOT EXISTS idx_asset_perf_date
    ON asset_performance_daily (account_id, report_date);
CREATE INDEX IF NOT EXISTS idx_asset_perf_container
    ON asset_performance_daily (account_id, association_level, association_id, report_date);

-- ----------------------------------------------------------------------------
-- Convenience view: current (non-removed) associations joined to sitelink
-- payload -- the "what is attached where, right now" question.
-- ----------------------------------------------------------------------------
CREATE VIEW IF NOT EXISTS asset_associations_current AS
SELECT a.account_id,
       a.asset_id,
       ast.asset_type,
       sd.link_text,
       a.association_level,
       a.association_id,
       a.field_type,
       a.link_status
FROM asset_associations a
JOIN assets ast
  ON ast.account_id = a.account_id AND ast.asset_id = a.asset_id
LEFT JOIN asset_sitelink_data sd
  ON sd.account_id = a.account_id AND sd.asset_id = a.asset_id
WHERE a.removed_date IS NULL
  AND (a.link_status IS NULL OR a.link_status != 'REMOVED');
