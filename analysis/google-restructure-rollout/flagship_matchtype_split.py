#!/usr/bin/env python3
"""
flagship_matchtype_split.py — per-text Exact-vs-Broad(-vs-Phrase) L90
splits for the flagship book ex-CA/FL, to rule the NB match-type policy:
(a) Broad-consolidated roster vs (b) match-grain mirror.

For each flagship-book keyword TEXT: clicks, spend, leads, CPL, VPC
(conv value / click), ROAS per match type, plus a per-text note on which
match is cheaper per lead and richer per click.

Usage: python flagship_matchtype_split.py --db paid_search.db
       --assignment keyword_book_assignment_FROZEN_2026-08-27.csv
       [--addendum keyword_book_addendum_2026-10-21.csv]
"""
import argparse
import csv
import datetime as dt
import sqlite3
import sys
from collections import defaultdict

RULESET = "8/27+9/2a+9/2b+9/4-aggtail+9/8-prodintent"
SHORT = {23394808297: "TEXAS", 23496334752: "TIER1", 23530860942: "TIER2",
         23586637678: "TIER3", 23428389081: "TIER4",
         23385188622: "CALIFORNIA", 23394808300: "FLORIDA"}
CAFL = {23385188622, 23394808300}


def die(m):
    print(f"*** HARD FAIL: {m}")
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--assignment", required=True)
    ap.add_argument("--addendum", default=None)
    a = ap.parse_args()
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    f = open(a.assignment, encoding="utf-8-sig")
    if RULESET not in f.readline():
        die("frozen metadata mismatch")
    tags, text_tags = {}, {}
    for r in csv.DictReader(f):
        if r["decision"].startswith("EXCLUDE"):
            continue
        k = (r["keyword"].lower().strip(), r["match_type"].upper())
        tags[(r["campaign"],) + k] = r["book"]
        text_tags.setdefault(k, r["book"])
    if a.addendum:
        fa = open(a.addendum, encoding="utf-8-sig")
        if "addendum" not in fa.readline().lower():
            die("addendum metadata missing")
        for r in csv.DictReader(fa):
            if r["decision"].startswith("EXCLUDE"):
                continue
            k = (r["keyword"].lower().strip(), r["match_type"].upper())
            tags.setdefault((r["campaign"],) + k, r["book"])
            text_tags.setdefault(k, r["book"])

    inst = {}
    for r in con.execute("SELECT id, campaign_id, keyword_text, "
                         "match_type FROM keyword_instances WHERE "
                         "account_id=1"):
        try:
            cid = int(r["campaign_id"])
        except (TypeError, ValueError):
            continue
        if cid not in SHORT or cid in CAFL:
            continue
        k = ((r["keyword_text"] or "").lower().strip(),
             (r["match_type"] or "").upper())
        bk = tags.get((SHORT[cid],) + k) or text_tags.get(k)
        if bk == "FLAGSHIP":
            inst[r["id"]] = k
    if not inst:
        die("no flagship instances mapped ex-CA/FL")
    print(f"[SCOPE] {len(inst):,} flagship-book instances, ex-CA/FL")

    kc = {r[1] for r in con.execute(
        "PRAGMA table_info(keyword_performance_daily)")}
    dcol = "report_date" if "report_date" in kc else "date"
    filt = ""
    if "device" in kc:
        filt += "AND device='ALL' "
    if "geo_state" in kc:
        filt += "AND geo_state='ALL' "
    g_max = con.execute(f"SELECT MAX({dcol}) FROM "
                        f"keyword_performance_daily WHERE 1=1 {filt}"
                        ).fetchone()[0]
    l90 = (dt.date.fromisoformat(g_max)
           - dt.timedelta(days=89)).isoformat()
    print(f"[WINDOW] L90 = {l90}..{g_max}\n")

    # text -> mt -> [clicks, cost, leads, value]
    agg = defaultdict(lambda: defaultdict(lambda: [0.0] * 4))
    ids = list(inst)
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        ph = ",".join("?" * len(chunk))
        for r in con.execute(
                f"SELECT instance_id iid, SUM(clicks) ck, "
                f"SUM(cost_micros)/1e6 co, SUM(primary_conv_count) cv, "
                f"SUM(primary_conv_value) rv FROM "
                f"keyword_performance_daily WHERE instance_id IN ({ph}) "
                f"{filt}AND {dcol} >= ? GROUP BY 1", chunk + [l90]):
            kw, mt = inst[r["iid"]]
            c = agg[kw][mt]
            c[0] += r["ck"] or 0
            c[1] += r["co"] or 0
            c[2] += r["cv"] or 0
            c[3] += r["rv"] or 0

    print(f"{'text':36} {'match':7} {'clicks':>7} {'spend':>9} "
          f"{'leads':>6} {'CPL':>7} {'VPC':>6} {'ROAS':>5}")
    order = sorted(agg.items(),
                   key=lambda kv: -sum(v[1] for v in kv[1].values()))
    for kw, mts in order:
        tot = sum(v[1] for v in mts.values())
        if tot < 10:
            continue
        rows = sorted(mts.items(), key=lambda m: -m[1][1])
        for j, (mt, v) in enumerate(rows):
            ck, co, cv, rv = v
            print(f"{kw if j == 0 else '':36} {mt:7} {ck:>7,.0f} "
                  f"${co:>8,.0f} {cv:>6,.1f} "
                  f"${co/cv if cv else 0:>6,.0f} "
                  f"${rv/ck if ck else 0:>5,.2f} "
                  f"{rv/co if co else 0:>5.2f}")
        e = mts.get("EXACT")
        b = mts.get("BROAD")
        if e and b and e[2] and b[2] and e[0] and b[0]:
            note = []
            note.append("Exact cheaper/lead"
                        if e[1]/e[2] < b[1]/b[2] else "Broad cheaper/lead")
            note.append("Exact richer/click"
                        if e[3]/e[0] > b[3]/b[0] else "Broad richer/click")
            print(f"{'':36} -> {', '.join(note)}")
        print()
    print("Policy framing: if the Exact half is consistently cheaper per "
          "lead and richer per click on the head texts, that argues for "
          "(b) match-grain mirror there; texts where Broad matches or "
          "beats Exact can consolidate under (a).")


if __name__ == "__main__":
    main()
