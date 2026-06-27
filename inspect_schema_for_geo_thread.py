"""
inspect_schema_for_geo_thread.py
================================
Prints the actual schema and current state of the Microsoft campaign
tables, to give the geo-design thread accurate facts instead of
recollection.

Outputs:
  1. CREATE TABLE statements for the campaign tables
  2. Indexes / UNIQUE constraints (the real natural keys)
  3. Distinct values in geo_state column (confirms whether it's all 'ALL')
  4. Sample row from each table

Run:
    python inspect_schema_for_geo_thread.py
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")


def banner(text):
    print("=" * 78)
    print(text)
    print("=" * 78)


def main():
    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}")
        return 1

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    for tbl in ["campaign_performance_daily", "campaign_performance_network_daily",
                "keyword_performance_daily"]:
        banner(f"TABLE: {tbl}")

        # CREATE TABLE
        row = cur.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (tbl,),
        ).fetchone()
        if row:
            print("CREATE statement:")
            print("  " + (row[0] or "").replace("\n", "\n  "))
        print()

        # Indexes
        print("Indexes:")
        for r in cur.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='index' AND tbl_name=? ORDER BY name",
            (tbl,),
        ).fetchall():
            print(f"  {r[0]}")
            if r[1]:
                print(f"    {r[1]}")
        print()

        # PRAGMA index_list
        print("PRAGMA index_list:")
        for r in cur.execute(f"PRAGMA index_list({tbl})").fetchall():
            print(f"  seq={r[0]}, name={r[1]}, unique={r[2]}, origin={r[3]}, partial={r[4]}")
        print()

        # Sample row
        print("Sample row (LIMIT 1):")
        row = cur.execute(
            f"SELECT * FROM {tbl} WHERE account_id IN (2, 54) LIMIT 1"
        ).fetchone()
        if row:
            col_names = [d[0] for d in cur.description]
            for name, val in zip(col_names, row):
                print(f"  {name:<35} = {val!r}")
        print()
        print()

    # geo_state distinct values
    banner("geo_state values currently in use")
    for tbl in ["campaign_performance_daily", "campaign_performance_network_daily"]:
        try:
            rows = cur.execute(
                f"SELECT geo_state, COUNT(*) FROM {tbl} "
                f"WHERE account_id IN (2, 54) GROUP BY geo_state ORDER BY geo_state"
            ).fetchall()
            print(f"\n{tbl}:")
            for v, n in rows:
                print(f"  {v!r:<20} {n:>10,} rows")
        except sqlite3.OperationalError as e:
            print(f"\n{tbl}: column not present ({e})")

    print()

    # device='ALL' confirmation on network table
    banner("device='ALL' presence by table (Microsoft accounts only)")
    for tbl in ["campaign_performance_daily", "campaign_performance_network_daily",
                "keyword_performance_daily"]:
        # keyword table joins via ki.account_id, others have account_id directly
        if tbl == "keyword_performance_daily":
            sql = f"""
            SELECT kpd.device, COUNT(*) FROM {tbl} kpd
            JOIN keyword_instances ki ON ki.id = kpd.instance_id
            WHERE ki.account_id IN (2, 54)
            GROUP BY kpd.device
            ORDER BY kpd.device
            """
        else:
            sql = f"SELECT device, COUNT(*) FROM {tbl} WHERE account_id IN (2, 54) GROUP BY device ORDER BY device"
        rows = cur.execute(sql).fetchall()
        print(f"\n{tbl}:")
        for v, n in rows:
            print(f"  device={v!r:<12} {n:>12,} rows")

    print()
    banner("network values present (Microsoft accounts only)")
    for tbl in ["campaign_performance_daily", "campaign_performance_network_daily"]:
        try:
            rows = cur.execute(
                f"SELECT network, COUNT(*) FROM {tbl} "
                f"WHERE account_id IN (2, 54) GROUP BY network ORDER BY network"
            ).fetchall()
            print(f"\n{tbl}:")
            for v, n in rows:
                print(f"  {v!r:<45} {n:>10,} rows")
        except sqlite3.OperationalError as e:
            print(f"\n{tbl}: {e}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
