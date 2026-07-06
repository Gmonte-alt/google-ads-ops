"""
backup_databases.py
-------------------
Makes CLEAN, point-in-time snapshots of your live SQLite databases using
the sqlite3 .backup() API. This is safe even if a script is mid-write,
because it locks and streams a consistent copy page-by-page.

Snapshots are written to a SEPARATE folder (db_clean_snapshots) so they
never collide with the file-copy backup that run_backup.bat performs.

HOW TO RUN (in your VS Code integrated terminal, conda env active):
    python backup_databases.py

Notes:
- sqlite3 is part of Python's standard library, so ANY Python works
  (base conda env is fine -- nothing to pip install).
- If a path below is wrong, just edit the strings between the dashes.
"""

import sqlite3
import os
import sys

# ----------------------------- EDIT IF NEEDED -----------------------------
LIVE_DBS = [
    r"C:\Projects\google-ads-ops\db\paid_search.db",
    r"C:\Projects\google-ads-ops\db\demand_research.db",
]
SNAPSHOT_DIR = r"D:\google-ads-ops-backup\db_clean_snapshots"
# --------------------------------------------------------------------------


def safe_backup(live_path, dest_dir):
    if not os.path.exists(live_path):
        print(f"  SKIP (not found): {live_path}")
        return False

    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, os.path.basename(live_path))

    print(f"  {live_path}")
    print(f"     -> {dest_path}")

    src = sqlite3.connect(live_path)
    dst = sqlite3.connect(dest_path)
    try:
        with dst:
            src.backup(dst)   # consistent, locked, page-by-page snapshot
    finally:
        dst.close()
        src.close()

    src_size = os.path.getsize(live_path)
    dst_size = os.path.getsize(dest_path)
    print(f"     source:   {src_size:,} bytes")
    print(f"     snapshot: {dst_size:,} bytes")
    return True


def main():
    print("=== SQLite clean snapshot (sqlite3 .backup API) ===")
    count = 0
    for db in LIVE_DBS:
        if safe_backup(db, SNAPSHOT_DIR):
            count += 1
    print(f"=== Done: {count} snapshot(s) written to {SNAPSHOT_DIR} ===")
    if count == 0:
        print("WARNING: no databases were backed up. Check the paths above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
