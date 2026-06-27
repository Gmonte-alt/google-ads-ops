"""
check_v37_registry_batch.py  — read-only pre-flight for the v3.7 brand_leak
durable-fix batch. Gather facts BEFORE writing 07/12.
==============================================================================
Three reports:

  (A) PRESENCE + BID_STATUS — for each carrier in the batch: identity present?
      (tries name aliases), current bid_status vs target, current variant count.
      Covers new conquest carriers (NJM, First American, State Auto, True),
      new restricts (NFP commercial, Brighthouse life/OOS), status FLIPS
      (Liberty Mutual→DNB, AIG→restrict), and existing brands getting variants
      (Universal Property, United P&C, ACE P&C).

  (B) VARIANT COLLISION PRE-FLIGHT — global UNIQUE(normalized, match_type).
      Per proposed variant: FREE / NO-OP (already on intended brand) / COLLIDES
      (already under another brand → insert would be rejected). The load-bearing
      check for 'universal home insurance' (may already be under Universal
      Insurance, a different entity) and the short-token carriers (njm/nfp).

  (C) HOMEOWNERS_EXTENSION TERM PRE-FLIGHT — the regex-caught forms the planner
      wants seeded (vacant property, empty home, holiday home, 2nd home, ...),
      checked against product_taxonomy_terms: new / exists-on-this-node /
      COLLIDES-with-another-node.

READ-ONLY.  python check_v37_registry_batch.py
"""

import re
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().strip())


# (A) presence/status set: (display, [names to try], target_bid_status, role)
PRESENCE = [
    ("NJM",               ["NJM", "NJM Insurance", "New Jersey Manufacturers"], "approved",   "new carrier (3-char, needs variants)"),
    ("First American",    ["First American", "First American Insurance",
                           "First American Property and Casualty"],            "approved",   "new carrier"),
    ("State Auto",        ["State Auto", "State Auto Insurance",
                           "State Automobile Mutual"],                          "approved",   "new carrier"),
    ("True",              ["True", "True Property Insurance", "True Insurance"], "approved",   "new carrier (GENERIC token — disambiguated only)"),
    ("NFP",               ["NFP", "NFP Insurance"],                             "restricted", "new restrict (commercial-leaning)"),
    ("Brighthouse",       ["Brighthouse", "Brighthouse Financial",
                           "Brighthouse Insurance"],                            "restricted", "new restrict (LIFE — out of scope)"),
    ("Liberty Mutual",    ["Liberty Mutual"],                                   "restricted", "FLIP to DNB/restricted (currently leaking)"),
    ("AIG",               ["AIG", "American International Group"],              "restricted", "FLIP to restrict (commercial-leaning)"),
    ("Universal Property",["Universal Property", "Universal Property and Casualty",
                           "Universal Insurance Holdings", "Universal Insurance"], "approved", "EXISTING — add variants"),
    ("United P&C",        ["United P&C", "United Property and Casualty"],        "restricted", "EXISTING — add variants"),
    ("ACE P&C",           ["ACE P&C", "ACE Property and Casualty"],             "restricted", "EXISTING — add variants"),
]

# (B) proposed variants to pre-flight: intended_brand -> [terms]
PROPOSED_VARIANTS = {
    "Universal Property": ["universal home insurance", "universal casualty and property"],
    "True":               ["true property insurance", "true home insurance"],
    "NJM":                ["njm insurance", "njm home insurance"],
    "First American":     ["first american insurance", "first american home insurance",
                           "first american property and casualty"],
    "State Auto":         ["state auto insurance", "state auto home insurance"],
    "NFP":                ["nfp insurance", "nfp home insurance"],
    "Brighthouse":        ["brighthouse insurance", "brighthouse financial"],
    "United P&C":         ["united property and casualty insurance", "upcic insurance"],
    "ACE P&C":            ["ace pc insurance", "ace home insurance"],
}

# (C) homeowners_extension regex-caught forms to seed
HOE_TERMS = ["vacant property", "vacant dwelling", "empty home", "empty property",
             "empty house", "holiday home", "second home", "2nd home", "unoccupied home"]


def main():
    if not DB.exists():
        sys.exit(f"DB not found: {DB}")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    def find_brand(names):
        for n in names:
            r = conn.execute("SELECT * FROM brand_registry WHERE normalized_name=?",
                             (normalize(n),)).fetchone()
            if r:
                return r, n
        return None, None

    def vcount(bid):
        return conn.execute("SELECT COUNT(*) c FROM brand_registry_keywords WHERE brand_id=?",
                            (bid,)).fetchone()["c"]

    print("=" * 80)
    print("  (A) PRESENCE + BID_STATUS")
    print("=" * 80)
    for disp, names, target, role in PRESENCE:
        b, matched = find_brand(names)
        if not b:
            print(f"  MISSING   {disp:<20} → not in registry  [{role}]")
        else:
            flag = "" if b["bid_status"] == target else f"  ⚠ is '{b['bid_status']}', want '{target}'"
            via = "" if matched == disp else f" (as '{b['brand_name']}')"
            print(f"  EXISTS    {disp:<20}{via:<34} {b['bid_status']:<11} variants={vcount(b['id']):<3}{flag}  [{role}]")

    print("\n" + "=" * 80)
    print("  (B) VARIANT COLLISION PRE-FLIGHT — global UNIQUE(normalized, match_type)")
    print("=" * 80)
    for brand_name, terms in PROPOSED_VARIANTS.items():
        b, _ = find_brand([brand_name] + [n for d, ns, *_ in PRESENCE if d == brand_name for n in ns])
        tgt_id = b["id"] if b else None
        tag = "" if b else "  (brand not yet present — FREE = safe once identity added)"
        print(f"\n  {brand_name}{tag}")
        for term in terms:
            nt = normalize(term)
            owners = conn.execute("""
                SELECT DISTINCT b.id, b.brand_name FROM brand_registry_keywords k
                JOIN brand_registry b ON b.id=k.brand_id WHERE k.normalized=?""", (nt,)).fetchall()
            if not owners:
                print(f"    FREE      '{term}'")
            elif tgt_id and all(o["id"] == tgt_id for o in owners):
                print(f"    NO-OP     '{term}'  (already on {brand_name})")
            else:
                who = ", ".join(o["brand_name"] for o in owners)
                print(f"    COLLIDES  '{term}'  → under: {who}  (insert REJECTED — needs decision)")

    print("\n" + "=" * 80)
    print("  (C) HOMEOWNERS_EXTENSION TERM PRE-FLIGHT — product_taxonomy_terms")
    print("=" * 80)
    hoe = conn.execute("SELECT id FROM product_taxonomy WHERE product_code='homeowners_extension'").fetchone()
    hoe_id = hoe["id"] if hoe else None
    if not hoe_id:
        print("  ⚠ homeowners_extension node not found — run 12 first.")
    for term in HOE_TERMS:
        nt = normalize(term)
        rows = conn.execute("""
            SELECT p.product_code FROM product_taxonomy_terms t
            JOIN product_taxonomy p ON p.id=t.product_id WHERE t.normalized=?""", (nt,)).fetchall()
        if not rows:
            print(f"    NEW       '{term}'")
        elif all(r["product_code"] == "homeowners_extension" for r in rows):
            print(f"    EXISTS    '{term}'  (already on homeowners_extension)")
        else:
            nodes = ", ".join(r["product_code"] for r in rows)
            print(f"    COLLIDES  '{term}'  → under node(s): {nodes}")

    print("\n  Paste back — then I write v3.7 (07) carrier adds + status flips +")
    print("  existing-brand variants (FREE only), and the 12 homeowners_extension")
    print("  term expansion (NEW only).")
    conn.close()


if __name__ == "__main__":
    main()
