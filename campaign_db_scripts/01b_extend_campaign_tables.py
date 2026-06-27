"""
01b_extend_campaign_tables.py
==============================
Campaign Structure Analytics — Microsoft Ads schema extension
Folder: C:\\Projects\\google-ads-ops\\campaign_db_scripts\\

Adds the campaign_performance_network_daily table used by Microsoft's
Hybrid campaigns (search + Audience Network in the same campaign).

WHY A SEPARATE TABLE
    campaign_performance_daily assumes search network. Its impression
    share columns (search_impression_share, search_rank_lost_is,
    search_budget_lost_is, search_exact_match_is) are meaningless for
    Audience Network traffic. Blending Audience rows into that table
    would corrupt every IS aggregate.

    Microsoft's Hybrid campaigns route spend to both search and Audience
    from the same campaign, so a second table is required to capture
    Audience spend without polluting the search table.

    Google campaigns are search-only (Google's Display campaigns live
    in different campaigns entirely) so Google rows never land here.

NETWORK ROUTING (applied by 09_load_microsoft_campaign_performance.py)
    SEARCH bucket -> campaign_performance_daily
        - Microsoft sites and select traffic
        - Bing and web
        - AOL search               (keyword-triggered partner; treated
                                    as search per project convention)

    NON-SEARCH bucket -> campaign_performance_network_daily
        - Audience
        - Syndicated search partners (should not appear per SOP; warned
                                     and routed here if encountered)
        - Any unknown network string (warned and routed here as a
                                     conservative default)

SCHEMA NOTES
    - account_id INTEGER REFERENCES accounts(id), consistent with v2
      campaign schema (joins to accounts on the integer PK, not the
      platform_account_id string).
    - campaign_id TEXT REFERENCES campaigns(campaign_id). Loader
      assumes campaigns are loaded first (06 before 07). With
      PRAGMA foreign_keys = ON, writes will fail if the parent
      campaign row does not exist -- which is the desired behavior.
    - Primary key (campaign_id, date, device, network). network is
      part of the PK because a single campaign/date/device can have
      multiple network rows in Hybrid campaigns.
    - Audience-specific IS columns are included. Microsoft reports
      audience_network_impression_share in the performance CSV even
      though search IS is meaningless for Audience rows. These are
      stored here as decimals 0.0-1.0; NULL when below threshold or
      not reported by Microsoft.
    - Snapshot columns (budget_micros_that_day, bidding_strategy_*_that_day)
      mirror campaign_performance_daily. These are approximated from
      current campaigns values and will be updated when change history
      ingestion is added.

IDEMPOTENT
    CREATE TABLE IF NOT EXISTS. Safe to re-run. Does not drop the table,
    does not modify existing data, does not touch other tables.

Usage:
    conda activate paid-search
    python campaign_db_scripts/01b_extend_campaign_tables.py
"""

import sqlite3
import logging
from pathlib import Path
from datetime import datetime, timezone


# ── Configuration ─────────────────────────────────────────────────────────────

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Table definition ──────────────────────────────────────────────────────────
# Parallel structure to campaign_performance_daily, with these changes:
#   - `network` column added (part of PK)
#   - search_* impression share columns removed (meaningless off search)
#   - audience_* impression share columns added (Microsoft-specific)
# ──────────────────────────────────────────────────────────────────────────────

CREATE_NETWORK_DAILY = """
    CREATE TABLE IF NOT EXISTS campaign_performance_network_daily (
        campaign_id                      TEXT    NOT NULL REFERENCES campaigns(campaign_id),
        account_id                       INTEGER NOT NULL REFERENCES accounts(id),
        date                             TEXT    NOT NULL,
        device                           TEXT    NOT NULL,
        network                          TEXT    NOT NULL,

        impressions                      INTEGER NOT NULL DEFAULT 0,
        clicks                           INTEGER NOT NULL DEFAULT 0,
        cost_micros                      INTEGER NOT NULL DEFAULT 0,
        conversions                      REAL    NOT NULL DEFAULT 0,
        conversion_value                 REAL    NOT NULL DEFAULT 0,
        view_through_conversions         INTEGER          DEFAULT 0,
        all_conversions                  REAL             DEFAULT 0,
        all_conversion_value             REAL             DEFAULT 0,

        -- Audience-network-specific impression share.
        -- Stored as 0.0-1.0; NULL when Microsoft does not report
        -- (e.g. below reporting threshold, or CSV cell is blank).
        audience_impression_share        REAL,
        audience_rank_lost_is            REAL,
        audience_budget_lost_is          REAL,

        -- Snapshot columns: current campaigns values at load time.
        -- These are approximated from current campaigns values and
        -- will be updated when change history ingestion is added.
        budget_micros_that_day           INTEGER,
        bidding_strategy_type_that_day   TEXT,
        bidding_strategy_id_that_day     TEXT,
        target_cpa_micros_that_day       INTEGER,
        target_roas_that_day             REAL,

        last_synced_at                   TEXT    NOT NULL,

        PRIMARY KEY (campaign_id, date, device, network)
    )
"""

INDEX_STATEMENTS = [
    "CREATE INDEX IF NOT EXISTS idx_cpnd_date           ON campaign_performance_network_daily(date)",
    "CREATE INDEX IF NOT EXISTS idx_cpnd_campaign_date  ON campaign_performance_network_daily(campaign_id, date)",
    "CREATE INDEX IF NOT EXISTS idx_cpnd_account_date   ON campaign_performance_network_daily(account_id, date)",
    "CREATE INDEX IF NOT EXISTS idx_cpnd_network        ON campaign_performance_network_daily(network)",
]


# ── Database connection ────────────────────────────────────────────────────────

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


# ── Verification ──────────────────────────────────────────────────────────────

def verify(conn):
    cursor = conn.cursor()

    exists = cursor.execute("""
        SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='campaign_performance_network_daily'
    """).fetchone()

    if not exists:
        log.error("  Table campaign_performance_network_daily NOT found after create.")
        return

    cols = cursor.execute(
        "PRAGMA table_info(campaign_performance_network_daily)"
    ).fetchall()
    log.info("")
    log.info("── Columns in campaign_performance_network_daily ─────────────")
    for c in cols:
        pk_marker = "  [PK]" if c["pk"] else ""
        nn_marker = "  NOT NULL" if c["notnull"] else ""
        log.info(f"  {c['name']:<36} {c['type']:<10}{nn_marker}{pk_marker}")

    idx = cursor.execute("""
        SELECT name FROM sqlite_master
        WHERE type='index' AND tbl_name='campaign_performance_network_daily'
          AND name NOT LIKE 'sqlite_%'
        ORDER BY name
    """).fetchall()
    log.info("")
    log.info(f"── Indexes ({len(idx)}) ─────────────────────────────────────────")
    for i in idx:
        log.info(f"  {i['name']}")

    n_rows = cursor.execute(
        "SELECT COUNT(*) FROM campaign_performance_network_daily"
    ).fetchone()[0]
    log.info("")
    log.info(f"  Current row count: {n_rows:,}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info(f"Database : {DB_PATH}")
    log.info(f"Started  : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    log.info("")

    conn = get_db_connection()

    # Sanity check: campaigns table must exist. If 01_create_campaign_tables.py
    # has not been run, this extension has nothing to reference.
    if not conn.execute("""
        SELECT 1 FROM sqlite_master WHERE type='table' AND name='campaigns'
    """).fetchone():
        log.error("  campaigns table not found — run 01_create_campaign_tables.py first.")
        conn.close()
        return

    if not conn.execute("""
        SELECT 1 FROM sqlite_master WHERE type='table' AND name='accounts'
    """).fetchone():
        log.error("  accounts table not found — run kw_db_scripts/01_create_schema.py first.")
        conn.close()
        return

    log.info("── Creating campaign_performance_network_daily ──────────────")
    conn.execute(CREATE_NETWORK_DAILY)
    conn.commit()
    log.info("  Table created (or already existed).")

    log.info("")
    log.info("── Creating indexes ─────────────────────────────────────────")
    for stmt in INDEX_STATEMENTS:
        name = stmt.split()[5]
        conn.execute(stmt)
        log.info(f"  OK  {name}")
    conn.commit()

    verify(conn)

    conn.close()
    log.info("")
    log.info("Done.")
    log.info("Next step:")
    log.info("  python campaign_db_scripts/08_load_microsoft_campaigns.py \\")
    log.info("      --campaigns-csv <path-to-Reports-campaign-export> \\")
    log.info("      --campaigns-grid-csv <path-to-Campaigns-grid-export> \\")
    log.info("      --adgroups-csv <path-to-Reports-adgroup-export>")


if __name__ == "__main__":
    main()
