-- ============================================================================
-- 04_add_creative_lineage.sql  --  ad_assets/schema/  --  DRAFT FOR GATE REVIEW
-- Creative lineage DAG + repository views. Proposed by the creative-strategy
-- thread 2026-07-23; shaped to house conventions by the measurement thread.
-- GATE-APPROVED 2026-07-23. Apply via apply_creative_lineage.py.
-- ACCEPTANCE TEST (architect requirement): after each authored creative
-- loads, the SYNCED fingerprint must equal the PRE-REGISTERED one
-- (reconcile_fingerprint.py). Divergence = recipe drift = every pre-load
-- lineage edge is suspect. This check is the load sequence's gate, not
-- an incidental.
--
-- Design (strategy thread's, adopted):
--   identity  = fingerprint (immutable, content-derived)      -- already solved
--   reference = variant_label (human alias -> fingerprint)    -- already solved
--   history   = THIS: a DAG of parent edges with authoring
--               semantics and a decision-record pointer
-- Branching is native: multiple parent rows per fingerprint. Roots simply
-- have no rows. Fingerprints are computable pre-load (canonical recipe), so
-- lineage rows can be inserted at AUTHORING time, before the creative exists
-- in rsa_ads -- hence no FK to rsa_ads (deliberate; the repository_current
-- view is where lineage meets live state).
--
-- Naming convention (adopted with this table): new creatives are labeled
-- {family}-{seq} (BCC-01, BCC-02...) at authoring time; V-numbers remain as
-- legacy aliases for the reverse-engineered generation.
--
-- Decision records live as FILES in ad_assets\decisions\ (repo-versioned);
-- decision_record_path stores the relative path, not content.
-- ============================================================================

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS creative_lineage (
    account_id          INTEGER NOT NULL,
    fingerprint         TEXT    NOT NULL,   -- the child creative
    parent_fingerprint  TEXT    NOT NULL,
    change_type         TEXT    NOT NULL
                        CHECK (change_type IN
                          ('line_swap','cut','add','compliance_rewrite',
                           'intent_fork','synthesis')),
    change_summary      TEXT,               -- one line: what was taken/changed from THIS parent
    authored_date       TEXT    NOT NULL,   -- ISO yyyy-mm-dd
    decision_record_path TEXT,              -- e.g. 'decisions/creative_decision_record_BCC-01.md'
    created_at          TEXT    NOT NULL DEFAULT (datetime('now')),  -- authoring-time row; nothing syncs this table
    PRIMARY KEY (account_id, fingerprint, parent_fingerprint, change_type)
);

CREATE INDEX IF NOT EXISTS idx_lineage_parent
    ON creative_lineage (account_id, parent_fingerprint);

-- ----------------------------------------------------------------------------
-- Repository view (a): current state -- enabled creatives with alias, tag,
-- scale, and lineage depth-1 context.
-- ----------------------------------------------------------------------------
CREATE VIEW IF NOT EXISTS creative_repository_current AS
SELECT r.account_id,
       r.fingerprint,
       MAX(r.variant_label)                    AS label,
       MAX(r.theme_tag)                        AS theme_tag,
       COUNT(*)                                AS n_enabled_ads,
       COUNT(DISTINCT r.campaign_id)           AS n_campaigns,
       MIN(r.first_seen_date)                  AS first_seen,
       MAX(r.last_seen_date)                   AS last_seen,
       (SELECT GROUP_CONCAT(parent_fingerprint)
        FROM creative_lineage l
        WHERE l.account_id = r.account_id
          AND l.fingerprint = r.fingerprint)   AS parents
FROM rsa_ads r
WHERE r.ad_status = 'ENABLED'
GROUP BY r.account_id, r.fingerprint;

-- ----------------------------------------------------------------------------
-- Repository view (b): lineage edges resolved to human aliases where known.
-- ----------------------------------------------------------------------------
CREATE VIEW IF NOT EXISTS creative_lineage_view AS
SELECT l.account_id,
       l.fingerprint,
       (SELECT MAX(variant_label) FROM rsa_ads r
        WHERE r.account_id=l.account_id AND r.fingerprint=l.fingerprint)        AS child_label,
       l.parent_fingerprint,
       (SELECT MAX(variant_label) FROM rsa_ads r
        WHERE r.account_id=l.account_id AND r.fingerprint=l.parent_fingerprint) AS parent_label,
       l.change_type, l.change_summary, l.authored_date, l.decision_record_path
FROM creative_lineage l;

-- ----------------------------------------------------------------------------
-- Taxonomy rows for the same gate visit (seed-script pattern; INSERT OR
-- REPLACE keeps this idempotent):
-- ----------------------------------------------------------------------------
INSERT OR REPLACE INTO creative_angle_taxonomy
    (angle_code, angle_name, dimension, description, effective_date, source)
VALUES
    ('brand_compliant_core', 'Brand-Compliant Core', 'intent_set',
     'DKI-free replacement copy for brand ad groups -- location-insertion relevance, no carrier terms; compliance-driven',
     '2026-07-23', 'BCC-01'),
    ('renewal_repricing', 'Renewal Repricing', 'intent_set',
     'Repriced existing policyholders -- renewal-increase/rate-hike switcher intent; distinct from ca_non_renewal (availability distress)',
     '2026-07-23', 'strategy-thread');

-- ----------------------------------------------------------------------------
-- BCC-01 lineage seed (pre-registered fingerprint e1fd7d1595) -- EXAMPLE rows
-- for the gate to see the shape; insert on approval, at load time:
--
-- INSERT INTO creative_lineage VALUES
--  (1,'e1fd7d1595','fdb8e75afa','synthesis',
--   'serve-survivor core lines (zip action, $98 LOCATION, 2026 comparison) from V1',
--   '2026-07-23','decisions/creative_decision_record_BCC-01.md',datetime('now')),   -- created_at
--  (1,'e1fd7d1595','9265069966','synthesis',
--   'brand-group lines (Prices In 2026, Instant Home Quote, LOCATION Best Rates) from V3',
--   '2026-07-23','decisions/creative_decision_record_BCC-01.md',datetime('now')),
--  (1,'e1fd7d1595','2c9e3190c9','compliance_rewrite',
--   'location-insertion relevance pattern + geo/recency lines from V12; replaces DKI as the dynamic element',
--   '2026-07-23','decisions/creative_decision_record_BCC-01.md',datetime('now'));
-- ----------------------------------------------------------------------------
