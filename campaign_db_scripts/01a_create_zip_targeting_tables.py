"""
01a_create_zip_targeting_tables.py  [v1 — gate-ratified DDL, 2026-10-07]
==================================
Term Life Zip-Targeting Subsystem — Quote.com Google Ads
Folder: C:\\Projects\\google-ads-ops\\campaign_db_scripts\\

Creates the two ratified tables for the zip-targeting subsystem
(sub-bullet of 01_create_campaign_tables.py — structure DDL):

  campaign_zip_targeting      — CURRENT-STATE structure snapshot, maintained
                                like `campaigns` by 02a_sync_zip_targeting.py.
                                Stores ALL LOCATION criteria (not postal only):
                                CR45-style catchalls are country-positive +
                                zip-negative, so membership for them is a
                                COMPLEMENT — never a positive-row lookup.
                                `zip` is TEXT, 5-digit, leading zeros preserved
                                (hard rule — validated on write by 02a).
                                geo_target_constant_id is informational only:
                                NO FK to geo_target_constants (ratified ruling;
                                222 criteria ids missing locally + 9 API-dead).
                                Non-US postal rows are STORED with country_code
                                (ratified — hygiene findings, not noise).

  zip_targeting_transitions   — SYSTEM OF RECORD for targeting history
                                (architect ruling, 2026-10-07). Written by 02a's
                                diff-before-delete pass. Current state minus
                                replayed transitions reconstructs any as-of date
                                exactly (daily grain). A zip "move" is
                                REMOVED(campaign A) + ADDED(campaign B) on the
                                same observed_date — derived in v_zip_moves,
                                never stored as a type. plan_label is Layer-2
                                annotation (ZIPMOVE-YYYYMMDD convention): plans
                                document WHY, diffs record WHAT; unlabeled
                                transitions are themselves an audit finding.
                                As-of reconstruction begins at 02a's FIRST run
                                (baseline day zero) — earlier dates get
                                current-state mapping, labeled approximation.

Also creates two convenience views (cheap, droppable — flag at next gate
visit if unwanted):
  v_zip_targeting_map  — the first business deliverable: positive zip →
                         ENABLED campaign, with campaign names.
  v_zip_moves          — same-day REMOVED+ADDED pairs per zip (the derived
                         "move" event the architect specified).

READ-ONLY elsewhere: this script creates objects only — no data writes,
no drops, fully idempotent (IF NOT EXISTS throughout).

RUN:
  conda activate paid-search
  python campaign_db_scripts\\01a_create_zip_targeting_tables.py
"""

import sqlite3
from pathlib import Path
from datetime import datetime, timezone

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")


DDL = [
    # ── campaign_zip_targeting — current-state structure snapshot ─────────
    """
    CREATE TABLE IF NOT EXISTS campaign_zip_targeting (
        account_id              INTEGER NOT NULL,
        campaign_id             TEXT    NOT NULL,
        criterion_id            TEXT    NOT NULL,
        zip                     TEXT,
        geo_target_constant_id  INTEGER,
        geo_type                TEXT,
        geo_name                TEXT,
        country_code            TEXT,
        is_negative             INTEGER NOT NULL DEFAULT 0,
        bid_modifier            REAL,
        status                  TEXT,
        last_synced_at          TEXT    NOT NULL,
        PRIMARY KEY (campaign_id, criterion_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_czt_account_zip
        ON campaign_zip_targeting(account_id, zip)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_czt_campaign
        ON campaign_zip_targeting(campaign_id, is_negative)
    """,

    # ── zip_targeting_transitions — system of record for history ─────────
    """
    CREATE TABLE IF NOT EXISTS zip_targeting_transitions (
        account_id    INTEGER NOT NULL,
        observed_date TEXT    NOT NULL,
        campaign_id   TEXT    NOT NULL,
        criterion_id  TEXT,
        zip           TEXT,
        geo_type      TEXT,
        is_negative   INTEGER,
        change_type   TEXT    NOT NULL,
        prev_status   TEXT,
        new_status    TEXT,
        plan_label    TEXT,
        PRIMARY KEY (account_id, observed_date, campaign_id,
                     criterion_id, change_type)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_ztt_zip
        ON zip_targeting_transitions(account_id, zip, observed_date)
    """,

    # ── v_zip_targeting_map — zip → enabled campaign (the deliverable) ───
    """
    CREATE VIEW IF NOT EXISTS v_zip_targeting_map AS
    SELECT
        t.account_id,
        t.zip,
        t.campaign_id,
        c.campaign_name,
        t.criterion_id,
        t.country_code,
        t.status            AS criterion_status,
        t.last_synced_at
    FROM campaign_zip_targeting t
    JOIN campaigns c
      ON c.campaign_id = t.campaign_id
    WHERE t.geo_type LIKE '%ostal%'
      AND t.is_negative = 0
      AND c.status = 'ENABLED'
    """,

    # ── v_zip_moves — same-day REMOVED+ADDED pairs = a zip move ──────────
    """
    CREATE VIEW IF NOT EXISTS v_zip_moves AS
    SELECT
        r.account_id,
        r.observed_date,
        r.zip,
        r.campaign_id       AS from_campaign_id,
        a.campaign_id       AS to_campaign_id,
        r.is_negative,
        COALESCE(a.plan_label, r.plan_label) AS plan_label
    FROM zip_targeting_transitions r
    JOIN zip_targeting_transitions a
      ON a.account_id    = r.account_id
     AND a.observed_date = r.observed_date
     AND a.zip           = r.zip
     AND a.is_negative   = r.is_negative
     AND a.change_type   = 'ADDED'
     AND a.campaign_id  != r.campaign_id
    WHERE r.change_type = 'REMOVED'
      AND r.zip IS NOT NULL
    """,
]


def main():
    print("=" * 65)
    print("01a_create_zip_targeting_tables.py  [v1 — ratified 2026-10-07]")
    print(f"Database : {DB_PATH}")
    print(f"Started  : {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    print("=" * 65)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row

    for stmt in DDL:
        conn.execute(stmt)
    conn.commit()

    # ── Verification ──────────────────────────────────────────────────────
    for table in ("campaign_zip_targeting", "zip_targeting_transitions"):
        print(f"\n  {table}:")
        for col in conn.execute(f"PRAGMA table_info({table})"):
            pk = "  PK" if col["pk"] else ""
            nn = "  NOT NULL" if col["notnull"] else ""
            print(f"    {col['name']:<24} {col['type']:<8}{nn}{pk}")
        idx = [r["name"] for r in conn.execute(
            f"PRAGMA index_list({table})") if not r["name"].startswith("sqlite_")]
        print(f"    indexes: {idx}")

    views = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='view' "
        "AND name IN ('v_zip_targeting_map','v_zip_moves')")]
    print(f"\n  Views created: {views}")

    conn.close()
    print(f"\nDone. Next: python campaign_db_scripts\\02a_sync_zip_targeting.py "
          f"--account term_life --dry-run")


if __name__ == "__main__":
    main()
