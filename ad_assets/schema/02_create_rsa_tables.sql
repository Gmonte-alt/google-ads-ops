-- ============================================================================
-- 02_create_rsa_tables.sql  --  ad_assets/schema/
-- Phase 2 (RSA layer) for paid_search.db -- gate-approved 2026-07-18.
--
-- Probe basis (probe_rsa_asset_grain.py): metrics ARE populated at RSA asset
-- grain (stale "impressions only" claims overturned); performance_label is
-- universally NOT_APPLICABLE in this account today; no combination ever
-- exceeded ~1.1% of an ad's impressions.
--
-- GATE RULINGS ENCODED HERE:
--   * click_type is DELIBERATELY OMITTED at this grain (Decision 3): a
--     headline click IS the ad click -- the sitelink-style split has no
--     meaning here. Do not "fix" this by adding it later without a gate.
--   * No raw combination storage (Decision 2): 4,462 combos/ad at <=1.1%
--     share is cardinality without information. Revisit only with a
--     use case in hand.
--   * device is in the perf PK either way (Decision 1); whether a
--     device-SEGMENTED sync pass runs depends on the Q10 probe verdict.
--     If rejected, rows only ever carry device='ALL' and nothing changes.
--   * Cross-additivity is checked as a first-sync acceptance assertion
--     against ad_group_ad ad-level totals (Decision 4); the RSA-grain
--     duplication ratio is PRINTED on first run, learned not assumed.
--
-- *** PRESENCE-CONDITIONED METRICS -- READ BEFORE CONSUMING ***
-- Every RSA impression shows 2-3 headlines + 2 descriptions together.
-- Per-asset rows measure "activity on impressions where this asset was
-- shown" -- NOT exclusive credit. Same rules as asset_performance_daily:
--   valid:   per-asset trends; ratios within one asset; relative
--            comparisons between assets (diluted, directional only)
--   invalid: summing ANY metric across assets; treating an asset's
--            conversions as ITS conversions
-- Ad-level totals come from ad_group_ad (02 sync stores them in rsa_ads
-- perf or queries them live); campaign totals from the campaign report.
--
-- APPLY VIA apply_rsa_schema.py -- dry-run default, required --db,
-- backup-first, small-DB tripwire.
-- ============================================================================

PRAGMA foreign_keys = ON;

-- ----------------------------------------------------------------------------
-- creative_angle_taxonomy: THE controlled vocabulary (gate ruling: one
-- vocabulary, seeded from the V11 angle families -- never free-text, never a
-- parallel scheme). Column-name synonyms for cross-tool clarity:
--   theme_tag == "messaging_angle"; usp_category == "value_prop_focus";
--   buyer_journey_stage ~= "target_audience" framing.
-- Tags on assets / rsa_ads validate against this table in tagging and
-- analysis code (advisory -- no FK, since assets.theme_tag predates it).
-- Seed via seed_creative_angle_taxonomy.py.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS creative_angle_taxonomy (
    angle_code     TEXT PRIMARY KEY,     -- stable snake_case, e.g. 'carrier_conquest'
    angle_name     TEXT NOT NULL,        -- display name as used in the V11 workbook
    dimension      TEXT NOT NULL
                   CHECK (dimension IN ('angle_family','usp_category',
                                        'buyer_journey_stage','intent_set')),
                   -- FOUR dimensions (gate-confirmed 2026-07-18). V11 carries
                   -- two real vocabularies at different grains:
                   --   angle_family = per-HEADLINE messaging axis
                   --   intent_set   = per-AD targeting context
                   -- COLUMN->DIMENSION MAP (same column name, different
                   -- vocabulary by table -- never union theme_tags across
                   -- tables without translating):
                   --   assets.theme_tag   validates vs dimension='angle_family'
                   --   rsa_ads.theme_tag  validates vs dimension='intent_set'
                   -- Tagging/validation scripts enforce PER-TABLE dimension,
                   -- not mere existence in this table.
                   -- usp_category / buyer_journey_stage are deliberately
                   -- UNSEEDED: V11 fuses USPs into angle descriptions; seed
                   -- only if a real list emerges, through this same table.
    description    TEXT,
    effective_date TEXT NOT NULL,
    source         TEXT NOT NULL DEFAULT 'V11'
);

-- ----------------------------------------------------------------------------
-- rsa_ads: one row per RSA. The FINGERPRINT is a stored fact with a
-- deterministic recipe (gate note 1) -- any script computing it MUST match:
--   1. take the ad's HEADLINE texts only (descriptions excluded)
--   2. normalize each: lowercase, strip, collapse internal whitespace to
--      single spaces
--   3. sort the normalized list
--   4. join with '||', md5, first 10 hex chars
-- (The 07-18 analysis script sorted but did not normalize case/whitespace;
-- the DB recipe is authoritative from Phase 2 -- analysis migrates to match.)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rsa_ads (
    account_id     INTEGER NOT NULL,
    ad_id          INTEGER NOT NULL,
    ad_group_id    INTEGER NOT NULL,
    campaign_id    INTEGER NOT NULL,
    ad_status      TEXT    NOT NULL,      -- AdGroupAdStatus enum .name
    fingerprint    TEXT    NOT NULL,      -- recipe above
    variant_label  TEXT,                  -- analysis-owned (e.g. 'V11'); sync never writes
    theme_tag      TEXT,                  -- analysis-owned; INTENT SET of the ad --
                                          -- validates vs creative_angle_taxonomy
                                          -- dimension='intent_set' (NOT angle_family)
    first_seen_date TEXT   NOT NULL,
    last_seen_date  TEXT   NOT NULL,
    last_synced_at  TEXT   NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (account_id, ad_id)
);

CREATE INDEX IF NOT EXISTS idx_rsa_ads_fingerprint
    ON rsa_ads (account_id, fingerprint);
CREATE INDEX IF NOT EXISTS idx_rsa_ads_campaign
    ON rsa_ads (account_id, campaign_id);

-- ----------------------------------------------------------------------------
-- asset_text_data: type-specific payload for TEXT assets (headlines /
-- descriptions), sibling of asset_sitelink_data on the same key pattern.
-- Parent rows in `assets` (asset_type='TEXT') are upserted by the sync
-- parents-first, exactly like sitelinks.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS asset_text_data (
    account_id     INTEGER NOT NULL,
    asset_id       INTEGER NOT NULL,
    text           TEXT    NOT NULL,
    asset_subtype  TEXT,                  -- May design: headline role taxonomy;
                                          -- analysis-owned, sync never writes
    last_synced_at TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (account_id, asset_id),
    FOREIGN KEY (account_id, asset_id) REFERENCES assets (account_id, asset_id)
);

-- ----------------------------------------------------------------------------
-- rsa_ad_assets: which text assets compose which RSA, with pinning and
-- Google's label. performance_label retained despite universal
-- NOT_APPLICABLE today (gate note 2): labels APPEARING after the lean-RSA
-- restructure is itself the success signal. Tombstone/revival semantics as
-- ruled for asset_associations (removed_date stamped, cleared on return).
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rsa_ad_assets (
    account_id        INTEGER NOT NULL,
    ad_id             INTEGER NOT NULL,
    asset_id          INTEGER NOT NULL,
    field_type        TEXT    NOT NULL,   -- ServedAssetFieldType/.field_type enum .name
    pinned_field      TEXT,               -- enum .name, NULL when unpinned
    performance_label TEXT,               -- enum .name (NOT_APPLICABLE today)
    first_seen_date   TEXT,
    last_seen_date    TEXT,
    removed_date      TEXT,
    last_synced_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (account_id, ad_id, asset_id, field_type),
    FOREIGN KEY (account_id, ad_id)    REFERENCES rsa_ads (account_id, ad_id),
    FOREIGN KEY (account_id, asset_id) REFERENCES assets  (account_id, asset_id)
);

CREATE INDEX IF NOT EXISTS idx_rsa_ad_assets_asset
    ON rsa_ad_assets (account_id, asset_id);

-- ----------------------------------------------------------------------------
-- rsa_asset_performance_daily: presence-conditioned per-asset metrics.
-- PK: (account_id, ad_id, asset_id, field_type, device, report_date)
-- device: 'ALL' unsegmented pass; segmented pass only if Q10 = ACCEPTED.
-- NO click_type column -- see gate ruling in header.
-- No FK to rsa_ad_assets: performance survives composition changes.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rsa_asset_performance_daily (
    account_id        INTEGER NOT NULL,
    ad_id             INTEGER NOT NULL,
    asset_id          INTEGER NOT NULL,
    field_type        TEXT    NOT NULL,
    device            TEXT    NOT NULL DEFAULT 'ALL',
    report_date       TEXT    NOT NULL,
    impressions       INTEGER NOT NULL DEFAULT 0,
    clicks            INTEGER NOT NULL DEFAULT 0,
    cost_micros       INTEGER NOT NULL DEFAULT 0,
    conversions       REAL    NOT NULL DEFAULT 0,
    conversions_value REAL    NOT NULL DEFAULT 0,
    last_synced_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (account_id, ad_id, asset_id, field_type, device, report_date),
    FOREIGN KEY (account_id, ad_id)    REFERENCES rsa_ads (account_id, ad_id),
    FOREIGN KEY (account_id, asset_id) REFERENCES assets  (account_id, asset_id)
);

CREATE INDEX IF NOT EXISTS idx_rsa_perf_date
    ON rsa_asset_performance_daily (account_id, report_date);
CREATE INDEX IF NOT EXISTS idx_rsa_perf_ad
    ON rsa_asset_performance_daily (account_id, ad_id, report_date);
