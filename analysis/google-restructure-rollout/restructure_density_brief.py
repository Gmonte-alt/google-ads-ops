#!/usr/bin/env python3
"""
restructure_density_brief.py — answers two Phase 2 planning questions:

[A] FLAGSHIP BOOK SHARE: revenue and leads from flagship-book (bare-brand
    navigational) keywords over the last 14/30/60 days, as dollars and as
    a share of the Home search account. Two cuts: all campaigns, and
    ex-CA/FL (= what the NB Flagship campaign will absorb).

[B] FULL-CYCLE LEVEL-SET: leads and revenue by book over the last
    COMPLETE pre-restructure spend cycle (Aug 2 - Sep 12, 42 days),
    so expectations are phase-independent rather than trough-anchored.

[C] DATA DENSITY: decision-grain comparison. Old structure = campaigns/
    ad groups that actually spent during that full cycle. New structure =
    the 4-campaign design (+CA/FL standalones). Output: leads per
    campaign per cycle and per ad group per cycle, before vs after.

Usage: python restructure_density_brief.py --db paid_search.db
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
CYCLE = ("2026-08-02", "2026-09-12")   # last complete pre-launch cycle
SHORT = {23394808297: "TEXAS", 23496334752: "TIER1", 23530860942: "TIER2",
         23586637678: "TIER3", 23428389081: "TIER4",
         23385188622: "CALIFORNIA", 23394808300: "FLORIDA"}
CAFL = {23385188622, 23394808300}
NB_GROUPS = 9      # staged NB flagship carrier groups
NB_KEYS = 18


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

    book_of, camp_of = {}, {}
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
        bk = tags.get((SHORT[cid],) + k) or text_tags.get(k)
        if bk:
            book_of[r["id"]] = bk
            camp_of[r["id"]] = cid
    if not book_of:
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
    M = dt.date.fromisoformat(g_max)
    wins = {"L14": ((M - dt.timedelta(days=13)).isoformat(), g_max),
            "L30": ((M - dt.timedelta(days=29)).isoformat(), g_max),
            "L60": ((M - dt.timedelta(days=59)).isoformat(), g_max),
            "CYCLE": CYCLE}
    start_all = min(w[0] for w in wins.values())

    # book-grain pulls: (win, book, cafl?) -> [co, cv, rv]
    agg = defaultdict(lambda: [0.0] * 3)
    ids = list(book_of)
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        ph = ",".join("?" * len(chunk))
        for r in con.execute(
                f"SELECT instance_id iid, {dcol} d, "
                f"SUM(cost_micros)/1e6 co, SUM(primary_conv_count) cv, "
                f"SUM(primary_conv_value) rv FROM "
                f"keyword_performance_daily WHERE instance_id IN ({ph}) "
                f"{filt}AND {dcol} >= ? GROUP BY 1,2",
                chunk + [start_all]):
            bk = book_of[r["iid"]]
            fl = camp_of[r["iid"]] in CAFL
            for wn, (s, e) in wins.items():
                if s <= r["d"] <= e:
                    c = agg[(wn, bk, fl)]
                    c[0] += r["co"] or 0
                    c[1] += r["cv"] or 0
                    c[2] += r["rv"] or 0

    # account totals (ad-group grain, keywordless included)
    agt = "ad_group_performance_daily"
    ac = {r[1] for r in con.execute(f"PRAGMA table_info({agt})")}
    ad = "report_date" if "report_date" in ac else "date"
    af = "AND device='ALL' " if "device" in ac else ""
    nc = next(x for x in ("ad_group_name", "adgroup_name", "ad_group")
              if x in ac)
    cvc = next((x for x in ("primary_conv_count", "conversions")
                if x in ac), None)
    rvc = next((x for x in ("primary_conv_value", "conversion_value")
                if x in ac), None)
    camps = ",".join(f"{c},'{c}'" for c in SHORT)
    acct = {}
    for wn, w in wins.items():
        r = con.execute(
            f"SELECT SUM(cost_micros)/1e6 co, SUM({cvc}) cv, "
            f"SUM({rvc}) rv FROM {agt} WHERE campaign_id IN ({camps}) "
            f"{af}AND {ad} BETWEEN ? AND ?", w).fetchone()
        acct[wn] = [r["co"] or 0, r["cv"] or 0, r["rv"] or 0]

    print(f"[DATA] through {g_max}\n")
    print("[A] FLAGSHIP BOOK (bare-brand navigational) — revenue & leads")
    print(f"   window   flagship rev   leads    acct rev   rev share  "
          f"lead share")
    for wn in ("L14", "L30", "L60"):
        fb = [agg[(wn, "FLAGSHIP", False)][i]
              + agg[(wn, "FLAGSHIP", True)][i] for i in range(3)]
        at = acct[wn]
        print(f"   {wn}     ${fb[2]:>9,.0f}  {fb[1]:>6,.0f}  "
              f"${at[2]:>9,.0f}   {fb[2]/at[2] if at[2] else 0:6.1%}   "
              f"{fb[1]/at[1] if at[1] else 0:6.1%}")
    print(f"\n   ex-CA/FL (what the NB Flagship campaign absorbs):")
    for wn in ("L14", "L30", "L60"):
        fb = agg[(wn, "FLAGSHIP", False)]
        at = acct[wn]
        print(f"   {wn}     ${fb[2]:>9,.0f}  {fb[1]:>6,.0f}"
              f"{'':>13}   {fb[2]/at[2] if at[2] else 0:6.1%}   "
              f"{fb[1]/at[1] if at[1] else 0:6.1%}")

    print(f"\n[B] FULL-CYCLE LEVEL-SET — {CYCLE[0]} to {CYCLE[1]} "
          f"(42 days, last complete pre-launch cycle)")
    print(f"   book         ex-CA/FL leads   rev        CA/FL leads  rev")
    for bk in ("GENERIC", "BRAND-HOME", "FLAGSHIP"):
        x = agg[("CYCLE", bk, False)]
        y = agg[("CYCLE", bk, True)]
        print(f"   {bk:12} {x[1]:>8,.0f}   ${x[2]:>9,.0f}   "
              f"{y[1]:>8,.0f}  ${y[2]:>9,.0f}")
    at = acct["CYCLE"]
    print(f"   ACCOUNT (incl. untagged/keywordless): {at[1]:,.0f} leads, "
          f"${at[2]:,.0f} rev, ${at[0]:,.0f} cost")

    print(f"\n[C] DATA DENSITY — decision grain, per full cycle")
    # old: campaigns/groups that spent in the cycle
    oldg = con.execute(
        f"SELECT COUNT(DISTINCT campaign_id || '|' || {nc}) FROM {agt} "
        f"WHERE campaign_id IN ({camps}) {af}AND {ad} BETWEEN ? AND ? "
        f"AND cost_micros > 0", CYCLE).fetchone()[0]
    oldc = con.execute(
        f"SELECT COUNT(DISTINCT campaign_id) FROM {agt} WHERE "
        f"campaign_id IN ({camps}) {af}AND {ad} BETWEEN ? AND ? AND "
        f"cost_micros > 0", CYCLE).fetchone()[0]
    # new: groups currently spending post-launch in natl + BH + CAFL,
    # plus the staged NB groups
    post14 = ((M - dt.timedelta(days=13)).isoformat(), g_max)
    newg_live = con.execute(
        f"SELECT COUNT(DISTINCT campaign_id || '|' || {nc}) FROM {agt} "
        f"WHERE campaign_id IN ({camps}) {af}AND {ad} BETWEEN ? AND ? "
        f"AND cost_micros > 0", post14).fetchone()[0]
    newg = newg_live + NB_GROUPS
    cyc_leads = at[1]
    print(f"   OLD (as of the {CYCLE[0]} cycle): {oldc} campaigns, "
          f"{oldg} spending ad groups")
    print(f"       -> {cyc_leads/oldc if oldc else 0:,.0f} leads/campaign"
          f"/cycle, {cyc_leads/oldg if oldg else 0:,.1f} leads/ad group"
          f"/cycle")
    gen_l = agg[("CYCLE", "GENERIC", False)][1]
    bh_l = agg[("CYCLE", "BRAND-HOME", False)][1]
    fl_l = agg[("CYCLE", "FLAGSHIP", False)][1]
    cafl_l = sum(agg[("CYCLE", b, True)][1]
                 for b in ("GENERIC", "BRAND-HOME", "FLAGSHIP"))
    print(f"   NEW (same cycle's leads mapped onto the new structure, "
          f"{newg_live} live + {NB_GROUPS} staged NB groups = {newg} "
          f"groups):")
    for nm, L in (("Nationwide Generic", gen_l),
                  ("Brand + Home (combined)", bh_l),
                  (f"NB Flagship ({NB_KEYS} keys)", fl_l),
                  ("CA/FL standalones", cafl_l)):
        print(f"       {nm:28} {L:>8,.0f} leads/cycle  "
              f"({L/42:,.1f}/day)")
    print(f"       -> account average {cyc_leads/newg:,.1f} leads/ad "
          f"group/cycle ({oldg/newg:.1f}x densification at group grain, "
          f"{oldc/4:.1f}x at campaign grain)")
    other = at[1] - gen_l - bh_l - fl_l - cafl_l
    print(f"\n   Note: cycle-total basis makes these phase-independent. "
          f"Untagged/keywordless leads ({other:,.0f}) sit in the "
          f"account row only.")


if __name__ == "__main__":
    main()
