#!/usr/bin/env python
"""
promote_gbraids_primary.py -- one-row update: QW Home GBRAIDS -> primary (account 1).

Rationale: GBRAIDS carries QW conversions for iOS-privacy traffic (gbraid/wbraid
keyed, no gclid). Promoting includes that cohort in primary_conv_* alongside the
main QW offline action. PRECONDITION (confirmed by Gilbert before --apply): the
GBRAIDS action and "QW Home Offline Conversions" are DISJOINT lead sets -- no
lead is uploaded to both -- so dual-primary cannot double-count.

Dry-run by default.

    python promote_gbraids_primary.py --db "C:\\Projects\\google-ads-ops\\db\\paid_search.db" --apply
"""
import argparse
import sqlite3

NEW_NOTE = ("PROMOTED excluded->primary 2026-07-30: QW conversions for iOS-privacy "
            "traffic (gbraid-keyed uploads). Disjoint from QW Home Offline "
            "Conversions per Gilbert -- same lead never uploaded to both, so "
            "dual-primary does not double-count. ~1.5% of primary volume. "
            "Prior note: bidding signal only; DO NOT REMOVE FROM ACCOUNT "
            "(Smart Bidding dependency) -- that warning still stands.")

ap = argparse.ArgumentParser()
ap.add_argument("--db", required=True)
ap.add_argument("--apply", action="store_true")
args = ap.parse_args()

conn = sqlite3.connect(args.db)
row = conn.execute(
    "SELECT id, conversion_name, include_mode FROM conversion_actions "
    "WHERE account_id = 1 AND id = 4").fetchone()
print("before:", row)
if row is None or "GBRAIDS" not in (row[1] or "").upper():
    raise SystemExit("[ABORT] row id=4 is not the GBRAIDS action -- verify id first")

if not args.apply:
    print("[DRY RUN] would set include_mode='primary'. Re-run with --apply.")
else:
    conn.execute(
        "UPDATE conversion_actions SET include_mode = 'primary', notes = ? "
        "WHERE account_id = 1 AND id = 4", (NEW_NOTE,))
    conn.commit()
    print("updated rows:", conn.total_changes)
    print("\naccount-1 map now:")
    for r in conn.execute(
            "SELECT id, conversion_name, include_mode FROM conversion_actions "
            "WHERE account_id = 1 ORDER BY id"):
        print("  ", r)
conn.close()
