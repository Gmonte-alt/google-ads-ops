"""
audit_guard_candidates.py  — read-only. Size the guard-reconciliation scope by
evidence: which brands' BARE token is a false-positive risk, what match_risk
class each falls into, and which are already covered by the guard YAML vs.
leaking unguarded.
==============================================================================
Why: brand_registry has `brand_category` (entity/commercial axis) but NO field
for MATCH-SAFETY risk — whether a brand's bare token is safe to tag. That
knowledge has lived only in conversation ("carrier-leak bares", "P&C generics").
This audit discovers the risk class per brand so it can be persisted to a new
`match_risk` column (proposed), instead of hand-assigned from memory.

Proposed match_risk values:
  safe            distinctive multi-word or uncommon name — no guard needed (default)
  short_token     normalized name < 4 chars — embed-gap AND bare-tag hazard (QBE/NJM class)
  common_word     bare token IS a common English word (true/now/most/peak/national...)
  substring_trap  bare token is a substring of common words (ace->place, kin->napkin...)
  shared_token    bare token collides with ANOTHER brand's name (allied<->nationwide)

For each risky brand, cross-reference the guard YAML (--config): is it already
present there (guarded) or absent (leaking unguarded)?

READ-ONLY. Writes nothing.
  python audit_guard_candidates.py --config confie_brands.yaml
"""

import argparse
import re
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")

# Common English words that are also brand tokens — the common_word hazard set.
# (Curated; the audit also flags any single-token name in COMMON_WORDS.)
COMMON_WORDS = {
    "true", "now", "most", "capital", "peak", "alpha", "allied", "protective",
    "national", "universal", "mercury", "liberty", "freedom", "american",
    "first", "general", "direct", "select", "selective", "foremost", "pioneer",
    "standard", "premier", "summit", "horizon", "unity", "century", "guard",
    "core", "elephant", "root", "branch", "lemonade", "kin", "hippo", "metromile",
}
# Bare tokens that are dangerous substrings of common words (the ACE trap class).
SUBSTRING_TRAPS = {
    "ace": ["place", "space", "race", "palace", "grace", "trace", "menace"],
    "kin": ["napkin", "pumpkin", "smoking", "parking", "looking"],
    "aig": ["craigslist"],
    "nfp": [],
    "twia": [],
}
CTX = {"insurance", "insurer", "home", "homeowners", "auto", "car", "life",
       "casualty", "property", "policy", "coverage", "quote", "flood", "renters",
       # health / medicare carriers carry these instead of home/auto
       "medicare", "health", "dental", "medical", "advantage", "supplement",
       # aggregators / comparison
       "compare", "comparison", "quotes", "rates", "agency",
       # bundle / generic insurance signals
       "company", "cost", "cheap", "affordable", "near"}


def normalize(t):
    return re.sub(r"\s+", " ", t.lower().strip())


def classify_risk(norm_name, ctx_variant_count, total_variant_count):
    """Propose a match_risk for a brand.

    Crediting variant coverage: a brand with disambiguated, context-bearing
    variants (e.g. 'geico auto insurance') tags safely via those variants, never
    via a bare embed — so it is NOT a bare-token risk regardless of name shape.
    Only brands whose bare token is the practical match path stay risky.
    """
    toks = norm_name.split()
    short = len(norm_name.replace(" ", "")) < 4

    # Variant coverage credit: 2+ context-bearing variants → effectively guarded
    # by variants. Short tokens still flagged (they can't embed; need the variants
    # confirmed) but downgraded to 'short_token_covered' when they have them.
    well_covered = ctx_variant_count >= 2

    if short:
        return "short_token_covered" if well_covered else "short_token"
    if len(toks) == 1:
        if norm_name in COMMON_WORDS:
            return "common_word_covered" if well_covered else "common_word"
        if norm_name in SUBSTRING_TRAPS:
            return "substring_trap_covered" if well_covered else "substring_trap"
        if len(norm_name) <= 5 and not well_covered:
            return "common_word"
        return "safe"
    # multi-word names are distinctive → safe
    return "safe"


def count_context_variants(conn, brand_id):
    """Variants whose normalized text carries an insurance/home context word
    (i.e. disambiguated forms that tag safely, not a bare token)."""
    n = 0
    for r in conn.execute("SELECT normalized FROM brand_registry_keywords WHERE brand_id=?", (brand_id,)):
        toks = set(r["normalized"].split())
        if toks & CTX and len(toks) >= 2:
            n += 1
    return n


def load_guard_names(config_path):
    """Return the set of normalized canonical+alias names present in the guard YAML."""
    try:
        import yaml
    except ImportError:
        return None
    if not config_path or not config_path.exists():
        return None
    with config_path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or []
    names = set()
    for e in raw:
        for k in ("canonical", "aliases", "legal_entities"):
            v = e.get(k)
            if isinstance(v, str):
                names.add(normalize(v))
            elif isinstance(v, list):
                names.update(normalize(x) for x in v)
    return names


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=None, help="Path to confie_brands.yaml")
    a = ap.parse_args()
    if not DB.exists():
        sys.exit(f"DB not found: {DB}")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    guard_names = load_guard_names(a.config)
    guard_note = ("guard YAML: %d names loaded" % len(guard_names)) if guard_names is not None \
        else "guard YAML: NOT loaded (pass --config to cross-reference; showing risk only)"

    brands = conn.execute(
        "SELECT id, brand_name, normalized_name, bid_status, brand_category FROM brand_registry "
        "ORDER BY brand_name").fetchall()

    risky = []
    covered = []
    for b in brands:
        vcount = conn.execute(
            "SELECT COUNT(*) c FROM brand_registry_keywords WHERE brand_id=?", (b["id"],)).fetchone()["c"]
        ctx_v = count_context_variants(conn, b["id"])
        risk = classify_risk(b["normalized_name"], ctx_v, vcount)
        if risk == "safe":
            continue
        guarded = (guard_names is not None and b["normalized_name"] in guard_names)
        rec = (b, risk, vcount, ctx_v, guarded)
        if risk.endswith("_covered") or guarded:
            covered.append(rec)
        else:
            risky.append(rec)

    print("=" * 92)
    print(f"  GUARD-CANDIDATE AUDIT   ({len(risky)} truly-risky + {len(covered)} covered "
          f"of {len(brands)} brands)   {guard_note}")
    print("=" * 92)
    by_risk = {}
    for b, risk, vc, ctxv, guarded in risky:
        by_risk.setdefault(risk, []).append((b, vc, ctxv, guarded))

    for risk in ("short_token", "common_word", "substring_trap"):
        rows = by_risk.get(risk, [])
        if not rows:
            continue
        print(f"\n  ── {risk}  ({len(rows)})  — TRULY RISKY (no variant coverage, not guarded) ──")
        for b, vc, ctxv, guarded in sorted(rows, key=lambda x: x[0]["brand_name"]):
            print(f"    {b['brand_name']:<32} {b['bid_status']:<11} "
                  f"cat={b['brand_category'] or '-':<28} var={vc:<3} ctx_var={ctxv}")

    if covered:
        print(f"\n  ── COVERED ({len(covered)}) — risky name-shape but SAFE (variant-covered or in guard YAML) ──")
        for b, risk, vc, ctxv, guarded in sorted(covered, key=lambda x: x[0]["brand_name"]):
            why = "guard-YAML" if guarded else f"{ctxv} ctx variants"
            print(f"    {b['brand_name']:<32} {risk:<24} var={vc:<3} ({why})")

    print("\n" + "=" * 92)
    print(f"  SCOPE: {len(risky)} brands genuinely need attention (variants or guard rules).")
    print(f"         {len(covered)} risky-looking but already safe — no action.")
    print("=" * 92)

    print("\n  This proposes match_risk per brand. Triage which UNGUARDED ones are")
    print("  worth guarding (some may have negligible demand), then we (1) persist")
    print("  match_risk to brand_registry and (2) write guard rules for the kept set.")
    conn.close()


if __name__ == "__main__":
    main()
