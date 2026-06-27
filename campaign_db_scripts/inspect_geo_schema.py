"""
inspect_geo_schema.py  — read-only: can we cut geo × device × {RPL, CvR, CPC}?
==============================================================================
Dumps the geo table schema and answers the three feasibility questions:
  Q1  Is there a `device` column with MOBILE/DESKTOP rows? (needed for ×device)
  Q2  Are routed primary_conv_* columns POPULATED on non-ALL device rows, or
      zero like keyword_performance_daily? (decides if device-RPL/CvR is in
      storage or needs a fresh API pull)
  Q3  geo_state coverage + date range + which metric columns exist.

Checks both candidate table names. READ-ONLY.

    python inspect_geo_schema.py
"""

import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
CANDIDATES = ["geo_performance_daily", "campaign_performance_geo_daily"]


def cols(conn, t):
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({t})").fetchall()]


def exists(conn, t):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
    ).fetchone() is not None


def main():
    if not DB.exists():
        sys.exit(f"DB not found: {DB}")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    for t in CANDIDATES:
        print("=" * 72)
        print(f"  {t}")
        print("=" * 72)
        if not exists(conn, t):
            print("  (table does not exist)\n")
            continue

        c = cols(conn, t)
        print(f"  columns: {', '.join(c)}")

        # CREATE TABLE (shows PK / UNIQUE / FKs)
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (t,)
        ).fetchone()[0]
        print("\n  CREATE TABLE:")
        for ln in ddl.splitlines():
            print(f"    {ln.strip()}")

        date_col = "report_date" if "report_date" in c else ("date" if "date" in c else None)
        has_device = "device" in c
        has_geo    = "geo_state" in c
        has_prim   = "primary_conv_count" in c
        has_acct   = "account_id" in c

        n = conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
        print(f"\n  rows: {n:,}   account_id column: {has_acct}   "
              f"date col: {date_col}   geo_state: {has_geo}   device: {has_device}")

        if date_col:
            r = conn.execute(f"SELECT MIN({date_col}) lo, MAX({date_col}) hi FROM {t}").fetchone()
            print(f"  {date_col} range: {r['lo']} → {r['hi']}")

        # Q1: device distribution
        if has_device:
            print("\n  Q1 — device values present:")
            for r in conn.execute(
                f"SELECT device, COUNT(*) n FROM {t} GROUP BY device ORDER BY n DESC"
            ).fetchall():
                print(f"      {str(r['device']):<12} {r['n']:>10,}")
        else:
            print("\n  Q1 — NO device column → geo × device is NOT available from storage.")

        # Q2: are primary conversions populated off the ALL rollup?
        if has_device and has_prim:
            print("\n  Q2 — primary_conv_count by device-class (is it zero off ALL?):")
            rows = conn.execute(f"""
                SELECT CASE WHEN device='ALL' THEN 'ALL' ELSE 'non-ALL' END AS cls,
                       ROUND(SUM(primary_conv_count),1) AS prim,
                       ROUND(SUM(all_conv_count),1)     AS allc
                FROM {t} GROUP BY cls
            """).fetchall()
            for r in rows:
                print(f"      {r['cls']:<8} primary_conv={r['prim']}  all_conv={r['allc']}")
            nonall = next((r for r in rows if r["cls"] == "non-ALL"), None)
            if nonall is not None and (nonall["prim"] or 0) == 0:
                print("      => primary_conv is ZERO on device rows (same gap as kpd):")
                print("         geo×device CvR/RPL on the PRIMARY lead needs a fresh API pull.")
                print("         geo×device on CPC and all_conv works from storage.")
            elif nonall is not None:
                print("      => primary_conv IS populated on device rows — full geo×device×primary")
                print("         RPL/CvR is available directly from storage.")
        elif has_prim:
            print("\n  Q2 — primary_conv_count exists but no device split (geo stored at ALL only).")
        else:
            print("\n  Q2 — NO primary_conv_count column. Conversions here are unrouted —")
            print("       route from keyword_performance_daily, or pull fresh from the API.")

        # Q3: geo_state coverage + a sample row
        if has_geo:
            gs = conn.execute(f"SELECT COUNT(DISTINCT geo_state) AS n FROM {t}").fetchone()["n"]
            print(f"\n  Q3 — distinct geo_state values: {gs}")
            print("       sample geo_state values:",
                  [r["geo_state"] for r in conn.execute(
                      f"SELECT DISTINCT geo_state FROM {t} LIMIT 8").fetchall()])
        print()

    conn.close()
    print("=" * 72)
    print("  Paste this back; I'll turn the geo×device cut into exact SQL or")
    print("  spec the API pull, depending on Q1/Q2.")
    print("=" * 72)


if __name__ == "__main__":
    main()
