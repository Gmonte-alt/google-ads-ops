"""
01b_create_zip_performance_table.py  [v1 — Phase 2, consumer named 2026-10-07]
===================================
Term Life Zip-Targeting Subsystem — Quote.com Google Ads
Folder: C:\\Projects\\google-ads-ops\\campaign_db_scripts\\

Creates campaign_performance_zip_daily — the Phase-2 zip-grain performance
table. Shape ratified at the 2026-10-07 architect gate (memo §4, resolution
path §5 ratified explicitly); build condition met same day: volume GREEN
(~943K rows/yr at device grain, probe 2026-10-07) AND consumer named
(sub-campaign geography + Quote-side margin by cluster — analysis thread).

DOC LINES (the table's contract):
  - Source: geographic_view + segments.geo_target_postal_code, synced by
    03a_sync_zip_performance.py. Grain: campaign x date x device x
    location_type x zip. NATIVE device splits only — NO device='ALL' rollup
    (mirror of campaign_performance_geo_daily's Google side): sum devices
    for all-device reads.
  - NATIVE metrics only, same as state grain. On acct 29 native conversions
    blend leads + zip submits + calls — NOT a lead count; routed reads live
    at keyword/campaign grain per the conversion-routing map.
  - NOT a spend-complete view: zip grain covers ~91% of cpd spend
    (matched-locations behavior; probe wk 9/27-10/3: $49,205 of $54,109).
    Reconcile campaign rollups against campaign_performance_daily, never
    against this table.
  - Zip is SERVED location (100% LOCATION_OF_PRESENCE under current
    settings), not targeted location. Served zips are a SUPERSET of the
    positive targeting lists (CR45 serves via country-minus-negatives).
  - zip: TEXT, 5-digit, leading zeros preserved; NULL when the geo constant
    is unresolvable or its name fails the 5-digit rule (row kept, counted —
    spend is never dropped for a naming problem). geo_target_constant_id is
    informational — NO FK to geo_target_constants (ratified ruling).
  - No-Sunday schedule: absent/zero Sundays are correct data; per-day
    denominators use 6-day weeks.
  - As-of joins to campaign_zip_targeting are exact from 2026-10-07
    (baseline) via zip_targeting_transitions; earlier dates get
    current-state mapping, labeled approximation.

Idempotent, creates-only, no drops, no data writes.

RUN:
  conda activate paid-search
  python campaign_db_scripts\\01b_create_zip_performance_table.py
"""

import sqlite3
from pathlib import Path
from datetime import datetime, timezone

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")

DDL = [
    """
    CREATE TABLE IF NOT EXISTS campaign_performance_zip_daily (
        account_id              INTEGER NOT NULL,
        campaign_id             TEXT    NOT NULL,
        date                    TEXT    NOT NULL,
        device                  TEXT    NOT NULL,
        location_type           TEXT    NOT NULL,
        geo_target_constant_id  INTEGER NOT NULL,
        zip                     TEXT,
        impressions             INTEGER,
        clicks                  INTEGER,
        cost_micros             INTEGER,
        conversions             REAL,
        conversion_value        REAL,
        all_conversions         REAL,
        all_conversion_value    REAL,
        last_synced_at          TEXT    NOT NULL,
        PRIMARY KEY (campaign_id, date, device, location_type,
                     geo_target_constant_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_cpzd_account_date
        ON campaign_performance_zip_daily(account_id, date)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_cpzd_account_zip_date
        ON campaign_performance_zip_daily(account_id, zip, date)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_cpzd_campaign_date
        ON campaign_performance_zip_daily(campaign_id, date)
    """,
]


def main():
    print("=" * 65)
    print("01b_create_zip_performance_table.py  [v1 — Phase 2]")
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

    print("\n  campaign_performance_zip_daily:")
    for col in conn.execute("PRAGMA table_info(campaign_performance_zip_daily)"):
        pk = "  PK" if col["pk"] else ""
        nn = "  NOT NULL" if col["notnull"] else ""
        print(f"    {col['name']:<24} {col['type']:<8}{nn}{pk}")
    idx = [r["name"] for r in conn.execute(
        "PRAGMA index_list(campaign_performance_zip_daily)")
        if not r["name"].startswith("sqlite_")]
    print(f"    indexes: {idx}")

    conn.close()
    print(f"\nDone. Next: python campaign_db_scripts\\03a_sync_zip_performance.py "
          f"--account term_life --dry-run --start 2026-09-27 --end 2026-10-03")


if __name__ == "__main__":
    main()
