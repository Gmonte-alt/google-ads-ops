"""
inspect_schema_geo_part2.py
===========================
The first inspection script crashed on the keyword sample-row query
(keyword_performance_daily has no account_id column - it joins via
keyword_instances). This rerun captures the remaining diagnostics that
didn't complete:
  - keyword_performance_daily sample row (joined correctly)
  - geo_state distinct values (only meaningful on keyword table)
  - device value distribution per table
  - network value distribution

Run:
    python inspect_schema_geo_part2.py
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

    # keyword_performance_daily sample row (joins through keyword_instances)
    banner("Sample row from keyword_performance_daily (Microsoft account)")
    row = cur.execute(
        """
        SELECT kpd.*
        FROM keyword_performance_daily kpd
        JOIN keyword_instances ki ON ki.id = kpd.instance_id
        WHERE ki.account_id IN (2, 54)
        LIMIT 1
        """
    ).fetchone()
    if row:
        col_names = [d[0] for d in cur.description]
        for name, val in zip(col_names, row):
            print(f"  {name:<30} = {val!r}")
    print()

    # geo_state values currently in use (campaign tables - which won't have it)
    banner("geo_state column presence and values")
    for tbl in ["campaign_performance_daily", "campaign_performance_network_daily",
                "keyword_performance_daily"]:
        try:
            if tbl == "keyword_performance_daily":
                rows = cur.execute(
                    f"""
                    SELECT kpd.geo_state, COUNT(*)
                    FROM {tbl} kpd
                    JOIN keyword_instances ki ON ki.id = kpd.instance_id
                    WHERE ki.account_id IN (2, 54)
                    GROUP BY kpd.geo_state
                    ORDER BY kpd.geo_state
                    """
                ).fetchall()
            else:
                rows = cur.execute(
                    f"SELECT geo_state, COUNT(*) FROM {tbl} "
                    f"WHERE account_id IN (2, 54) GROUP BY geo_state ORDER BY geo_state"
                ).fetchall()
            print(f"\n{tbl}:")
            for v, n in rows:
                print(f"  geo_state={v!r:<20} {n:>12,} rows")
        except sqlite3.OperationalError as e:
            print(f"\n{tbl}: column not present ({e})")
    print()

    # device value distribution
    banner("device value distribution (Microsoft accounts only)")
    for tbl in ["campaign_performance_daily", "campaign_performance_network_daily",
                "keyword_performance_daily"]:
        if tbl == "keyword_performance_daily":
            sql = f"""
            SELECT kpd.device, COUNT(*) FROM {tbl} kpd
            JOIN keyword_instances ki ON ki.id = kpd.instance_id
            WHERE ki.account_id IN (2, 54)
            GROUP BY kpd.device
            ORDER BY kpd.device
            """
        else:
            sql = (
                f"SELECT device, COUNT(*) FROM {tbl} "
                f"WHERE account_id IN (2, 54) GROUP BY device ORDER BY device"
            )
        rows = cur.execute(sql).fetchall()
        print(f"\n{tbl}:")
        for v, n in rows:
            print(f"  device={v!r:<12} {n:>12,} rows")
    print()

    # network values (only on the two campaign tables - keyword table has no network col)
    banner("network value distribution (Microsoft accounts only)")
    for tbl in ["campaign_performance_daily", "campaign_performance_network_daily"]:
        try:
            rows = cur.execute(
                f"SELECT network, COUNT(*) FROM {tbl} "
                f"WHERE account_id IN (2, 54) GROUP BY network ORDER BY network"
            ).fetchall()
            print(f"\n{tbl}:")
            for v, n in rows:
                print(f"  {v!r:<45} {n:>12,} rows")
        except sqlite3.OperationalError as e:
            print(f"\n{tbl}: {e}")
    print()

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
