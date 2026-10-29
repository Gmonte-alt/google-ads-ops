#!/usr/bin/env python3
"""
generic_share_trend.py — generic share of Home search spend, daily and
weekly, since Sep 1. Answers: did the generic % of account spend draw
down before or after the Phase 1 launch (Oct 13)?

Numerator: GENERIC-book keyword spend (frozen tags + 10/21 addendum),
all seven search campaigns including CA/FL — share-of-account is an
account question, so nothing is excluded here.
Denominator: ad-group-grain spend summed over the same seven campaigns
(includes keywordless/AI-Max spend; PMax is out of scope by ruling).

Usage: python generic_share_trend.py --db paid_search.db
       --assignment keyword_book_assignment_FROZEN_2026-08-27.csv
       --addendum keyword_book_addendum_2026-10-21.csv
"""
import argparse
import csv
import datetime as dt
import sqlite3
import sys
from collections import defaultdict

START = "2026-09-01"
LAUNCH = "2026-10-13"
RULESET = "8/27+9/2a+9/2b+9/4-aggtail+9/8-prodintent"
SHORT = {23394808297: "TEXAS", 23496334752: "TIER1", 23530860942: "TIER2",
         23586637678: "TIER3", 23428389081: "TIER4",
         23385188622: "CALIFORNIA", 23394808300: "FLORIDA"}


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

    gen_ids = []
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
        if bk == "GENERIC":
            gen_ids.append(r["id"])
    if not gen_ids:
        die("no generic instances mapped")
    print(f"[SCOPE] {len(gen_ids):,} generic keyword instances")

    kc = {r[1] for r in con.execute(
        "PRAGMA table_info(keyword_performance_daily)")}
    dcol = "report_date" if "report_date" in kc else "date"
    filt = ""
    if "device" in kc:
        filt += "AND device='ALL' "
    if "geo_state" in kc:
        filt += "AND geo_state='ALL' "
    gen_d = defaultdict(float)
    for i in range(0, len(gen_ids), 900):
        chunk = gen_ids[i:i + 900]
        ph = ",".join("?" * len(chunk))
        for r in con.execute(
                f"SELECT {dcol} d, SUM(cost_micros)/1e6 co FROM "
                f"keyword_performance_daily WHERE instance_id IN ({ph}) "
                f"{filt}AND {dcol} >= ? GROUP BY 1", chunk + [START]):
            gen_d[r["d"]] += r["co"] or 0

    agt = "ad_group_performance_daily"
    ac = {r[1] for r in con.execute(f"PRAGMA table_info({agt})")}
    ad = "report_date" if "report_date" in ac else "date"
    af = "AND device='ALL' " if "device" in ac else ""
    camps = ",".join(f"{c},'{c}'" for c in SHORT)
    tot_d = {}
    for r in con.execute(
            f"SELECT {ad} d, SUM(cost_micros)/1e6 co FROM {agt} WHERE "
            f"campaign_id IN ({camps}) {af}AND {ad} >= ? GROUP BY 1",
            (START,)):
        tot_d[r["d"]] = r["co"] or 0
    g_max = max(tot_d) if tot_d else die("no account spend rows")

    print(f"\n[WEEKLY] generic share of Home search spend "
          f"(7 campaigns, PMax excluded)")
    print(f"   week starting    generic$/d   account$/d   share")
    d0 = dt.date.fromisoformat(START)
    d0 -= dt.timedelta(days=d0.weekday())  # align to Monday
    while d0.isoformat() <= g_max:
        days = [(d0 + dt.timedelta(days=i)).isoformat() for i in range(7)]
        days = [d for d in days if START <= d <= g_max]
        if days:
            g = sum(gen_d.get(d, 0) for d in days) / len(days)
            t = sum(tot_d.get(d, 0) for d in days) / len(days)
            mark = "  << launch week" if any(d == LAUNCH for d in days) \
                else ""
            print(f"   {d0.isoformat()}       ${g:>7,.0f}    ${t:>8,.0f}"
                  f"   {g/t if t else 0:5.1%}{mark}")
        d0 += dt.timedelta(days=7)

    print(f"\n[DAILY] last 21 days")
    print(f"   date          generic$   account$   share")
    d0 = dt.date.fromisoformat(g_max) - dt.timedelta(days=20)
    while d0.isoformat() <= g_max:
        ds = d0.isoformat()
        g, t = gen_d.get(ds, 0), tot_d.get(ds, 0)
        print(f"   {ds}   ${g:>7,.0f}  ${t:>8,.0f}   "
              f"{g/t if t else 0:5.1%}"
              + ("  << LAUNCH" if ds == LAUNCH else ""))
        d0 += dt.timedelta(days=1)


if __name__ == "__main__":
    main()
