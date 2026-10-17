-- ============================================================================
-- 05_add_asset_campaign_perf.sql  --  ad_assets/schema/  --  FOR GATE REVIEW
-- Campaign-split performance for account-linked assets (Q11 probe: GO, full
-- fidelity -- customer_asset carries campaign.id AND segments.click_type in
-- v23; probe output attached to the gate request).
--
-- WHY: asset_performance_daily is LINK-grain (asset x association) -- correct
-- and unchanged. Customer-linked sitelinks therefore have no campaign split
-- there. This table adds the CAMPAIGN-ATTRIBUTED grain for those assets:
-- performance of an asset within a campaign REGARDLESS of link level.
-- First consumer: sitelink_direct_report.py (Share/ROAS/VPC by campaign).
--
-- SEMANTICS (all Phase-1 rules carry over):
--   * impressions: presence-conditioned; authoritative on click_type='ALL'
--     rows only (repeat across click_type rows, never divided)
--   * clicks/conversions/value/cost PARTITION across click_type within one
--     (asset, campaign, date). click_type = KIND of click, not which asset
--     was clicked: 'SITELINKS' rows include co-served neighbors' sitelink
--     clicks (~1.5x). Per-asset direct credit = click_type='THIS_ASSET'
--     rows (interaction segment, 01c sync; excluded from partition checks;
--     never use their impressions). Corrected 2026-09-03.
--   * across campaigns for one asset: campaign attribution sums ~= the
--     customer-level total in asset_performance_daily (reconciliation is a
--     WARN assertion in the sync, not an equality -- Google's attribution
--     can shade totals slightly)
--   * NEVER sum across assets (co-serving, ~3.3x for sitelinks)
--   * device: column present for pattern-consistency; v1 syncs 'ALL' only
--     (no consumer needs the device split; adding it later is a sync change,
--     not a schema change)
--
-- No FK to asset_associations: campaign attribution exists precisely for
-- assets NOT linked at campaign level. No tombstoning: perf history is
-- history. APPLY VIA apply_asset_campaign_perf.py.
-- ============================================================================

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS asset_campaign_performance_daily (
    account_id        INTEGER NOT NULL,
    asset_id          INTEGER NOT NULL,
    campaign_id       INTEGER NOT NULL,
    device            TEXT    NOT NULL DEFAULT 'ALL',
    click_type        TEXT    NOT NULL DEFAULT 'ALL',   -- 'ALL' or ClickType enum .name
    report_date       TEXT    NOT NULL,                 -- ISO yyyy-mm-dd
    impressions       INTEGER NOT NULL DEFAULT 0,
    clicks            INTEGER NOT NULL DEFAULT 0,
    cost_micros       INTEGER NOT NULL DEFAULT 0,
    conversions       REAL    NOT NULL DEFAULT 0,
    conversions_value REAL    NOT NULL DEFAULT 0,
    last_synced_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (account_id, asset_id, campaign_id, device, click_type, report_date),
    FOREIGN KEY (account_id, asset_id) REFERENCES assets (account_id, asset_id)
);

CREATE INDEX IF NOT EXISTS idx_asset_camp_perf_date
    ON asset_campaign_performance_daily (account_id, report_date);
CREATE INDEX IF NOT EXISTS idx_asset_camp_perf_campaign
    ON asset_campaign_performance_daily (account_id, campaign_id, report_date);
