"""
db_unlock_check.py  — is paid_search.db held by another writer right now?
=========================================================================
Tries to take a write lock with a short timeout. If it succeeds, nothing is
holding the DB and the hang was elsewhere (or the holder is now gone) — it then
checkpoints the WAL to a clean state. If it FAILS with 'database is locked',
another process (almost certainly a lingering python.exe) is holding it — see
the chat for how to kill it.

Safe: takes the lock for a microsecond and rolls back; only the WAL checkpoint
writes, and only if the DB is free.

    python db_unlock_check.py
"""

import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")


def size(p: Path) -> str:
    return f"{p.stat().st_size/1e6:,.1f} MB" if p.exists() else "—"


def main():
    if not DB.exists():
        sys.exit(f"DB not found: {DB}")

    wal = DB.with_name(DB.name + "-wal")
    shm = DB.with_name(DB.name + "-shm")
    print("=" * 60)
    print("  paid_search.db lock check")
    print("=" * 60)
    print(f"  main : {size(DB)}")
    print(f"  -wal : {size(wal)}   (large = uncheckpointed writes pending)")
    print(f"  -shm : {size(shm)}")

    print("\n  Attempting an immediate write lock (3s timeout)...")
    try:
        conn = sqlite3.connect(DB, timeout=3.0)
        conn.execute("PRAGMA busy_timeout = 3000")
        conn.execute("BEGIN IMMEDIATE")     # acquires the write lock or fails
        conn.execute("ROLLBACK")
        print("  [OK] Write lock acquired — NOTHING is holding the DB.")
        r = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        # returns (busy, log_pages, checkpointed_pages)
        busy = r[0] if r else None
        print(f"  WAL checkpoint(TRUNCATE): busy={r[0]} log={r[1]} checkpointed={r[2]}")
        conn.close()
        if busy == 0:
            print("\n  => DB is free and WAL is clean. Safe to re-run the sync.")
        else:
            print("\n  => Lock is free, but checkpoint was busy — a reader is open.")
            print("     Close other terminals/tools touching the DB, then re-run.")
    except sqlite3.OperationalError as e:
        print(f"  [LOCKED] {e}")
        print("\n  => Another process holds the write lock — almost certainly a")
        print("     lingering python.exe from a Ctrl-C'd run. Kill it (see chat),")
        print("     then run this check again until it reports [OK].")


if __name__ == "__main__":
    main()
