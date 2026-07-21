#!/usr/bin/env python3
"""Generate the paired Editor fix for carrier-addition keywords posted with the inverted match type.
Reads corrected keyword_current, finds EVERY placement (all mirrored campaigns) in the new dedicated
'-Core' groups whose match type disagrees with the grab-bag, and emits:
  - fix_pause.csv : all wrong copies, keyed on Ad group ID + Keyword ID -> Paused
  - fix_add.csv   : the correct match type re-added (Campaign ID + Ad group ID + Keyword) -> Enabled,
                    EXCEPT geo variants, which are dropped (base term + geo-campaign covers them)
Match type is immutable, so the fix is pause-wrong + add-right.
Scope: ACTIVE campaigns only (campaign_status='ENABLED') -- the account carries ~100+ old/paused campaigns whose '-Core' ad groups would otherwise inflate the placement count.

Run: python additions_matchtype_fix.py --db <path>/paid_search.db --carriers "Kin,Costco,American Family"
"""
import argparse
import csv
import os
import re
import sqlite3
from collections import defaultdict

_STATE = re.compile(r"\b(alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|"
    r"florida|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|"
    r"massachusetts|michigan|minnesota|mississippi|missouri|montana|nebraska|nevada|new hampshire|"
    r"new jersey|new mexico|new york|north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|"
    r"rhode island|south carolina|south dakota|tennessee|texas|utah|vermont|virginia|washington|"
    r"west virginia|wisconsin|wyoming)\b")


def main():
    ap = argparse.ArgumentParser(description="Emit paired pause+add Editor files to fix inverted match types")
    ap.add_argument("--db", required=True)
    ap.add_argument("--account", type=int, default=1)
    ap.add_argument("--carriers", required=True)
    ap.add_argument("--grabbag-token", default="home_group")
    ap.add_argument("--dedicated-token", default="core")
    ap.add_argument("--out-dir", default="matchtype_fix_output")
    ap.add_argument("--keep-geo", action="store_true", help="re-add geo variants too (default: drop them)")
    a = ap.parse_args()
    carriers = [c.strip().lower() for c in a.carriers.split(",") if c.strip()]

    conn = sqlite3.connect("file:%s?mode=ro" % a.db, uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT campaign_id, campaign_name, ad_group_name, ad_group_id, criterion_id, keyword_text, "
        "match_type FROM keyword_current WHERE account_id=? AND hierarchy_enabled = 1", (a.account,)).fetchall()
    conn.close()

    def carrier_of(t):
        t = (t or "").lower()
        for c in carriers:
            if c in t:
                return c
        return None

    grabbag = defaultdict(set)          # (carrier, kw) -> correct match types
    dedicated = defaultdict(list)       # (carrier, kw) -> [dedicated placements]
    ded_have = defaultdict(set)         # (carrier, kw) -> match types already present in dedicated groups
    for r in rows:
        car = carrier_of(r["keyword_text"])
        if not car:
            continue
        agn = (r["ad_group_name"] or "").lower()
        mt = (r["match_type"] or "").upper()
        key = (car, (r["keyword_text"] or "").lower())
        if a.dedicated_token in agn and car in agn:
            dedicated[key].append(r)
            ded_have[key].add(mt)
        elif a.grabbag_token in agn:
            grabbag[key].add(mt)

    pause_rows, add_rows, geo_dropped = [], [], []
    for key, drows in dedicated.items():
        correct = grabbag.get(key)
        if not correct:
            continue
        is_geo = bool(_STATE.search(key[1]))
        for r in drows:
            mt = (r["match_type"] or "").upper()
            if mt in correct:
                continue  # already correct
            pause_rows.append([r["campaign_id"], r["ad_group_id"], r["criterion_id"],
                               r["keyword_text"], mt.title(), "Paused"])
            if is_geo and not a.keep_geo:
                geo_dropped.append([r["keyword_text"], r["ad_group_name"]])
                continue
            for ct in sorted(correct):
                if ct in ded_have[key]:
                    continue  # correct type already exists in that group; don't duplicate
                add_rows.append([r["campaign_id"], r["ad_group_id"], r["keyword_text"], ct.title(), "Enabled"])

    # de-dup add rows
    add_rows = [list(t) for t in dict.fromkeys(tuple(x) for x in add_rows)]

    os.makedirs(a.out_dir, exist_ok=True)

    def dump(name, header, rows_):
        with open(os.path.join(a.out_dir, name), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows_)

    dump("fix_pause.csv", ["Campaign ID", "Ad group ID", "Keyword ID", "Keyword", "Match Type", "Status"],
         pause_rows)
    dump("fix_add.csv", ["Campaign ID", "Ad group ID", "Keyword", "Match Type", "Status"], add_rows)
    dump("fix_geo_dropped.csv", ["Keyword", "Ad group"], geo_dropped)

    print("pause (wrong match type, all placements): %d" % len(pause_rows))
    print("add (correct match type re-added):        %d" % len(add_rows))
    print("geo variants dropped (paused, not re-added): %d" % len(geo_dropped))
    print("wrote -> %s" % os.path.abspath(a.out_dir))


if __name__ == "__main__":
    main()
