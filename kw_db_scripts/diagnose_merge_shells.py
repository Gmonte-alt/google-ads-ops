"""
diagnose_merge_shells.py  — read-only. Full row detail for the suspected Phase 2
merge-artifact shells, so we delete with confidence and catch any mis-link
(the UHC->Surebridge sibling match looked wrong).

READ-ONLY.  python diagnose_merge_shells.py
"""

import re
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")

SHELLS = ["Kin", "UHC", "CSAA", "BCBS"]
# the canonical each SHOULD map to (from the Phase 2 merge record)
EXPECTED_CANON = {
    "Kin": "Kin Insurance", "UHC": "UnitedHealthcare",
    "CSAA": "CSAA Insurance", "BCBS": "Blue Cross Blue Shield",
}


def normalize(t):
    return re.sub(r"\s+", " ", t.lower().strip())


def main():
    if not DB.exists():
        sys.exit(f"DB not found: {DB}")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    print("=" * 88)
    print("  MERGE-SHELL DIAGNOSTIC — confirm clean-delete vs corrupted link")
    print("=" * 88)
    for name in SHELLS:
        b = conn.execute("SELECT * FROM brand_registry WHERE normalized_name=?",
                         (normalize(name),)).fetchone()
        print(f"\n  ── {name} ──")
        if not b:
            print("    not present.")
            continue
        vc = conn.execute("SELECT COUNT(*) c FROM brand_registry_keywords WHERE brand_id=?",
                          (b["id"],)).fetchone()["c"]
        print(f"    row: id={b['id']}  name='{b['brand_name']}'  status={b['bid_status']}  "
              f"category={b['brand_category']}  parent_brand={b['parent_brand']!r}  variants={vc}")
        # does the EXPECTED canonical exist, and with variants?
        canon = conn.execute("SELECT * FROM brand_registry WHERE normalized_name=?",
                             (normalize(EXPECTED_CANON[name]),)).fetchone()
        if canon:
            cvc = conn.execute("SELECT COUNT(*) c FROM brand_registry_keywords WHERE brand_id=?",
                              (canon["id"],)).fetchone()["c"]
            print(f"    expected canonical '{EXPECTED_CANON[name]}': EXISTS "
                  f"(id={canon['id']}, status={canon['bid_status']}, variants={cvc})")
            if vc == 0 and cvc > 0:
                print(f"    VERDICT: clean artifact → safe to DELETE shell (canonical holds {cvc} variants)")
            else:
                print(f"    VERDICT: review — shell has {vc} variants or canonical empty")
        else:
            print(f"    expected canonical '{EXPECTED_CANON[name]}': NOT FOUND under that name")
            print(f"    VERDICT: review — cannot confirm canonical; do not delete yet")
        # flag a suspicious parent_brand (the UHC->Surebridge smell)
        if b["parent_brand"] and normalize(b["parent_brand"]) != normalize(EXPECTED_CANON[name]):
            print(f"    ⚠ parent_brand is '{b['parent_brand']}', expected '{EXPECTED_CANON[name]}' "
                  f"— possible mis-link, investigate before delete")
    print("\n  Clean artifacts → Phase 2.1 shell deletion. Mis-links → fix parent first.")
    conn.close()


if __name__ == "__main__":
    main()
