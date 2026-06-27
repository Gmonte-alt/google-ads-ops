#!/usr/bin/env python3
"""
tag_products_demand_research.py

Product-intent tagging pass for the USIA keyword planner. Mirrors the retag_demand_research
pattern, but for the NEW product taxonomy (paid_search.db) rather than the brand registry.

It reads `product_taxonomy` + `product_taxonomy_terms` (node_type='product' only -- the
campaign-structure grain), matches each home demand keyword in demand_research.db, and writes
three columns onto kwp_ideas:
    product_code       e.g. 'homeowners','condo','landlord','renters','commercial_property'
    product_vertical   'home' | 'commercial'
    product_ambiguous  1 if matched only via an is_ambiguous root ("building insurance" etc.)

This is the data-driven replacement for the hand-rolled ADJACENT_PATTERNS guesses: usia_keyword
_select reads these tags and gates on the USIA in-scope product set. is_ambiguous matches are
written but flagged, NOT hard-assigned -- the selection module decides how to treat them.

Run (dry-run first, then --apply):
    python tag_products_demand_research.py --vertical home
    python tag_products_demand_research.py --vertical home --apply
"""
import argparse
import re
import sqlite3
from collections import Counter, defaultdict

DEF_DEMAND = r"C:\Projects\google-ads-ops\db\demand_research.db"
DEF_REGISTRY = r"C:\Projects\google-ads-ops\db\paid_search.db"   # taxonomy lives here

PRODUCT_COLS = [("product_code", "TEXT"), ("product_vertical", "TEXT"),
                ("product_ambiguous", "INTEGER")]


def normalize(s):
    """Single normalization used on BOTH sides (keyword and taxonomy term) so we never depend
    on the architect's normalization matching ours -- we re-normalize their normalized field."""
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def load_product_index(registry_db):
    """Load node_type='product' terms -> matchers. Returns (exact_map, phrase_list).
    exact_map: {normalized -> (code, vertical, ambiguous)}
    phrase_list: [(tokens, code, vertical, ambiguous)] sorted longest-first (most specific)."""
    conn = sqlite3.connect(registry_db)
    rows = conn.execute("""
        SELECT t.normalized, t.match_type, t.is_ambiguous,
               p.product_code, p.vertical
        FROM product_taxonomy_terms t
        JOIN product_taxonomy p ON p.id = t.product_id
        WHERE p.node_type = 'product'
    """).fetchall()
    conn.close()
    exact_map, phrase_list = {}, []
    for norm, mtype, ambig, code, vertical in rows:
        n = normalize(norm)
        if not n:
            continue
        rec = (code, (vertical or "home"), int(ambig or 0))
        if (mtype or "").lower() == "exact":
            # keep the less-ambiguous / first seen for a given exact string
            if n not in exact_map or rec[2] < exact_map[n][2]:
                exact_map[n] = rec
        else:                                  # 'phrase' / '' / anything else -> contiguous match
            phrase_list.append((tuple(n.split()), code, vertical or "home", int(ambig or 0)))
    phrase_list.sort(key=lambda x: len(x[0]), reverse=True)
    return exact_map, phrase_list


def _contiguous(needle, haystack):
    n = len(needle)
    if n == 0 or n > len(haystack):
        return False
    for i in range(len(haystack) - n + 1):
        if tuple(haystack[i:i + n]) == needle:
            return True
    return False


def match_product(kw, index):
    """Best node_type='product' match for a keyword. Prefers a non-ambiguous match, then the
    longest (most specific) term. Returns (code, vertical, ambiguous) or None."""
    exact_map, phrase_list = index
    nk = normalize(kw)
    if not nk:
        return None
    toks = nk.split()
    cands = []
    if nk in exact_map:
        cands.append((exact_map[nk], len(toks)))     # exact = full length, most specific
    for needle, code, vertical, ambig in phrase_list:
        if _contiguous(needle, toks):
            cands.append(((code, vertical, ambig), len(needle)))
    if not cands:
        return None
    # non-ambiguous first, then longest term
    cands.sort(key=lambda c: (c[0][2], -c[1]))
    return cands[0][0]


def ensure_columns(conn):
    have = {r[1] for r in conn.execute("PRAGMA table_info(kwp_ideas)").fetchall()}
    for col, typ in PRODUCT_COLS:
        if col not in have:
            conn.execute("ALTER TABLE kwp_ideas ADD COLUMN %s %s" % (col, typ))


def run(demand_db, registry_db, vertical, apply):
    index = load_product_index(registry_db)
    n_exact, n_phrase = len(index[0]), len(index[1])
    print("Loaded product taxonomy: %d exact + %d phrase terms (node_type='product')" %
          (n_exact, n_phrase))

    conn = sqlite3.connect(demand_db)
    if apply:
        ensure_columns(conn)

    kws = [r[0] for r in conn.execute(
        "SELECT DISTINCT keyword_text FROM kwp_ideas WHERE vertical=?", (vertical,)).fetchall()]
    print("Distinct %s keywords to tag: %d" % (vertical, len(kws)))

    tally = Counter()
    ambig_n = 0
    updates = []
    for kw in kws:
        m = match_product(kw, index)
        if not m:
            tally["(no product)"] += 1
            continue
        code, vert, ambig = m
        tally[code] += 1
        ambig_n += ambig
        updates.append((code, vert, ambig, kw))

    print("\n=== product tag distribution ===")
    for code, n in tally.most_common():
        print("  %-22s %6d" % (code, n))
    print("  (ambiguous-root matches: %d)" % ambig_n)

    if not apply:
        print("\nDRY RUN -- no writes. Re-run with --apply to persist.")
        # show a few sample assignments for eyeballing
        print("\nsample assignments:")
        for code, vert, ambig, kw in updates[:25]:
            print("  %-40s -> %-20s %s%s" % (kw[:40], code, vert, "  [ambiguous]" if ambig else ""))
        conn.close()
        return

    conn.executemany(
        "UPDATE kwp_ideas SET product_code=?, product_vertical=?, product_ambiguous=? "
        "WHERE keyword_text=?", updates)
    conn.commit()
    conn.close()
    print("\nAPPLIED: tagged %d keywords with a product_code." % len(updates))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEF_DEMAND, help="demand_research.db")
    ap.add_argument("--registry-db", default=DEF_REGISTRY, help="paid_search.db (taxonomy)")
    ap.add_argument("--vertical", default="home")
    ap.add_argument("--apply", action="store_true", help="persist (default: dry-run)")
    a = ap.parse_args()
    run(a.db, a.registry_db, a.vertical, a.apply)


if __name__ == "__main__":
    main()
