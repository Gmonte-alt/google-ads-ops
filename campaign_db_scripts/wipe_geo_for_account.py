"""
wipe_geo_for_account.py — One-off cleanup before re-running the geo loader.

The first run of 10_load_microsoft_geo.py stored integer columns as BLOBs
(raw little-endian bytes) due to a pandas Int64 vs numpy int64 dtype issue.
After fixing the loader, run this once to wipe the corrupted rows so the
re-run starts from a clean slate. INSERT OR REPLACE would technically overwrite
the same natural-key rows, but anything where the corrupted load created a
unique row that the clean load doesn't reproduce would persist. Cleanest to
wipe and reload.

Usage:
    python wipe_geo_for_account.py --account home
    python wipe_geo_for_account.py --account auto
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from refresh_helper import ACCOUNT_REGISTRY, get_connection, resolve_account_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True, choices=list(ACCOUNT_REGISTRY.keys()))
    args = parser.parse_args()

    with get_connection() as conn:
        account_id = resolve_account_id(conn, args.account)

        # Count first so the user sees what's being deleted.
        before = conn.execute(
            "SELECT COUNT(*) FROM geo_performance_daily WHERE account_id = ?",
            (account_id,),
        ).fetchone()[0]
        print(f"Found {before:,} rows for account={args.account} (id={account_id}).")

        if before == 0:
            print("Nothing to delete.")
            return 0

        confirm = input("Delete all of these rows? [y/N]: ").strip().lower()
        if confirm != "y":
            print("Aborted.")
            return 1

        n = conn.execute(
            "DELETE FROM geo_performance_daily WHERE account_id = ?",
            (account_id,),
        ).rowcount
        conn.commit()
        print(f"Deleted {n:,} rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
