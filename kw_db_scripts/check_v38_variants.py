"""
check_v38_variants.py — read-only collision pre-flight for the v3.8
match_risk='needs_variants' work queue. Per proposed variant: FREE (safe to add) /
NO-OP (already on intended brand) / COLLIDES (under another brand → would be
rejected by the global UNIQUE(normalized, match_type)).

READ-ONLY.  python check_v38_variants.py
"""

import re
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")

# intended_brand -> [disambiguated, context-bearing variants] (never bare)
PROPOSED = {
    "ACE":        ["ace home insurance", "ace property insurance"],
    "Allied":     ["allied property and casualty", "allied home insurance"],
    "Alpha":      ["alpha property and casualty", "alpha home insurance"],
    "AmFam":      ["amfam insurance", "amfam home insurance", "am fam insurance"],
    "Gabi":       ["gabi insurance", "gabi home insurance"],
    "Lemonade":   ["lemonade insurance", "lemonade home insurance", "lemonade renters insurance"],
    "MPIUA":      ["mpiua insurance", "mpiua home insurance"],
    "PICC":       ["picc insurance", "picc property insurance"],
    "Peak":       ["peak property and casualty", "peak home insurance"],
    "Protective": ["protective insurance", "protective life insurance"],
    "TWIA":       ["twia insurance", "twia windstorm insurance"],
}


def normalize(t):
    return re.sub(r"\s+", " ", t.lower().strip())


def main():
    if not DB.exists():
        sys.exit(f"DB not found: {DB}")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    print("=" * 78)
    print("  v3.8 VARIANT PRE-FLIGHT — needs_variants work queue")
    print("=" * 78)
    free = noop = collide = 0
    for brand, terms in PROPOSED.items():
        b = conn.execute("SELECT id, brand_name, match_risk FROM brand_registry WHERE normalized_name=?",
                         (normalize(brand),)).fetchone()
        tgt = b["id"] if b else None
        tag = "" if b else "  ⚠ brand not found by this name"
        print(f"\n  {brand}{tag}")
        for term in terms:
            nt = normalize(term)
            owners = conn.execute("""
                SELECT DISTINCT br.id, br.brand_name FROM brand_registry_keywords k
                JOIN brand_registry br ON br.id=k.brand_id WHERE k.normalized=?""", (nt,)).fetchall()
            if not owners:
                print(f"    FREE      '{term}'")
                free += 1
            elif tgt and all(o["id"] == tgt for o in owners):
                print(f"    NO-OP     '{term}'  (already on {brand})")
                noop += 1
            else:
                who = ", ".join(o["brand_name"] for o in owners)
                print(f"    COLLIDES  '{term}'  → under: {who}")
                collide += 1

    print("\n" + "=" * 78)
    print(f"  {free} FREE (add), {noop} NO-OP (already there), {collide} COLLIDES (need a decision)")
    print("=" * 78)
    conn.close()


if __name__ == "__main__":
    main()
