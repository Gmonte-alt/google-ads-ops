"""
create_keyword_network_table.py
===============================
Creates keyword_performance_network_daily — the network-split companion to
keyword_performance_daily. Path B: preserves the search-vs-audience split at
keyword grain, which loader 04 currently aggregates away.

DESIGN (mirrors the proven campaign pattern)
  keyword_performance_daily          -- network-COLLAPSED, unchanged. Every
                                        existing query keeps working byte-for-byte.
  keyword_performance_network_daily  -- NEW. Network-split. Audience/carrier
                                        analysis queries this.

This mirrors campaign_performance_daily (collapsed) vs
campaign_performance_network_daily (split). Adding a `network` column to the
existing keyword_performance_daily would retroactively break every query that
filters device='ALL' and expects the network-collapsed total (they'd sum
per-network rows and double-count) — so a separate table is the safe design.

GRAIN (locked): device='ALL' x network only.
  One row per (instance_id, report_date, network), with device fixed to 'ALL'.
  No per-device x network rows (not needed for the audience analysis; keeps the
  table small). device is kept in the schema/PK, always 'ALL', so query and
  reconciliation patterns stay parallel to keyword_performance_daily.

RECONCILIATION INVARIANT
  For any (instance_id, report_date):
    SUM over networks in keyword_performance_network_daily (device='ALL')
      == the single keyword_performance_daily row (device='ALL')
  i.e. this table decomposes the kpd ALL-device total by network. refresh_check
  should assert this.

COLUMNS mirror keyword_performance_daily exactly, plus `network`:
  instance_id, report_date, device, network, geo_state, geo_target_constant_id,
  clicks, impressions, cost_micros,
  primary_conv_count, primary_conv_value, secondary_conv_count,
  all_conv_count, all_conv_value

No account_id column — like keyword_performance_daily, account scoping is via
the join to keyword_instances (instance_id -> keyword_instances.account_id).

Usage:
    python create_keyword_network_table.py            # dry-run, shows DDL
    python create_keyword_network_table.py --apply
"""

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")

DDL = """
CREATE TABLE IF NOT EXISTS keyword_performance_network_daily (
    instance_id             INTEGER NOT NULL,
    report_date             TEXT    NOT NULL,
    device                  TEXT    NOT NULL DEFAULT 'ALL',   -- always 'ALL' at this grain
    network                 TEXT    NOT NULL,                 -- the live dimension
    geo_state               TEXT    NOT NULL DEFAULT 'ALL',
    geo_target_constant_id  TEXT,
    clicks                  INTEGER NOT NULL DEFAULT 0,
    impressions             INTEGER NOT NULL DEFAULT 0,
    cost_micros             REAL    NOT NULL DEFAULT 0,
    primary_conv_count      REAL    NOT NULL DEFAULT 0,       -- pixel (tCPA signal)
    primary_conv_value      REAL    NOT NULL DEFAULT 0,       -- offline revenue
    secondary_conv_count    REAL    NOT NULL DEFAULT 0,       -- offline-only count
    all_conv_count          REAL    NOT NULL DEFAULT 0,       -- raw MS total
    all_conv_value          REAL    NOT NULL DEFAULT 0,       -- offline revenue
    PRIMARY KEY (instance_id, report_date, device, network, geo_state),
    FOREIGN KEY (instance_id) REFERENCES keyword_instances(id)
);
CREATE INDEX IF NOT EXISTS ix_kpnd_date_network
    ON keyword_performance_network_daily (report_date, network);
CREATE INDEX IF NOT EXISTS ix_kpnd_instance
    ON keyword_performance_network_daily (instance_id, report_date);
"""


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.row_factory = sqlite3.Row
    return conn


def main():
    ap = argparse.ArgumentParser(description="Create keyword_performance_network_daily.")
    ap.add_argument("--apply", action="store_true", help="Actually create (default dry-run).")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"ERROR: DB not found at {DB_PATH}")
        return 1

    print("=" * 68)
    print(f"  Create keyword_performance_network_daily  "
          f"({'APPLY' if args.apply else 'DRY RUN'})")
    print("=" * 68)

    conn = get_connection()
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='keyword_performance_network_daily'").fetchone()
    print(f"\n  Table currently: {'EXISTS' if exists else 'missing'}")

    if not args.apply:
        print("\n--- DDL that WOULD run ---")
        print(DDL)
        print("Re-run with --apply to create.")
        conn.close()
        return 0

    if exists:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        n = conn.execute(
            "SELECT COUNT(*) FROM keyword_performance_network_daily").fetchone()[0]
        if n:
            backup = f"keyword_performance_network_daily_backup_{ts}"
            print(f"\n  Backing up {n:,} existing rows -> {backup}")
            conn.execute(f"CREATE TABLE {backup} AS "
                         f"SELECT * FROM keyword_performance_network_daily")

    conn.executescript(DDL)
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM keyword_performance_network_daily").fetchone()[0]
    print(f"\n  Created. Row count: {n:,}")
    print("  Next: run the modified loader 04 to populate (full history reload).")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
