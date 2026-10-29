#!/usr/bin/env python3
"""
phase2_coverage_check.py — pre-staging coverage audit for Phase 2
(Brand+Home consolidation + NB Flagship enable).

Transition-first rule: every keyword with L90 traction must have a home
in the destination structure before its donor pauses. This emits the
gap lists from the DB, before the builder generates anything.

Sections:
  [A] BRAND-HOME book: keys with L90 spend in the retiring tiers
      (T1/T3/T4), split into COVERED (a copy also spends in the BH
      campaign, ex-Tier 2) vs NEEDS ADD (only home is a retiring tier).
  [B] FLAGSHIP book, ex-CA/FL: every key with L90 spend, by where it
      lives — diff this against the NB campaign's staged roster; any
      spending key not on the roster is either an add or a conscious
      deferral.
  [C] PAUSE LIST: flagship-book keys spending in Tier 2 (and the
      nationwide Generic, if any) — these must pause when NB enables,
      or bare-brand traffic splits across live campaigns.

Usage: python phase2_coverage_check.py --db paid_search.db
       --assignment keyword_book_assignment_FROZEN_2026-08-27.csv
       --addendum keyword_book_addendum_2026-10-21.csv
"""
import argparse
import csv
import datetime as dt
import sqlite3
import sys
from collections import defaultdict

RULESET = "8/27+9/2a+9/2b+9/4-aggtail+9/8-prodintent"
SHORT = {23394808297: "NATL", 23496334752: "T1", 23530860942: "T2",
         23586637678: "T3", 23428389081: "T4",
         23385188622: "CA", 23394808300: "FL"}
TAGCAMP = {23394808297: "TEXAS", 23496334752: "TIER1",
           23530860942: "TIER2", 23586637678: "TIER3",
           23428389081: "TIER4", 23385188622: "CALIFORNIA",
           23394808300: "FLORIDA"}
RETIRING = {"T1", "T3", "T4"}
CAFL = {"CA", "FL"}


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
        if cid not in SHORT:
            continue
        k = ((r["keyword_text"] or "").lower().strip(),
             (r["match_type"] or "").upper())
        bk = tags.get((TAGCAMP[cid],) + k) or text_tags.get(k)
        if bk:
            inst[r["id"]] = (SHORT[cid], k[0], k[1], bk)
    if not inst:
        die("no instances mapped")

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
    print(f"[WINDOW] L90 = {l90}..{g_max}")

    # (kw, mt, book) -> {camp: [co, cv]}
    key = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
    ids = list(inst)
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        ph = ",".join("?" * len(chunk))
        for r in con.execute(
                f"SELECT instance_id iid, SUM(cost_micros)/1e6 co, "
                f"SUM(primary_conv_count) cv FROM "
                f"keyword_performance_daily WHERE instance_id IN ({ph}) "
                f"{filt}AND {dcol} >= ? GROUP BY 1", chunk + [l90]):
            camp, kw, mt, bk = inst[r["iid"]]
            c = key[(kw, mt, bk)][camp]
            c[0] += r["co"] or 0
            c[1] += r["cv"] or 0

    def tot(camps_d, only=None):
        co = sum(v[0] for c, v in camps_d.items()
                 if only is None or c in only)
        cv = sum(v[1] for c, v in camps_d.items()
                 if only is None or c in only)
        return co, cv

    print("\n[A] BRAND-HOME BOOK — keys spending in retiring tiers "
          "(T1/T3/T4), L90")
    needs, covered = [], []
    for (kw, mt, bk), camps in key.items():
        if bk != "BRAND-HOME":
            continue
        rco, rcv = tot(camps, RETIRING)
        if rco < 10:
            continue
        homes = {c for c, v in camps.items() if v[0] > 0} - RETIRING
        (covered if ("T2" in homes) else needs).append(
            (kw, mt, rco, rcv, "+".join(sorted(camps))))
    needs.sort(key=lambda t: -t[2])
    print(f"   NEEDS ADD to Brand+Home ({len(needs)} keys — only homes "
          f"are retiring tiers):")
    for kw, mt, co, cv, w in needs:
        print(f"     {kw:38} [{mt:6}] ${co:>7,.0f} {cv:>5.1f} ld  "
              f"lives: {w}")
    if not needs:
        print("     none — every retiring-tier BH key already spends "
              "in Tier 2")
    print(f"   covered (also live in T2): {len(covered)} keys, "
          f"${sum(t[2] for t in covered):,.0f} L90 retiring-tier spend")

    print("\n[B] FLAGSHIP BOOK ex-CA/FL — every key with L90 spend "
          "(diff against the NB staged roster)")
    rows = []
    for (kw, mt, bk), camps in key.items():
        if bk != "FLAGSHIP":
            continue
        co, cv = tot(camps, set(SHORT.values()) - CAFL)
        if co >= 10:
            rows.append((kw, mt, co, cv,
                         "+".join(sorted(c for c, v in camps.items()
                                         if v[0] > 0))))
    rows.sort(key=lambda t: -t[2])
    for kw, mt, co, cv, w in rows:
        print(f"     {kw:38} [{mt:6}] ${co:>7,.0f} {cv:>5.1f} ld  "
              f"lives: {w}")
    print(f"   {len(rows)} keys, ${sum(t[2] for t in rows):,.0f} L90 "
          f"spend — every one must be on the NB roster or consciously "
          f"deferred")

    print("\n[C] PAUSE LIST — flagship-book keys live in Tier 2 / "
          "Generic (must pause when NB enables)")
    pl = []
    for (kw, mt, bk), camps in key.items():
        if bk != "FLAGSHIP":
            continue
        for c in ("T2", "NATL"):
            if camps.get(c, [0, 0])[0] > 0:
                pl.append((kw, mt, c, camps[c][0]))
    pl.sort(key=lambda t: -t[3])
    for kw, mt, c, co in pl:
        print(f"     {kw:38} [{mt:6}] in {c:4} ${co:>7,.0f} L90")
    print(f"   {len(pl)} placements, ${sum(t[3] for t in pl):,.0f} L90 "
          f"spend" if pl else "   none")


if __name__ == "__main__":
    main()
