"""
migrate_phase21_shell_cleanup.py — remove the 4 empty merge-artifact brand shells
Phase 2 left behind, and re-apply Kin Insurance's intended 'approved' status.
==============================================================================
Confirmed by diagnose_merge_shells.py: each shell has 0 variants and its canonical
sibling holds the real variants, so deleting the shell loses nothing.

  DELETE (empty, canonical holds variants):
     id=266 Kin   -> Kin Insurance (id=24, 31 var)
     id=268 UHC   -> UnitedHealthcare (id=125, 10 var)
     id=264 CSAA  -> CSAA Insurance (id=174, 4 var)
     id=269 BCBS  -> Blue Cross Blue Shield (id=132, 5 var)
  FIX:
     id=24 Kin Insurance  bid_status -> 'approved' (Phase 2 intended; never applied)

SAFETY: dry-run default (--apply to commit). Backs up brand_registry. Verifies
each target id is present, name-matches, and has 0 variants BEFORE deleting (so a
drifted id can't delete the wrong brand). Global UNIQUE(normalized, match_type)
from Phase 2 is unaffected (we touch brands, not keyword rows).

    python migrate_phase21_shell_cleanup.py            # dry-run
    python migrate_phase21_shell_cleanup.py --apply
"""

import argparse
import sqlite3
import sys
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
STAMP = f"{date.today():%Y%m%d}"

# (id, expected_name) — must be empty (0 variants) to delete
SHELLS = [(266, "Kin"), (268, "UHC"), (264, "CSAA"), (269, "BCBS")]
# (id, expected_name, new_status)
STATUS_FIX = (24, "Kin Insurance", "approved")


def main():
    a = argparse.ArgumentParser()
    a.add_argument("--apply", action="store_true")
    args = a.parse_args()
    if not DB.exists():
        sys.exit(f"DB not found: {DB}")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")

    print("=" * 76)
    print(f"  Phase 2.1 shell cleanup{'   (DRY RUN)' if not args.apply else '   [APPLY]'}")
    print("=" * 76)

    # verify every shell: present, name matches, 0 variants
    ok = True
    for sid, name in SHELLS:
        r = conn.execute("SELECT brand_name FROM brand_registry WHERE id=?", (sid,)).fetchone()
        if not r:
            print(f"  id={sid} '{name}': MISSING (already deleted?) — skipping")
            continue
        if r["brand_name"] != name:
            print(f"  id={sid}: name '{r['brand_name']}' != expected '{name}' — ABORT GUARD")
            ok = False
            continue
        vc = conn.execute("SELECT COUNT(*) c FROM brand_registry_keywords WHERE brand_id=?",
                          (sid,)).fetchone()["c"]
        if vc != 0:
            print(f"  id={sid} '{name}': has {vc} variants — NOT empty, refusing to delete")
            ok = False
        else:
            print(f"  id={sid} '{name}': empty shell, OK to delete")

    # verify status-fix target
    fr = conn.execute("SELECT brand_name, bid_status FROM brand_registry WHERE id=?",
                      (STATUS_FIX[0],)).fetchone()
    if not fr or fr["brand_name"] != STATUS_FIX[1]:
        print(f"  status-fix id={STATUS_FIX[0]}: name mismatch/missing — ABORT GUARD")
        ok = False
    else:
        cur = fr["bid_status"]
        if cur == STATUS_FIX[2]:
            print(f"  id={STATUS_FIX[0]} '{STATUS_FIX[1]}': already '{cur}' — no flip needed")
        else:
            print(f"  id={STATUS_FIX[0]} '{STATUS_FIX[1]}': '{cur}' → '{STATUS_FIX[2]}'")

    if not ok:
        sys.exit("\n  Guard failed — no changes. Re-run diagnose_merge_shells.py.")

    if not args.apply:
        print("\n  DRY RUN — re-run with --apply to commit.")
        conn.close()
        return

    # lock check
    try:
        conn.execute("BEGIN IMMEDIATE"); conn.execute("ROLLBACK")
    except sqlite3.OperationalError as e:
        sys.exit(f"  [LOCKED] {e}")

    try:
        conn.execute("BEGIN")
        conn.execute(f"DROP TABLE IF EXISTS brand_registry_prephase21_{STAMP}")
        conn.execute(f"CREATE TABLE brand_registry_prephase21_{STAMP} AS SELECT * FROM brand_registry")
        deleted = 0
        for sid, name in SHELLS:
            cur = conn.execute(
                "DELETE FROM brand_registry WHERE id=? AND brand_name=? AND "
                "(SELECT COUNT(*) FROM brand_registry_keywords WHERE brand_id=?)=0",
                (sid, name, sid))
            deleted += cur.rowcount
        conn.execute("UPDATE brand_registry SET bid_status=? WHERE id=? AND brand_name=?",
                     (STATUS_FIX[2], STATUS_FIX[0], STATUS_FIX[1]))
        # verify no keyword row references a deleted brand
        orphans = conn.execute("""
            SELECT COUNT(*) c FROM brand_registry_keywords k
            LEFT JOIN brand_registry b ON b.id=k.brand_id WHERE b.id IS NULL""").fetchone()["c"]
        if orphans:
            conn.execute("ROLLBACK")
            sys.exit(f"  ABORT: {orphans} orphaned keyword rows. No changes committed.")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    total = conn.execute("SELECT COUNT(*) c FROM brand_registry").fetchone()["c"]
    kin = conn.execute("SELECT bid_status FROM brand_registry WHERE id=24").fetchone()
    print(f"\n  COMPLETE: {deleted} shells deleted; Kin Insurance now '{kin['bid_status']}'.")
    print(f"  brand_registry: {total} brands.  backup: brand_registry_prephase21_{STAMP}")
    conn.close()


if __name__ == "__main__":
    main()
