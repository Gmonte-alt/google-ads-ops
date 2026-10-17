#!/usr/bin/env python
"""
seed_termlife_conversions.py -- seeds conversion_actions for account 29
(Term Life) per Gilbert's 9/24 ruling: SQ Life Hourly Offline Conversions is
THE primary (leads + values reported on); Zip Submit mirrors Home's
secondary_count pattern; everything else excluded with reasons in notes.

    python seed_termlife_conversions.py            (dry run)
    python seed_termlife_conversions.py --apply
"""
import argparse
import sqlite3

DB = r"C:\Projects\google-ads-ops\db\paid_search.db"
A = 29

ROWS = [
    ("7232001514", "sq life hourly offline conversions", "primary",
     "9/24 ruling: THE lead action + values reported on (bidding optimizes here)"),
    ("6900820002", "term life - zip submit", "secondary_count",
     "zip-submit twin of Home's pattern (top-of-funnel count, $0 value)"),
    ("6887625285", "sq life offline conversions", "excluded",
     "overlaps hourly upload -- double-count guard; ERA CANDIDATE if early routed history reads zero"),
    ("7073214410", "sq life gbraids", "excluded",
     "GBRAIDS twin -- promotion pending disjointness audit (Home 7/30 precedent)"),
    ("7072382570", "offline (upload)", "excluded",
     "REMOVED legacy upload; ERA CANDIDATE for 2024-2025 routed history"),
    ("7072647022", "offline (upload) (2)", "excluded",
     "REMOVED legacy upload; ERA CANDIDATE for 2024-2025 routed history"),
    ("7072646815", "sq gbraids conversion action", "excluded", "REMOVED gbraids era"),
    ("7072666710", "sq life gbraids test", "excluded", "REMOVED test"),
    ("7079165517", "sq gbraids test: do not use", "excluded", "test action, do not use"),
    ("7107610001", "sq high-value leads", "excluded", "value-split re-upload -- double-count"),
    ("7107610235", "sq low-value leads", "excluded", "value-split re-upload -- double-count"),
    ("7073619961", "android installs (all other apps)", "excluded", "not a lead"),
    ("7652410506", "rb-fe-calls", "excluded",
     "calls upload -- not claimed as leads per 9/24 ruling; revisit if calls become monetized"),
]

ap = argparse.ArgumentParser()
ap.add_argument("--apply", action="store_true")
args = ap.parse_args()

conn = sqlite3.connect(DB)
cols = [r[1] for r in conn.execute("PRAGMA table_info(conversion_actions)")]
print("conversion_actions columns:", cols)
need = {"id", "account_id", "conversion_name", "include_mode"}
if not need <= set(cols):
    raise SystemExit("[ABORT] expected columns missing: %s" % sorted(need - set(cols)))
existing = conn.execute("SELECT COUNT(*) FROM conversion_actions WHERE account_id=?", (A,)).fetchone()[0]
print("existing acct-%d rows: %d" % (A, existing))
print("[PLAN] %d rows for acct %d:" % (len(ROWS), A))
for gid, name, mode, note in ROWS:
    print("   %-16s %-38s %s" % (mode, name[:38], gid))
if not args.apply:
    print("[DRY RUN] nothing written.")
else:
    has_notes = "notes" in cols
    for gid, name, mode, note in ROWS:
        if has_notes:
            conn.execute(
                "INSERT OR REPLACE INTO conversion_actions (id, account_id, conversion_name, include_mode, notes) VALUES (?,?,?,?,?)",
                (gid, A, name, mode, note))
        else:
            conn.execute(
                "INSERT OR REPLACE INTO conversion_actions (id, account_id, conversion_name, include_mode) VALUES (?,?,?,?)",
                (gid, A, name, mode))
    conn.commit()
    print("[APPLY] map seeded. verify:")
    for r in conn.execute("SELECT include_mode, COUNT(*) FROM conversion_actions WHERE account_id=? GROUP BY 1", (A,)):
        print("  ", r)
conn.close()
