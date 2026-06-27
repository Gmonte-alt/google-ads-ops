"""
migrate_add_match_risk.py — add a queryable match_risk dimension to brand_registry.
==============================================================================
match_risk records WHY a brand is or isn't a bare-token false-positive risk, and
how it's handled — orthogonal to brand_category (entity/commercial axis). It makes
"which brands need guarding" persistent, queryable data instead of tribal
knowledge, and lets the tagger SKIP guard machinery for safe brands.

Vocabulary (NOT NULL DEFAULT 'safe'):
  safe             distinctive/multi-word name, no collision risk (most brands)
  variant_covered  risky name shape BUT disambiguated context variants make it safe
                   (Geico, AAA, Hippo, True, NJM, QBE, Aetna, Cigna, Zebra...)
  needs_variants   risky token, thin/no coverage — fix by adding disambiguated
                   variants (Alpha, Peak, Protective, MPIUA, PICC, TWIA, Gabi,
                   ACE, Lemonade, Allied, AmFam)  ← the work queue
  guarded_confie   a Confie subsidiary handled by confie_brands.yaml guard layer
                   (detected via brand_category='confie_subsidiary', NOT the noisy
                   YAML name-match that false-marked AmFam/Geico/USAA)
  needs_guard      risky even WITH context — genuinely needs guard rules. Reserved;
                   currently unused (triage found none yet).

Population is HYBRID: the audit's classify logic proposes, then an explicit
OVERRIDE map corrects the known heuristic edge cases (documented per brand).
Re-runnable: re-run after registry changes to reclassify (override wins).

SAFETY: dry-run default (--apply). Backs up brand_registry. Idempotent (adds the
column only if absent; otherwise just re-populates).

    python migrate_add_match_risk.py            # dry-run: show distribution
    python migrate_add_match_risk.py --apply
"""

import argparse
import re
import sqlite3
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
STAMP = f"{date.today():%Y%m%d}"

CTX = {"insurance", "insurer", "home", "homeowners", "auto", "car", "life",
       "casualty", "property", "policy", "coverage", "quote", "flood", "renters",
       "medicare", "health", "dental", "medical", "advantage", "supplement",
       "compare", "comparison", "quotes", "rates", "agency",
       "company", "cost", "cheap", "affordable", "near"}
COMMON_WORDS = {
    "true", "now", "most", "capital", "peak", "alpha", "allied", "protective",
    "national", "universal", "mercury", "liberty", "freedom", "american",
    "first", "general", "direct", "select", "selective", "foremost", "pioneer",
    "standard", "premier", "summit", "horizon", "unity", "century", "guard",
    "core", "elephant", "root", "branch", "lemonade", "kin", "hippo", "metromile",
}
SUBSTRING_TRAPS = {"ace", "kin", "aig", "nfp", "twia"}

# Explicit overrides where the heuristic is known wrong (documented).
# After v3.8, the deferred carriers (Alpha/Peak/Protective/MPIUA/PICC/TWIA/Gabi/
# ACE/Lemonade/AmFam) have disambiguated context variants → the variant-credit
# logic now classifies them variant_covered on its own, so they are NO LONGER
# overridden here. Only genuine exceptions remain:
OVERRIDE = {
    # false flags — real variants the CTX heuristic doesn't catch (health/aggregator)
    "aetna": "variant_covered", "cigna": "variant_covered", "zebra": "variant_covered",
    "ethos": "variant_covered", "chubb": "variant_covered", "aarp": "safe",
    # Allied — deliberately held variant-less pending the Nationwide-Allied demand
    # decision; flag it as the one real needs_variants so it stays visible.
    "allied": "needs_variants",
}


def normalize(t):
    return re.sub(r"\s+", " ", t.lower().strip())


def classify(norm_name, ctx_v):
    toks = norm_name.split()
    short = len(norm_name.replace(" ", "")) < 4
    covered = ctx_v >= 2
    if short:
        return "variant_covered" if covered else "needs_variants"
    if len(toks) == 1:
        if norm_name in COMMON_WORDS or norm_name in SUBSTRING_TRAPS:
            return "variant_covered" if covered else "needs_variants"
        if len(norm_name) <= 5 and not covered:
            return "needs_variants"
        return "safe"
    return "safe"


def main():
    a = argparse.ArgumentParser()
    a.add_argument("--apply", action="store_true")
    args = a.parse_args()
    if not DB.exists():
        sys.exit(f"DB not found: {DB}")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")

    print("=" * 80)
    print(f"  Add match_risk dimension{'   (DRY RUN)' if not args.apply else '   [APPLY]'}")
    print("=" * 80)

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(brand_registry)")}
    has_col = "match_risk" in cols
    print(f"  match_risk column: {'exists (will re-populate)' if has_col else 'will be added'}")

    # compute classification per brand
    plan = []
    for b in conn.execute("SELECT id, brand_name, normalized_name, brand_category FROM brand_registry"):
        ctx_v = 0
        for r in conn.execute("SELECT normalized FROM brand_registry_keywords WHERE brand_id=?", (b["id"],)):
            toks = set(r["normalized"].split())
            if toks & CTX and len(toks) >= 2:
                ctx_v += 1
        if (b["brand_category"] or "") == "confie_subsidiary":
            val = "guarded_confie"
        else:
            val = classify(b["normalized_name"], ctx_v)
        ov = OVERRIDE.get(b["normalized_name"])
        if ov:
            val = ov
        plan.append((b["id"], b["brand_name"], val, ov is not None))

    dist = Counter(v for _, _, v, _ in plan)
    print("\n  Proposed distribution:")
    for k in ("safe", "variant_covered", "needs_variants", "guarded_confie", "needs_guard"):
        print(f"    {k:<16} {dist.get(k, 0)}")
    print(f"    (overrides applied: {sum(1 for *_, o in plan if o)})")

    print("\n  needs_variants (the work queue):")
    for _, name, val, _ in sorted(plan, key=lambda x: x[1]):
        if val == "needs_variants":
            print(f"    {name}")

    if not args.apply:
        print("\n  DRY RUN — re-run with --apply to write.")
        conn.close()
        return

    try:
        conn.execute("BEGIN IMMEDIATE"); conn.execute("ROLLBACK")
    except sqlite3.OperationalError as e:
        sys.exit(f"  [LOCKED] {e}")
    try:
        conn.execute("BEGIN")
        conn.execute(f"DROP TABLE IF EXISTS brand_registry_prematchrisk_{STAMP}")
        conn.execute(f"CREATE TABLE brand_registry_prematchrisk_{STAMP} AS SELECT * FROM brand_registry")
        if not has_col:
            conn.execute("ALTER TABLE brand_registry ADD COLUMN match_risk TEXT NOT NULL DEFAULT 'safe'")
        for bid, _, val, _ in plan:
            conn.execute("UPDATE brand_registry SET match_risk=? WHERE id=?", (val, bid))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    print(f"\n  COMPLETE: match_risk populated for {len(plan)} brands.")
    print(f"  backup: brand_registry_prematchrisk_{STAMP}")
    print("  Query the work queue:  SELECT brand_name FROM brand_registry WHERE match_risk='needs_variants';")
    conn.close()


if __name__ == "__main__":
    main()
