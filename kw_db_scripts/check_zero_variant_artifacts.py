"""
check_zero_variant_artifacts.py  — read-only. For risky brands with 0 variants,
determine: Phase 2 merge ARTIFACT (a canonical sibling already holds the variants,
so this empty shell should be deleted) vs. genuinely distinct brand that needs
attention. Settles whether Kin/UHC/CSAA/BCBS etc. are cleanup or guard targets.

READ-ONLY.  python check_zero_variant_artifacts.py
"""

import re
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")

# zero/low-variant rows flagged by the audit that smell like merge artifacts
SUSPECTS = ["Kin", "UHC", "CSAA", "BCBS", "AmFam", "Allied"]


def normalize(t):
    return re.sub(r"\s+", " ", t.lower().strip())


def main():
    if not DB.exists():
        sys.exit(f"DB not found: {DB}")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    print("=" * 86)
    print("  ZERO/LOW-VARIANT ARTIFACT CHECK — merge debris (delete) vs distinct (keep)?")
    print("=" * 86)
    for name in SUSPECTS:
        b = conn.execute("SELECT * FROM brand_registry WHERE normalized_name=?",
                         (normalize(name),)).fetchone()
        if not b:
            print(f"\n  {name}: not present (already removed)")
            continue
        vc = conn.execute("SELECT COUNT(*) c FROM brand_registry_keywords WHERE brand_id=?",
                          (b["id"],)).fetchone()["c"]
        # find a likely canonical sibling: a brand whose name CONTAINS this name as a
        # word and which HAS variants (e.g. 'Kin' -> 'Kin Insurance')
        sibs = conn.execute(
            """SELECT br.id, br.brand_name, br.bid_status,
                      (SELECT COUNT(*) FROM brand_registry_keywords k WHERE k.brand_id=br.id) vc
               FROM brand_registry br
               WHERE br.normalized_name LIKE ? AND br.id != ?
               ORDER BY vc DESC""",
            (normalize(name) + " %", b["id"])).fetchall()
        # also LIKE '% name %' and '% name'
        more = conn.execute(
            """SELECT br.id, br.brand_name, br.bid_status,
                      (SELECT COUNT(*) FROM brand_registry_keywords k WHERE k.brand_id=br.id) vc
               FROM brand_registry br
               WHERE (br.normalized_name LIKE ? OR br.parent_brand = ?) AND br.id != ?
               ORDER BY vc DESC""",
            ("% " + normalize(name) + "%", b["brand_name"], b["id"])).fetchall()
        seen = {b["id"]}
        cands = []
        for r in list(sibs) + list(more):
            if r["id"] not in seen:
                seen.add(r["id"]); cands.append(r)

        print(f"\n  {b['brand_name']}  (id={b['id']}, {b['bid_status']}, variants={vc})")
        if vc == 0 and cands:
            best = cands[0]
            verdict = ("→ LIKELY ARTIFACT: canonical sibling holds the variants → delete this empty shell"
                       if best["vc"] > 0 else "→ sibling exists but also empty — review")
            print(f"     {verdict}")
            for r in cands[:4]:
                print(f"       sibling: {r['brand_name']:<34} {r['bid_status']:<11} variants={r['vc']}")
        elif vc == 0:
            print("     → 0 variants, NO sibling found → genuinely empty brand: keep+add variants OR remove")
        else:
            print(f"     → has {vc} variants; not a zero-variant artifact (audit low-variant flag stands)")
    print("\n  Artifacts → fold into Phase 2.1 cleanup (delete shells). Distinct → variant/guard track.")
    conn.close()


if __name__ == "__main__":
    main()
