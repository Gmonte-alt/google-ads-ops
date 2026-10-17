"""
migrate_rename_history_table.py -- rename google_change_csv_2025_raw -> google_change_csv_history_raw

The table now holds 2024-2026, so the year-specific name is misleading. This renames the table
and its two indexes. Dry-run by default; --apply to execute. Idempotent: if the new name already
exists (migration already run), it reports and exits cleanly. Read-only until --apply; wraps the
rename in a transaction so a failure rolls back.

    python migrate_rename_history_table.py --db PATH            # dry-run (shows plan)
    python migrate_rename_history_table.py --db PATH --apply
"""
import argparse
import sqlite3

OLD = "google_change_csv_2025_raw"
NEW = "google_change_csv_history_raw"
OLD_IX = ["ix_google_change_csv_2025_raw_dt", "ix_google_change_csv_2025_raw_camp_ag"]
NEW_IX = ["ix_google_change_csv_history_raw_dt", "ix_google_change_csv_history_raw_camp_ag"]
IX_COLS = ["(change_datetime)", "(campaign_col, ad_group_col)"]


def exists(conn, name, typ="table"):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type=? AND name=?",
                        (typ, name)).fetchone() is not None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA busy_timeout=30000")

    have_old = exists(conn, OLD)
    have_new = exists(conn, NEW)

    if have_new and not have_old:
        n = conn.execute("SELECT COUNT(*) FROM %s" % NEW).fetchone()[0]
        print("[ok] already migrated: %s exists (%d rows), %s is gone. Nothing to do." % (NEW, n, OLD))
        conn.close(); return
    if not have_old:
        print("[!] source table %s not found and %s missing — nothing to migrate." % (OLD, NEW))
        conn.close(); return
    if have_new and have_old:
        print("[!] BOTH %s and %s exist. Refusing to auto-merge — resolve manually." % (OLD, NEW))
        conn.close(); return

    rows = conn.execute("SELECT COUNT(*) FROM %s" % OLD).fetchone()[0]
    rng = conn.execute("SELECT MIN(change_datetime), MAX(change_datetime) FROM %s" % OLD).fetchone()
    print("=== rename plan ===")
    print("  table:   %s  ->  %s   (%d rows, %s .. %s)" % (OLD, NEW, rows, rng[0], rng[1]))
    for oi, ni in zip(OLD_IX, NEW_IX):
        print("  index:   %s  ->  %s" % (oi, ni))

    if not args.apply:
        print("\nDry run only. Re-run with --apply to execute the rename.")
        conn.close(); return

    try:
        conn.execute("BEGIN")
        conn.execute("ALTER TABLE %s RENAME TO %s" % (OLD, NEW))
        # SQLite keeps old-named indexes attached to the renamed table; drop & recreate cleanly
        for oi in OLD_IX:
            if exists(conn, oi, "index"):
                conn.execute("DROP INDEX %s" % oi)
        conn.execute("CREATE INDEX IF NOT EXISTS %s ON %s %s" % (NEW_IX[0], NEW, IX_COLS[0]))
        conn.execute("CREATE INDEX IF NOT EXISTS %s ON %s %s" % (NEW_IX[1], NEW, IX_COLS[1]))
        conn.execute("COMMIT")
        n = conn.execute("SELECT COUNT(*) FROM %s" % NEW).fetchone()[0]
        print("\n[apply] renamed -> %s (%d rows), indexes rebuilt. Old name is gone." % (NEW, n))
        print("[note] back up to the T9 (Rule 4) — schema change.")
    except Exception:
        conn.execute("ROLLBACK")
        print("[ERROR] rolled back — table unchanged.")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
