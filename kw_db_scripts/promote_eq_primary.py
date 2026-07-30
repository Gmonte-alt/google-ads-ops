#!/usr/bin/env python
"""
promote_eq_primary.py -- one-row update: EQ Home Conversions with Value -> primary.

Era-correctness fix before the 2024 backfill: EQ was the primary revenue action
during the EverQuote partnership. Dual-primary with QW is safe (each conversion
belongs to exactly one action -- no double-count). Covers BOTH syncs: 03 and 05
route via conversion_actions.include_mode.

Dry-run by default. Run with --apply to execute.

    python promote_eq_primary.py --db "C:\\Projects\\google-ads-ops\\db\\paid_search.db" --apply
"""
import argparse
import sqlite3

NEW_NOTE = ("PROMOTED monitor->primary 2026-07-20 for 2024 backfill era-correctness: "
            "EQ was the primary revenue action during the EverQuote partnership "
            "(ended ~2024). Dual-primary with QW is safe -- each conversion belongs "
            "to one action, no double-count. Current-era EQ volume is trivial "
            "(~$4.9K). Prior note: nearly dormant, fired 1 day in audit range; "
            "do-not-promote guidance superseded for backfill.")

ap = argparse.ArgumentParser()
ap.add_argument("--db", required=True)
ap.add_argument("--apply", action="store_true")
args = ap.parse_args()

conn = sqlite3.connect(args.db)
row = conn.execute(
    "SELECT id, conversion_name, include_mode FROM conversion_actions "
    "WHERE account_id = 1 AND id = 3").fetchone()
print("before:", row)
if row is None:
    raise SystemExit("[ABORT] row id=3 not found")

if not args.apply:
    print("[DRY RUN] would set include_mode='primary'. Re-run with --apply.")
else:
    conn.execute(
        "UPDATE conversion_actions SET include_mode = 'primary', notes = ? "
        "WHERE account_id = 1 AND id = 3", (NEW_NOTE,))
    conn.commit()
    print("updated rows:", conn.total_changes)
    print("\naccount-1 map now:")
    for r in conn.execute(
            "SELECT id, conversion_name, include_mode FROM conversion_actions "
            "WHERE account_id = 1 ORDER BY id"):
        print("  ", r)
conn.close()
