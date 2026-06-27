#!/usr/bin/env python
"""
retag_demand_research.py -- re-apply the current paid_search.db brand registry to the
brand columns already stored in demand_research.db, WITHOUT re-pulling from KWP.

Why this exists: usia_kwp_pull.py tags brands at capture time against the registry as
it existed then. Updating paid_search.db (e.g. the carrier-leak batch) does NOT
propagate to demand_research.db, so the keyword-selection module -- which reads the
stored `brand` column -- keeps seeing the old (often empty) tags. This pass closes the
gap: it reuses the SAME BrandTagger + normalizer as the pull (via brand_tagger.py), so
the result equals what a fresh pull would tag now, and rewrites brand / brand_bid_status
/ brand_category / dnb_flag / brand_source in place for every matching keyword.

Dry-run by default (reports the diff, writes nothing). Add --apply to commit.

    python retag_demand_research.py                       # dry-run, all verticals
    python retag_demand_research.py --apply               # commit
    python retag_demand_research.py --vertical home --apply
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from brand_tagger import BrandTagger, normalize_keyword

DEFAULT_DEMAND_DB = r"C:\Projects\google-ads-ops\db\demand_research.db"
DEFAULT_BRAND_DB = r"C:\Projects\google-ads-ops\db\paid_search.db"

# Terms the architect's v3.1 batch said should now resolve -- focused readout.
WATCH = [
    "all state home insurance", "citizen property insurance corporation",
    "aaa home owners insurance", "light house insurance",
    "universal homeowners insurance", "qbe home insurance quote",
]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--demand-db", default=DEFAULT_DEMAND_DB)
    p.add_argument("--brand-db", default=DEFAULT_BRAND_DB)
    p.add_argument("--vertical", default=None, help="limit to one vertical (default: all)")
    p.add_argument("--apply", action="store_true", help="commit changes (default: dry-run)")
    a = p.parse_args()

    if not os.path.exists(a.brand_db):
        print("ERROR: brand registry not found at %s" % a.brand_db); sys.exit(1)
    tagger = BrandTagger(a.brand_db)
    if "registry" not in " ".join(tagger.sources):
        print("ERROR: brand_registry did not load from %s (sources: %s)"
              % (a.brand_db, tagger.sources)); sys.exit(1)
    print("registry: %s" % " | ".join(tagger.sources))

    conn = sqlite3.connect(a.demand_db)
    where = "WHERE vertical = ?" if a.vertical else ""
    params = (a.vertical,) if a.vertical else ()

    # current stored tag per distinct keyword (consistent across geos/pulls)
    cur = {}
    for kw, br, bid, dnb in conn.execute(
            "SELECT keyword_text, brand, brand_bid_status, dnb_flag FROM kwp_ideas "
            "%s GROUP BY keyword_text" % where, params):
        cur[kw] = (br or "", bid or "", int(dnb or 0))

    newly_tagged, brand_changed, status_changed, updates = [], [], [], []
    for kw, (obr, obid, odnb) in cur.items():
        brand, bid, cat, dnb, src = tagger.tag(normalize_keyword(kw))
        brand, bid, dnb = brand or "", bid or "", int(bool(dnb))
        if (brand, bid, dnb) == (obr, obid, odnb):
            continue
        updates.append((brand, bid, cat or "", dnb, src or "", kw))
        if not obr and brand:
            newly_tagged.append((kw, brand, bid))
        elif obr and brand and obr != brand:
            brand_changed.append((kw, obr, brand))
        else:
            status_changed.append((kw, brand or obr, obid, bid))

    print("\ndistinct keywords scanned : %d" % len(cur))
    print("keywords that would change: %d" % len(updates))
    print("  newly tagged (''->brand): %d" % len(newly_tagged))
    print("  brand reassigned        : %d" % len(brand_changed))
    print("  status/dnb changed      : %d" % len(status_changed))
    if newly_tagged:
        print("\nnewly tagged (sample):")
        for kw, br, bid in sorted(newly_tagged)[:20]:
            print("   %-44s -> %s (%s)" % (kw, br, bid or "-"))
    if status_changed:
        print("\nstatus/dnb changed:")
        for kw, br, ob, nb in status_changed[:20]:
            print("   %-44s %s: %s -> %s" % (kw, br, ob or "-", nb or "-"))
    if brand_changed:
        print("\nbrand reassigned:")
        for kw, ob, nb in brand_changed[:20]:
            print("   %-44s %s -> %s" % (kw, ob, nb))

    print("\nwatch terms (v3.1 expected resolutions):")
    for w in WATCH:
        if w in cur:
            brand, bid, cat, dnb, src = tagger.tag(normalize_keyword(w))
            print("   %-44s -> %s (status=%s dnb=%d)"
                  % (w, brand or "STILL UNTAGGED", bid or "-", int(bool(dnb))))
        else:
            print("   %-44s (not present in demand data)" % w)

    if a.apply and updates:
        rows = 0
        for brand, bid, cat, dnb, src, kw in updates:
            c = conn.execute(
                "UPDATE kwp_ideas SET brand=?, brand_bid_status=?, brand_category=?, "
                "dnb_flag=?, brand_source=? WHERE keyword_text=?",
                (brand, bid, cat, dnb, src, kw))
            rows += c.rowcount
        conn.commit()
        print("\nAPPLIED: %d keywords re-tagged (%d rows updated across geos/pulls)."
              % (len(updates), rows))
    elif updates:
        print("\nDRY-RUN: nothing written. Re-run with --apply to commit.")
    else:
        print("\nNo changes needed -- stored tags already match the current registry.")
    conn.close()


if __name__ == "__main__":
    main()
