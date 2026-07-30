-- ============================================================================
-- 03_add_rsa_ad_perf.sql  --  ad_assets/schema/
-- Addendum to the Phase 2 RSA layer -- gate-approved 2026-07-22.
--
-- WHY THIS TABLE EXISTS (the lesson, written down): the RSA layer initially
-- persisted the DECOMPOSITION (per-asset, presence-conditioned rows) and
-- threw away the TOTAL (ad-grain rows) -- yet the ad is the only
-- exclusively-attributed grain and the testing regime's unit of measurement.
-- Variant x campaign comparisons MUST run on this table; summing
-- rsa_asset_performance_daily rows per ad inflates by the A5 ratios
-- (headlines ~2.78x, descriptions ~1.49x, learned 2026-07-22).
--
-- Semantics: FULLY ADDITIVE. Ad metrics are exclusive -- sum freely across
-- ads, campaigns, dates, fingerprints. No click_type dimension (Decision 3
-- logic applies identically at ad grain). Dual-pass device: 'ALL'
-- authoritative, segmented rows for splits.
--
-- CROSS-TABLE INVARIANT (the persistable A4): for every
-- (account_id, ad_id, device='ALL', report_date), each row in
-- rsa_asset_performance_daily must satisfy
--   asset.clicks <= ad.clicks  AND  asset.conversions <= ad.conversions
-- against this table. Checked by 02_sync_rsa.py on every run, and checkable
-- by anyone, any time, in SQL.
--
-- Analysis convention: variant x campaign readouts carry a click floor
-- (HAVING SUM(clicks) >= <floor>) -- thin cells make 3-click CVR heroes.
--
-- No tombstoning: perf history is history, consistent with every perf table.
-- APPLY VIA apply_rsa_ad_perf.py (standard migration discipline).
-- ============================================================================

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS rsa_ad_performance_daily (
    account_id        INTEGER NOT NULL,
    ad_id             INTEGER NOT NULL,
    device            TEXT    NOT NULL DEFAULT 'ALL',   -- 'ALL' or Device enum .name
    report_date       TEXT    NOT NULL,                 -- ISO yyyy-mm-dd
    impressions       INTEGER NOT NULL DEFAULT 0,
    clicks            INTEGER NOT NULL DEFAULT 0,
    cost_micros       INTEGER NOT NULL DEFAULT 0,
    conversions       REAL    NOT NULL DEFAULT 0,
    conversions_value REAL    NOT NULL DEFAULT 0,
    last_synced_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (account_id, ad_id, device, report_date),
    FOREIGN KEY (account_id, ad_id) REFERENCES rsa_ads (account_id, ad_id)
);

CREATE INDEX IF NOT EXISTS idx_rsa_ad_perf_date
    ON rsa_ad_performance_daily (account_id, report_date);
