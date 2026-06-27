"""
check_usia_registry_batch.py  — read-only: gather the facts for the USIA
registry follow-up batch BEFORE writing any 07 update.
==============================================================================
Three reports, all read-only:

  (A) CARRIER AUDIT — for each carrier in the batch: identity present? current
      bid_status vs the target? variant count + does it have a CATCHING variant
      (one that can actually tag a query)? A carrier with an identity row but no
      catching variant is invisible to the demand pull — the Kin symptom.

  (B) P&C ENTITY SPLIT — United P&C / Cypress / Peak / Alpha / Allied / Protective
      / Homeowners Choice / ACE P&C: new identity vs already-present, with a flag
      for generic/short names whose BARE form must defer to the guard pass.

  (C) VARIANT COLLISION PRE-FLIGHT — the post-Phase-2 hazard. There is now a
      global UNIQUE(normalized, match_type). Any proposed AAA / Kin / P&C variant
      that ALREADY belongs to another brand will be REJECTED on insert (not
      duplicated). This reports, per proposed variant: free / already-on-this-
      brand (no-op) / COLLIDES-with-other-brand (blocked — needs a decision).

READ-ONLY.  python check_usia_registry_batch.py
"""

import re
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")

CONTEXT = ("insurance", "insur", "home", "homeowners", "homeowner", "auto",
           "life", "medicare", "quote", "coverage", "policy", "flood",
           "property", "casualty", "affinity", "hazard")


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().strip())


# (A) carriers to confirm: (name, target_bid_status)
CARRIERS = [
    ("Erie", "approved"), ("Kin", "approved"), ("Foremost", "approved"),
    ("Hanover", "approved"),
    ("Twin City Fire", "restricted"), ("Charter Oak Fire", "restricted"),
    ("Citizens", "restricted"), ("Honeycomb", "restricted"),
    ("Blue Fire", "restricted"), ("MPIUA", "restricted"),
    ("Wright Flood", "restricted"), ("TWIA", "restricted"),
    ("US Fire", "restricted"), ("United Fire and Casualty", "restricted"),
    ("National Fire and Marine", "restricted"),
    ("National Liability and Fire", "restricted"),
    ("Selective", "restricted"), ("Argenia", "restricted"),
    # name aliases to also try if the primary lookup misses
]
NAME_ALIASES = {
    "Kin": ["Kin Insurance"], "Erie": ["Erie Insurance"],
    "Foremost": ["Foremost Insurance"], "Hanover": ["Hanover Insurance"],
    "Citizens": ["Citizens Property Insurance Corporation"],
    "US Fire": ["US Fire Insurance"], "Selective": ["Selective Insurance"],
}

# (B) P&C entities riding inside generic "property and casualty insurance"
PC_ENTITIES = [
    "United P&C", "Cypress", "Peak", "Alpha", "Allied", "Protective",
    "Homeowners Choice", "ACE P&C",
]

# (C) proposed variants to pre-flight: (intended_brand_name, [variant terms])
PROPOSED_VARIANTS = {
    "AAA": ["aaa insurance", "aaa home insurance", "aaa homeowners insurance",
            "aaa auto insurance", "triple a insurance", "triple a home insurance",
            "triple a auto insurance",
            "aaa home and auto insurance", "aaa auto and home insurance",
            "triple a home and auto insurance", "triple a auto and home insurance"],
    "Kin Insurance": ["kin insurance", "kin home insurance", "kin.com",
                      "kin com", "getkin", "get kin insurance"],
}


def main():
    if not DB.exists():
        sys.exit(f"DB not found: {DB}")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    def find_brand(name):
        r = conn.execute("SELECT * FROM brand_registry WHERE normalized_name=?",
                         (normalize(name),)).fetchone()
        return r

    def variants(bid):
        return conn.execute(
            "SELECT normalized, match_type FROM brand_registry_keywords WHERE brand_id=?",
            (bid,)).fetchall()

    def has_catching(vs):
        return any(any(t in v["normalized"] for t in CONTEXT) or " " in v["normalized"]
                   for v in vs)

    print("=" * 78)
    print("  (A) CARRIER AUDIT — present? bid_status vs target? catching variant?")
    print("=" * 78)
    for name, target in CARRIERS:
        b = find_brand(name)
        if not b:
            for alias in NAME_ALIASES.get(name, []):
                b = find_brand(alias)
                if b:
                    name = f"{name} → {alias}"
                    break
        if not b:
            print(f"  MISSING   {name:<32} (no identity row)")
            continue
        vs = variants(b["id"])
        catch = has_catching(vs)
        status_flag = "" if b["bid_status"] == target else f"  ⚠ is '{b['bid_status']}', want '{target}'"
        cov = "OK" if catch else ("THIN/NONE — invisible to pull" if vs else "NO VARIANTS — invisible")
        print(f"  {b['brand_name']:<34} {b['bid_status']:<11} variants={len(vs):<3} {cov}{status_flag}")

    print("\n" + "=" * 78)
    print("  (B) P&C ENTITY SPLIT — new identity vs existing; bare-form risk")
    print("=" * 78)
    GENERIC_BARE = {"peak", "alpha", "allied", "protective"}  # bare token unsafe
    for ent in PC_ENTITIES:
        b = find_brand(ent)
        nb = normalize(ent)
        bare_warn = "  (bare token generic — defer bare, disambiguated only)" if nb in GENERIC_BARE else ""
        if b:
            print(f"  EXISTS    {ent:<22} → '{b['brand_name']}' ({b['bid_status']}){bare_warn}")
        else:
            print(f"  NEW       {ent:<22} → add identity (restricted){bare_warn}")

    print("\n" + "=" * 78)
    print("  (C) VARIANT COLLISION PRE-FLIGHT — global UNIQUE(normalized,match_type)")
    print("=" * 78)
    for brand_name, terms in PROPOSED_VARIANTS.items():
        tgt = find_brand(brand_name)
        tgt_id = tgt["id"] if tgt else None
        print(f"\n  intended brand: {brand_name}"
              + ("" if tgt else "  ⚠ BRAND NOT FOUND — resolve name first"))
        for term in terms:
            nt = normalize(term)
            owners = conn.execute("""
                SELECT b.id, b.brand_name FROM brand_registry_keywords k
                JOIN brand_registry b ON b.id=k.brand_id WHERE k.normalized=?""",
                (nt,)).fetchall()
            if not owners:
                print(f"    FREE      '{term}'  → safe to add")
            elif tgt_id and all(o["id"] == tgt_id for o in owners):
                print(f"    NO-OP     '{term}'  → already on {brand_name}")
            else:
                who = ", ".join(o["brand_name"] for o in owners)
                print(f"    COLLIDES  '{term}'  → already under: {who}  (insert would be REJECTED)")

    print("\n  Paste this back — it tells us which carriers need variants, which P&C")
    print("  entities are new, and which proposed variants are blocked by the")
    print("  post-Phase-2 unique constraint. Then I write the v3.5 update from facts.")
    conn.close()


if __name__ == "__main__":
    main()
