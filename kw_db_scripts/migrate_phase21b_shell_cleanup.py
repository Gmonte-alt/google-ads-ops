"""
migrate_phase21b_shell_cleanup.py — delete the 4 empty acronym shells by NAME
(not by hardcoded id). The id-based v1 broke because re-seeding recreated the
shells under NEW auto-increment ids, so id=266/268/... no longer matched and the
cleanup skipped them while they still existed. Keying on normalized_name is
id-drift-proof. Pairs with the corrected 07 (which no longer re-seeds them), so
this deletion now holds permanently.

Deletes a shell ONLY if: it exists, has 0 variants, AND its canonical sibling
exists WITH variants (so nothing is lost). Also re-applies Kin Insurance approved.

SAFETY: dry-run default (--apply). Backs up brand_registry. Per-shell guard.

    python migrate_phase21b_shell_cleanup.py            # dry-run
    python migrate_phase21b_shell_cleanup.py --apply
"""

import argparse
import re
import sqlite3
import sys
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
STAMP = f"{date.today():%Y%m%d}"

# (shell_normalized_name, canonical_normalized_name)
SHELLS = [
    ("kin", "kin insurance"),
    ("uhc", "unitedhealthcare"),
    ("csaa", "csaa insurance"),
    ("bcbs", "blue cross blue shield"),
]
KIN_INS = ("kin insurance", "approved")  # re-apply intended status


def normalize(t):
    return re.sub(r"\s+", " ", t.lower().strip())


def vcount(conn, bid):
    return conn.execute("SELECT COUNT(*) c FROM brand_registry_keywords WHERE brand_id=?",
                        (bid,)).fetchone()["c"]


def main():
    a = argparse.ArgumentParser()
    a.add_argument("--apply", action="store_true")
    args = a.parse_args()
    if not DB.exists():
        sys.exit(f"DB not found: {DB}")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")

    print("=" * 78)
    print(f"  Phase 2.1b shell cleanup (by-name){'   (DRY RUN)' if not args.apply else '   [APPLY]'}")
    print("=" * 78)

    to_delete = []
    ok = True
    for shell_n, canon_n in SHELLS:
        shell = conn.execute("SELECT id, brand_name FROM brand_registry WHERE normalized_name=?",
                            (shell_n,)).fetchone()
        canon = conn.execute("SELECT id, brand_name, bid_status FROM brand_registry WHERE normalized_name=?",
                            (canon_n,)).fetchone()
        if not shell:
            print(f"  '{shell_n}': not present — nothing to delete")
            continue
        sv = vcount(conn, shell["id"])
        if sv != 0:
            print(f"  '{shell['brand_name']}' (id={shell['id']}): has {sv} variants — NOT empty, refusing")
            ok = False
            continue
        if not canon:
            print(f"  '{shell['brand_name']}': canonical '{canon_n}' MISSING — refusing (would lose brand)")
            ok = False
            continue
        cv = vcount(conn, canon["id"])
        if cv == 0:
            print(f"  '{shell['brand_name']}': canonical '{canon['brand_name']}' also empty — review, refusing")
            ok = False
            continue
        print(f"  '{shell['brand_name']}' (id={shell['id']}, 0 var) → delete; "
              f"canonical '{canon['brand_name']}' holds {cv} variants ✓")
        to_delete.append(shell["id"])

    if not ok:
        sys.exit("\n  Guard failed — no changes.")

    if not args.apply:
        # show Kin status intent
        k = conn.execute("SELECT bid_status FROM brand_registry WHERE normalized_name=?", (KIN_INS[0],)).fetchone()
        if k and k["bid_status"] != KIN_INS[1]:
            print(f"  Kin Insurance: '{k['bid_status']}' → '{KIN_INS[1]}'")
        print(f"\n  DRY RUN — would delete {len(to_delete)} shells. Re-run with --apply.")
        conn.close()
        return

    try:
        conn.execute("BEGIN IMMEDIATE"); conn.execute("ROLLBACK")
    except sqlite3.OperationalError as e:
        sys.exit(f"  [LOCKED] {e}")
    try:
        conn.execute("BEGIN")
        conn.execute(f"DROP TABLE IF EXISTS brand_registry_prephase21b_{STAMP}")
        conn.execute(f"CREATE TABLE brand_registry_prephase21b_{STAMP} AS SELECT * FROM brand_registry")
        deleted = 0
        for bid in to_delete:
            cur = conn.execute(
                "DELETE FROM brand_registry WHERE id=? AND "
                "(SELECT COUNT(*) FROM brand_registry_keywords WHERE brand_id=?)=0", (bid, bid))
            deleted += cur.rowcount
        conn.execute("UPDATE brand_registry SET bid_status=? WHERE normalized_name=?",
                     (KIN_INS[1], KIN_INS[0]))
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
    print(f"\n  COMPLETE: {deleted} shells deleted.  brand_registry: {total} brands.")
    print(f"  backup: brand_registry_prephase21b_{STAMP}")
    conn.close()


if __name__ == "__main__":
    main()
