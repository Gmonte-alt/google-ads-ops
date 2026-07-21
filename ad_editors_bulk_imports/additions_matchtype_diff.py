#!/usr/bin/env python3
"""Find carrier-addition keywords whose match type is wrong in-account (posted from inverted DB data),
WITHOUT needing the original posted bulk file. Reads the corrected `keyword_current` view directly and
compares each migrated keyword's match type in its NEW dedicated '-Core' ad group against the SAME
keyword in the grab-bag (Home_Group*). The grab-bag is the correct source (it predates the split and
carries the account's true type); where the dedicated group differs, that's the keyword to fix.

Match type is immutable per keyword, so a "fix" = pause the wrong-match-type keyword in the dedicated
group and add a new one at the correct type. This script identifies them (with Ad group ID + Keyword ID
so you can build the pause file); it doesn't mutate anything.

Run: python additions_matchtype_diff.py --db C:\\Projects\\google-ads-ops\\db\\paid_search.db \\
        --carriers "Kin,Costco,American Family"
"""
import argparse
import csv
import sqlite3
from collections import defaultdict


def main():
    ap = argparse.ArgumentParser(description="Flag posted addition keywords with wrong match type")
    ap.add_argument("--db", required=True)
    ap.add_argument("--account", type=int, default=1)
    ap.add_argument("--carriers", default="", help='comma-separated split carriers to scope (optional)')
    ap.add_argument("--grabbag-token", default="home_group", help="substring identifying grab-bag ad groups")
    ap.add_argument("--dedicated-token", default="core", help="substring identifying dedicated carrier groups")
    ap.add_argument("--out", default="matchtype_fixes.csv")
    a = ap.parse_args()
    carriers = [c.strip().lower() for c in a.carriers.split(",") if c.strip()]

    conn = sqlite3.connect("file:%s?mode=ro" % a.db, uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT ad_group_name, ad_group_id, criterion_id, keyword_text, match_type "
        "FROM keyword_current WHERE account_id=? AND hierarchy_enabled = 1", (a.account,)).fetchall()
    conn.close()

    def carrier_of(text):
        t = (text or "").lower()
        if not carriers:
            return "*"
        for c in carriers:
            if c in t:
                return c
        return None

    dedicated = defaultdict(dict)   # (carrier, kw) -> {MATCH: (ad_group_name, ad_group_id, criterion_id)}
    grabbag = defaultdict(set)      # (carrier, kw) -> {MATCH, ...}
    for r in rows:
        kw = (r["keyword_text"] or "").lower()
        car = carrier_of(kw)
        if car is None:
            continue
        agn = (r["ad_group_name"] or "").lower()
        mt = (r["match_type"] or "").upper()
        key = (car, kw)
        if a.dedicated_token in agn and (not carriers or car in agn):
            dedicated[key][mt] = (r["ad_group_name"], r["ad_group_id"], r["criterion_id"])
        elif a.grabbag_token in agn:
            grabbag[key].add(mt)

    fixes = []
    for key, mts in sorted(dedicated.items()):
        correct = grabbag.get(key)
        if not correct:
            continue  # no grab-bag counterpart -> can't establish the correct type; skip (likely a bare term)
        for mt, (agname, agid, crit) in sorted(mts.items()):
            if mt not in correct:
                fixes.append([key[1], agname, agid, crit, mt.title(),
                              ", ".join(sorted(m.title() for m in correct))])

    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Keyword", "Dedicated ad group", "Ad group ID", "Keyword ID",
                    "Wrong match type (live)", "Correct match type (grab-bag)"])
        w.writerows(fixes)
    print("dedicated-group keywords with wrong match type: %d  ->  %s" % (len(fixes), a.out))
    for row in fixes[:25]:
        print("  %-34s in %-38s  %s -> %s" % (row[0][:34], row[1][:38], row[4], row[5]))
    if not fixes:
        print("  (none -- all carry-over match types agree with the grab-bag)")


if __name__ == "__main__":
    main()
