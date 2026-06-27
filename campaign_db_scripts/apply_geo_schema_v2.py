"""
apply_geo_schema_v2.py — Run the geo_performance_daily v2 migration.

Reads geo_performance_daily_v2.sql from the same directory and executes it
against paid_search.db via Python's sqlite3 module. Use this instead of the
sqlite3 CLI when the CLI binary isn't installed on the system.

The migration DROPs and recreates geo_performance_daily — any existing data
in that table is lost. Re-run the loader with --backfill after this completes.

Usage:
    python apply_geo_schema_v2.py
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from refresh_helper import get_connection

SQL_FILE = SCRIPT_DIR / "geo_performance_daily_v2.sql"


def main() -> int:
    if not SQL_FILE.exists():
        print(f"FATAL: SQL file not found: {SQL_FILE}", file=sys.stderr)
        return 1

    sql = SQL_FILE.read_text(encoding="utf-8")
    print(f"Applying migration from: {SQL_FILE}")
    print(f"Statements: {sql.count(';')} (approx)")

    confirm = input(
        "This DROPs and recreates geo_performance_daily. "
        "Any existing data in that table will be lost. Proceed? [y/N]: "
    ).strip().lower()
    if confirm != "y":
        print("Aborted.")
        return 1

    with get_connection() as conn:
        conn.executescript(sql)
        conn.commit()

        # Verify the new schema by introspecting the table.
        cols = conn.execute("PRAGMA table_info(geo_performance_daily)").fetchall()
        col_names = [dict(c)["name"] for c in cols]
        if "location_type" not in col_names:
            print("FATAL: migration ran but location_type column not found.", file=sys.stderr)
            return 1
        print(f"OK: geo_performance_daily now has {len(col_names)} columns.")
        print(f"PK columns: " + ", ".join(
            f"{dict(c)['name']}(pk={dict(c)['pk']})"
            for c in cols if dict(c)["pk"] > 0
        ))

        # Verify indexes were created.
        idx_rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='geo_performance_daily' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        idx_names = [dict(r)["name"] for r in idx_rows]
        print(f"Indexes: {sorted(idx_names)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
