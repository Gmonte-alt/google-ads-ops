#!/usr/bin/env python3
"""
quote_group_probe.py — reconciles the workbook's keyword-grain ROAS
against the UI's ad-group numbers for the nationwide Generic campaign,
and diagnoses the Home-Generic-Quote drop.

Sections:
  [A] Quote group daily, AD-GROUP grain (what the UI shows): cost,
      impressions, IS / lost-to-rank, and ROAS if the table carries
      conversion columns.
  [B] Quote keywords daily, KEYWORD grain: per-keyword cost/leads/value.
  [C] THE WEDGE, campaign-wide per day: ad-group-grain cost minus
      keyword-grain cost for the nationwide campaign = spend serving
      WITHOUT keyword attribution (AI Max matching + anything untagged
      at the platform level). If this is large and growing, AI Max is
      buying queries in the new states.
  [D] Untagged keywords: instances in the nationwide campaign with
      POST-window spend that the frozen book does not cover — the
      "missing from generic tagging" audit.

Read-only; runtime column resolution; house filters.
Usage: python quote_group_probe.py --db paid_search.db
       --assignment keyword_book_assignment_FROZEN_2026-08-27.csv
"""
import argparse
import csv
import datetime as dt
import sqlite3
import sys
from collections import defaultdict

NATL = ("23394808297", 23394808297)
QUOTE_TEXTS = {"home insurance quotes", "homeowners insurance quote",
               "home insurance estimate"}
START = "2026-10-01"
RULESET = "8/27+9/2a+9/2b+9/4-aggtail+9/8-prodintent"


def die(m):
    print(f"*** HARD FAIL: {m}")
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--assignment", required=True)
    a = ap.parse_args()
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    f = open(a.assignment, encoding="utf-8-sig")
    meta = f.readline().strip()
    if RULESET not in meta:
        die(f"frozen metadata mismatch: {meta}")
    tags = {}
    text_tags = {}
    for r in csv.DictReader(f):
        if r["decision"].startswith("EXCLUDE"):
            continue
        k = (r["keyword"].lower().strip(), r["match_type"].upper())
        tags[(r["campaign"],) + k] = r["book"]
        text_tags.setdefault(k, r["book"])

    kic = {r[1] for r in con.execute("PRAGMA table_info(keyword_instances)")}
    agcol = next((x for x in ("ad_group_name", "adgroup_name", "ad_group")
                  if x in kic), None)
    inst = {}
    for r in con.execute(
            "SELECT id, keyword_text, match_type"
            + (f", {agcol}" if agcol else "") +
            " FROM keyword_instances WHERE account_id=1 AND "
            "campaign_id IN (?,?)", NATL):
        inst[r["id"]] = ((r["keyword_text"] or "").lower().strip(),
                         (r["match_type"] or "").upper(),
                         r[agcol] if agcol else "")
    if not inst:
        die("no instances found in the nationwide campaign")
    print(f"[SCOPE] {len(inst):,} keyword instances in the nationwide "
          f"campaign" + ("" if agcol else
                         "  (no ad-group column on instances — [B] uses "
                         "keyword text to identify the Quote keys)"))

    kc = {r[1] for r in con.execute(
        "PRAGMA table_info(keyword_performance_daily)")}
    dcol = "report_date" if "report_date" in kc else "date"
    filt = ""
    if "device" in kc:
        filt += "AND device='ALL' "
    if "geo_state" in kc:
        filt += "AND geo_state='ALL' "
    g_max = con.execute(f"SELECT MAX({dcol}) FROM keyword_performance_daily"
                        f" WHERE 1=1 {filt}").fetchone()[0]

    # keyword-grain daily for the campaign
    kw_day = defaultdict(lambda: [0.0] * 3)   # d -> co, cv, rv
    q_day = defaultdict(lambda: defaultdict(lambda: [0.0] * 3))
    untagged = defaultdict(lambda: [0.0] * 3)  # (kw,mt) -> co,cv,rv POST
    cover = defaultdict(lambda: [0.0] * 3)     # book|UNTAGGED -> POST
    ids = list(inst)
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        ph = ",".join("?" * len(chunk))
        for r in con.execute(
                f"SELECT instance_id iid, {dcol} d, "
                f"SUM(cost_micros)/1e6 co, SUM(primary_conv_count) cv, "
                f"SUM(primary_conv_value) rv FROM keyword_performance_daily"
                f" WHERE instance_id IN ({ph}) {filt}AND {dcol} >= ? "
                f"GROUP BY 1,2", chunk + [START]):
            kw, mt, ag = inst[r["iid"]]
            kw_day[r["d"]][0] += r["co"] or 0
            kw_day[r["d"]][1] += r["cv"] or 0
            kw_day[r["d"]][2] += r["rv"] or 0
            if kw in QUOTE_TEXTS:
                c = q_day[r["d"]][(kw, mt)]
                c[0] += r["co"] or 0
                c[1] += r["cv"] or 0
                c[2] += r["rv"] or 0
            if r["d"] >= "2026-10-14":
                bk = tags.get(("TEXAS", kw, mt)) or text_tags.get((kw, mt))
                if bk is None:
                    u = untagged[(kw, mt)]
                    u[0] += r["co"] or 0
                    u[1] += r["cv"] or 0
                    u[2] += r["rv"] or 0
                cover[bk or "UNTAGGED"][0] += r["co"] or 0
                cover[bk or "UNTAGGED"][1] += r["cv"] or 0
                cover[bk or "UNTAGGED"][2] += r["rv"] or 0

    # ad-group grain daily
    tset = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    agt = "ad_group_performance_daily"
    ag_day = {}
    qg_day = {}
    has_conv = False
    if agt in tset:
        ac = {r[1] for r in con.execute(f"PRAGMA table_info({agt})")}
        ad = "report_date" if "report_date" in ac else "date"
        af = "AND device='ALL' " if "device" in ac else ""
        nc = next((x for x in ("ad_group_name", "adgroup_name", "ad_group")
                   if x in ac), None)
        cvc = next((x for x in ("primary_conv_count", "conversions")
                    if x in ac), None)
        rvc = next((x for x in ("primary_conv_value", "conversion_value",
                                "conversions_value") if x in ac), None)
        isc = next((x for x in ("search_impression_share",) if x in ac),
                   None)
        lrc = next((x for x in ("search_rank_lost_is",) if x in ac), None)
        has_conv = bool(cvc and rvc)
        sel = "SUM(cost_micros)/1e6 co, SUM(impressions) im"
        if has_conv:
            sel += f", SUM({cvc}) cv, SUM({rvc}) rv"
        if isc:
            sel += (f", SUM({isc}*impressions)/SUM(impressions) iw_is")
        if lrc:
            sel += (f", SUM({lrc}*impressions)/SUM(impressions) iw_lr")
        for r in con.execute(
                f"SELECT {ad} d, {sel} FROM {agt} WHERE campaign_id IN "
                f"(?,?) {af}AND {ad} >= ? GROUP BY 1",
                (*NATL, START)):
            ag_day[r["d"]] = dict(r)
        for r in con.execute(
                f"SELECT {ad} d, {sel} FROM {agt} WHERE campaign_id IN "
                f"(?,?) AND {nc}='Home-Generic-Quote' {af}AND {ad} >= ? "
                f"GROUP BY 1", (*NATL, START)):
            qg_day[r["d"]] = dict(r)
    else:
        print("[WARN] ad-group table missing — sections A/C unavailable")

    print(f"\n[A] HOME-GENERIC-QUOTE — ad-group grain daily (= the UI)")
    hdr = "   date         cost    impr"
    if has_conv:
        hdr += "   leads   ROAS"
    hdr += "     IS   lost-rank"
    print(hdr)
    d0 = dt.date.fromisoformat(START)
    while d0.isoformat() <= g_max:
        ds = d0.isoformat()
        g = qg_day.get(ds)
        if g:
            ln = (f"   {ds}  ${g['co'] or 0:>6,.0f}  {g['im'] or 0:>6,.0f}")
            if has_conv:
                co, rv = g["co"] or 0, g.get("rv") or 0
                ln += (f"  {g.get('cv') or 0:>5.1f}  "
                       f"{rv/co if co else 0:5.2f}")
            ln += (f"  {g.get('iw_is') or 0:5.1%}  "
                   f"{g.get('iw_lr') or 0:6.1%}"
                   if g.get("iw_is") is not None else "")
            print(ln + ("  <LAUNCH" if ds == "2026-10-13" else ""))
        d0 += dt.timedelta(days=1)

    print(f"\n[B] QUOTE KEYWORDS — keyword grain, POST window totals")
    tot = defaultdict(lambda: [0.0] * 3)
    for d, kws in q_day.items():
        if d >= "2026-10-14":
            for k, v in kws.items():
                for i in range(3):
                    tot[k][i] += v[i]
    for (kw, mt), v in sorted(tot.items(), key=lambda kv: -kv[1][0]):
        print(f"   {kw:34} [{mt:6}]  ${v[0]:>7,.0f}  {v[1]:>5.1f} leads  "
              f"ROAS {v[2]/v[0] if v[0] else 0:4.2f}")

    print(f"\n[C] THE WEDGE — nationwide campaign, ad-group cost minus "
          f"keyword cost per day")
    print(f"   (= spend serving WITHOUT a keyword: AI Max matching + "
          f"platform-untracked)")
    print(f"   date        adgrp$    kw$    wedge$   wedge%")
    d0 = dt.date.fromisoformat(START)
    while d0.isoformat() <= g_max:
        ds = d0.isoformat()
        ag_co = (ag_day.get(ds) or {}).get("co") or 0
        kw_co = kw_day.get(ds, [0, 0, 0])[0]
        w = ag_co - kw_co
        print(f"   {ds}  ${ag_co:>6,.0f}  ${kw_co:>6,.0f}  ${w:>+6,.0f}"
              f"  {w/ag_co if ag_co else 0:6.1%}"
              + ("  <LAUNCH" if ds == "2026-10-13" else ""))
        d0 += dt.timedelta(days=1)
    if has_conv:
        pa = sum((v.get("co") or 0) for d, v in ag_day.items()
                 if d >= "2026-10-14")
        pr = sum((v.get("rv") or 0) for d, v in ag_day.items()
                 if d >= "2026-10-14")
        pk = sum(v[0] for d, v in kw_day.items() if d >= "2026-10-14")
        pkr = sum(v[2] for d, v in kw_day.items() if d >= "2026-10-14")
        print(f"\n   POST totals: ad-group grain ROAS "
              f"{pr/pa if pa else 0:4.2f} vs keyword grain "
              f"{pkr/pk if pk else 0:4.2f} — the gap is the keywordless "
              f"spend's performance.")

    print(f"\n[D] BOOK COVERAGE of nationwide-campaign keyword spend, "
          f"POST window")
    print(f"   (the workbook's 'Generic' rows count only GENERIC-tagged "
          f"keywords)")
    for bk, v in sorted(cover.items(), key=lambda kv: -kv[1][0]):
        print(f"   {bk:12}  ${v[0]:>7,.0f}  {v[1]:>5.1f} leads  ROAS "
              f"{v[2]/v[0] if v[0] else 0:4.2f}")
    print(f"\n   Untagged keywords with POST spend:")
    if untagged:
        for (kw, mt), v in sorted(untagged.items(),
                                  key=lambda kv: -kv[1][0])[:20]:
            print(f"   {kw:40} [{mt:6}]  ${v[0]:>7,.0f}  "
                  f"{v[1]:>4.1f} leads")
        print(f"   total untagged POST spend: "
              f"${sum(v[0] for v in untagged.values()):,.0f}")
    else:
        print("   none — every spending keyword is book-covered")


if __name__ == "__main__":
    main()
