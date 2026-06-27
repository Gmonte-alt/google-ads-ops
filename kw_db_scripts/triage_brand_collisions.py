"""
triage_brand_collisions.py  — read-only: classify the cross-brand collisions
that block a global UNIQUE(normalized, match_type), and propose a resolution
for each so the Phase 2 migration can be written against decisions, not guesses.
==============================================================================
Two families of collision, needing opposite fixes:

  BRAND MERGE       — two brand_registry rows are the SAME company (CSAA /
                      CSAA Insurance, Kin / Kin Insurance, BCBS / Blue Cross
                      Blue Shield...). Fix = pick a canonical row, reassign the
                      duplicate's keywords to it, delete the duplicate brand.
                      The collision then vanishes. CANONICAL CHOICE IS YOURS —
                      this report shows both rows' attributes + variant counts.

  KEYWORD FIX       — a term is mapped to a brand it doesn't belong to:
       mis-attribution      a substring trap ('ace' inside 'insurace' →
                            Progressive misspellings landed under ACE). Fix =
                            drop the WRONG brand's keyword row.
       unattributed-vs-real a real brand and the 'Unattributed Negative
                            Keyword' catch-all both claim a term. Fix = keep
                            whichever the term actually belongs to (word-boundary
                            test), drop the other.

CLASSIFICATION (heuristic — you confirm):
  * same significant-token set, or one is an acronym of the other -> BRAND MERGE
  * one side is 'Unattributed Negative Keyword' -> keep the side whose brand
    words appear as WHOLE WORDS in the term; drop the other
  * a real brand whose words are only a SUBSTRING of the term -> mis-attribution
  * both real brands' words appear -> OTHER (garbled multi-brand negative)

READ-ONLY.

    python triage_brand_collisions.py
"""

import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
UNATTR = "unattributed negative keyword"

STOP = {"insurance", "insurances", "the", "of", "and", "co", "company", "inc",
        "llc", "group", "corporation", "corp", "mutual", "auto", "life",
        "home", "health", "medicare", "&design", "design"}


def sig_tokens(name: str) -> set:
    return {t for t in re.sub(r"\s+", " ", name.lower().strip()).split()
            if t not in STOP and len(t) > 1}


def is_acronym_of(short_name: str, long_name: str) -> bool:
    """Catch BCBS↔Blue Cross Blue Shield, UHC↔UnitedHealthcare.
    Ordered initials of the long name's words == short, OR short is an in-order
    subsequence of the long name's letters (guarded to short, acronym-like names)."""
    s = re.sub(r"[^a-z]", "", short_name.lower())
    if not (2 <= len(s) <= 6):
        return False
    words = [w for w in re.sub(r"[^a-z ]", " ", long_name.lower()).split() if w]
    if len(words) < 2 and len(re.sub(r"[^a-z]", "", long_name.lower())) <= len(s):
        return False  # long isn't actually longer; not an acronym relationship
    initials = "".join(w[0] for w in words)
    if s == initials:
        return True
    # in-order subsequence of the long name's letters (e.g. uhc ⊂ unitedhealthcare)
    long_clean = re.sub(r"[^a-z]", "", long_name.lower())
    it = iter(long_clean)
    return all(ch in it for ch in s)


def words_present(brand_tokens: set, term: str) -> bool:
    """True if every significant brand token appears as a WHOLE WORD in term."""
    if not brand_tokens:
        return False
    return all(re.search(rf"\b{re.escape(t)}\b", term) for t in brand_tokens)


def main():
    if not DB.exists():
        sys.exit(f"DB not found: {DB}")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    # variant totals + attributes per brand
    attr = {b["id"]: b for b in conn.execute("""
        SELECT id, brand_name, bid_status, parent_brand, verticals, brand_category
        FROM brand_registry""")}
    vcount = {r["brand_id"]: r["n"] for r in conn.execute("""
        SELECT brand_id, COUNT(*) n FROM brand_registry_keywords GROUP BY brand_id""")}

    # collisions
    coll = conn.execute("""
        SELECT normalized, COALESCE(match_type,'') mt
        FROM brand_registry_keywords
        GROUP BY normalized, COALESCE(match_type,'')
        HAVING COUNT(DISTINCT brand_id) > 1
        ORDER BY normalized""").fetchall()

    # group colliding terms by the unordered brand-id pair
    by_pair = defaultdict(list)
    for c in coll:
        bids = [r["brand_id"] for r in conn.execute("""
            SELECT DISTINCT brand_id FROM brand_registry_keywords
            WHERE normalized=? AND COALESCE(match_type,'')=?""",
            (c["normalized"], c["mt"]))]
        by_pair[tuple(sorted(bids))].append((c["normalized"], c["mt"]))

    print("=" * 78)
    print(f"  Collision triage — {len(coll)} colliding (term,match_type) across "
          f"{len(by_pair)} brand-pairs")
    print("=" * 78)

    merges, kw_fixes, others = [], [], []

    for pair, terms in sorted(by_pair.items(), key=lambda x: -len(x[1])):
        names = [attr[b]["brand_name"] if b in attr else f"?{b}" for b in pair]
        toksets = [sig_tokens(n) for n in names]

        # classify (handles the common 2-brand case)
        cls, detail = "OTHER", ""
        if len(pair) == 2:
            a, b = pair
            na, nb = names
            ta, tb = toksets
            lo_a, lo_b = na.lower(), nb.lower()
            if lo_a == UNATTR or lo_b == UNATTR:
                real_i = 1 if lo_a == UNATTR else 0
                real_b, real_tok = pair[real_i], toksets[real_i]
                unattr_b = pair[1 - real_i]
                # does the term actually belong to the real brand?
                belongs = all(words_present(real_tok, t) for t, _ in terms)
                if belongs:
                    cls = "KEEP_REAL_drop_unattributed"
                    detail = f"keep '{names[real_i]}', drop Unattributed copy"
                    kw_fixes.append((pair, terms, cls, detail, unattr_b))
                else:
                    cls = "MISATTRIBUTION_drop_real"
                    detail = (f"term(s) don't contain '{names[real_i]}' as words "
                              f"(substring trap) → drop the '{names[real_i]}' mapping, keep Unattributed")
                    kw_fixes.append((pair, terms, cls, detail, real_b))
            elif ta == tb or is_acronym_of(na, nb) or is_acronym_of(nb, na):
                cls = "BRAND_MERGE"
                detail = "same company, two rows"
                merges.append((pair, terms, cls, detail))
            else:
                cls = "OTHER_review"
                detail = "neither Unattributed nor clear duplicate — review"
                others.append((pair, terms, cls, detail))
        else:
            others.append((pair, terms, "OTHER_multi", "3+ brands"))

        print(f"\n  [{cls}]  terms={len(terms)}")
        for b in pair:
            a_ = attr.get(b, {})
            print(f"    brand_id={b:<5} {a_['brand_name'] if a_ else '?':<34} "
                  f"{a_['bid_status'] if a_ else '?':<11} "
                  f"variants={vcount.get(b,0):<5} parent={a_['parent_brand'] if a_ else '?'}")
        show = ", ".join(f"'{t}'[{mt or 'null'}]" for t, mt in terms[:6])
        print(f"    terms: {show}{' ...' if len(terms) > 6 else ''}")
        if detail:
            print(f"    → {detail}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 78)
    print("  SUMMARY")
    print("=" * 78)
    print(f"\n  BRAND MERGES (your canonical choice — recommend keeping the higher")
    print(f"  variant-count / fuller-attribute row):  {len(merges)} pairs")
    for pair, terms, _, _ in merges:
        a, b = pair
        # recommend canonical = more variants
        keep, drop = (a, b) if vcount.get(a, 0) >= vcount.get(b, 0) else (b, a)
        print(f"     merge: keep '{attr[keep]['brand_name']}' (v={vcount.get(keep,0)}, "
              f"{attr[keep]['bid_status']})  ←  drop '{attr[drop]['brand_name']}' "
              f"(v={vcount.get(drop,0)}, {attr[drop]['bid_status']})")

    print(f"\n  KEYWORD FIXES (drop the wrong mapping):  {len(kw_fixes)} groups")
    for pair, terms, cls, detail, drop_bid in kw_fixes:
        print(f"     {cls}: {len(terms)} term(s) — {detail}")

    if others:
        print(f"\n  OTHER / REVIEW BY HAND:  {len(others)} groups")
        for pair, terms, cls, detail in others:
            ns = " + ".join(attr[b]["brand_name"] if b in attr else f"?{b}" for b in pair)
            print(f"     {ns}: {len(terms)} term(s) — {detail}")

    print("\n  Confirm the canonical choices + any overrides, and I'll write the")
    print("  Phase 2 migration (merge brands, drop wrong mappings), then we can")
    print("  tighten to UNIQUE(normalized, match_type).")
    conn.close()


if __name__ == "__main__":
    main()
