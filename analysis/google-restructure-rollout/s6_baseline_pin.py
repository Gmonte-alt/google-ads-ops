#!/usr/bin/env python3
"""
s6_baseline_pin.py — S6 BASELINE CAPTURE (run before Stage 1 posts).

Pins the five pre-registered metrics against the account as it
stands, computed at BOOK grain (frozen tags, campaign-agnostic — the
class is the unit, exactly as the structure case measures):
  1. generic CPC          2. generic CPL
  3. generic impressions  (raw AND share-of-class; denominator =
     generic+brand-home+flagship impressions, live frozen-tag
     instances, stated exactly)
  4. flagship CPL         5. blended CPL (all three books)
Windows: BASE28 = trailing 28 days ending at data max (the "before");
REF = 2026-03-01..07-28 printed for context only.

Writes output/s6_baseline_<datamax>.txt with the capture timestamp
and the three PRE-DECLARED steps, so the pin and the declarations are
one artifact. Read-only DB.

Usage: python s6_baseline_pin.py --db paid_search.db
       --assignment keyword_book_assignment_FROZEN_2026-08-27.csv
"""
import argparse
import csv
import datetime as dt
import os
import sqlite3
import sys
from collections import defaultdict

REF = ("2026-03-01", "2026-07-28")
RULESET_EXPECTED = "8/27+9/2a+9/2b+9/4-aggtail+9/8-prodintent"
PRE_DECLARED = """PRE-DECLARED STEPS (written into the S6 plan at baseline pin,
before any Stage 1 post — none of these is a mid-window intervention):
  (a) ~+14d: D5 target step — flagship tROAS tightens, both
      home-intent books loosen.
  (b) post-+28d: themed-creative replacement of the stopgap DKI RSAs
      in Home-Generic-{Quote,State,Peril,Other} (strategy-ruled
      condition of the stopgap approval).
  (c) no earlier than +28d: Wave-2 generic ad groups
      (Cost/Coverage/Compare/Companies/Hazard), GKP-gated.
READS: cutover +14 / +28 / +56. Falsification clause as
pre-registered in the S6 plan. Launch generic creative is DKI copies
of Core's incumbents until (b) — first-window CTR/pCTR movement is
structure + negatives, not copy."""


def die(m):
    print(f"*** HARD FAIL: {m}")
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--assignment", required=True)
    a = ap.parse_args()
    f = open(a.assignment, encoding="utf-8-sig")
    meta = f.readline().strip()
    if RULESET_EXPECTED not in meta:
        die(f"frozen metadata mismatch: {meta}")
    tags = {}
    for r in csv.DictReader(f):
        if r["decision"].startswith("EXCLUDE"):
            continue
        tags[(r["campaign"], r["keyword"].lower().strip(),
              r["match_type"].upper())] = r["book"]
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    camp_short = {}
    for r in con.execute("SELECT campaign_id, campaign_name FROM "
                         "campaigns WHERE account_id=1"):
        n = r["campaign_name"].upper()
        for s in ("TEXAS", "TIER1", "TIER2", "TIER3", "TIER4",
                  "CALIFORNIA", "FLORIDA"):
            if s in n or (s.replace("TIER", "TIER") + "RPL") in n:
                camp_short[r["campaign_id"]] = s
    # generic/brand-home/flagship instance sets via frozen tags
    book_of_inst = {}
    for r in con.execute(
            "SELECT id, campaign_id, keyword_text, match_type FROM "
            "keyword_instances WHERE account_id=1"):
        cs = camp_short.get(r["campaign_id"])
        if not cs:
            continue
        bk = tags.get((cs, (r["keyword_text"] or "").lower().strip(),
                       (r["match_type"] or "").upper()))
        if bk:
            book_of_inst[r["id"]] = bk
    if not book_of_inst:
        die("no instances mapped to frozen tags — check campaign "
            "short-name resolution")
    kpd_cols = {r[1] for r in con.execute(
        "PRAGMA table_info(keyword_performance_daily)")}
    filt = ""
    if "platform" in kpd_cols:
        filt += "AND LOWER(platform)='google' "
    if "device" in kpd_cols:
        filt += "AND device='ALL' "
    if "geo_state" in kpd_cols:
        filt += "AND geo_state='ALL' "
    print("[KPD] filters resolved: "
          + (filt.strip() or "(none — account scoping only)"))
    g_max = con.execute(
        "SELECT MAX(report_date) FROM keyword_performance_daily "
        "WHERE 1=1 " + filt).fetchone()[0]
    BASE28 = ((dt.date.fromisoformat(g_max)
               - dt.timedelta(days=27)).isoformat(), g_max)
    agg = {w: defaultdict(lambda: [0.0] * 4)
           for w in ("BASE28", "REF")}
    ph_ids = list(book_of_inst)
    for chunk in (ph_ids[i:i + 900] for i in range(0, len(ph_ids), 900)):
        ph = ",".join("?" * len(chunk))
        for r in con.execute(
                f"SELECT instance_id iid, report_date d, "
                f"SUM(impressions) im, SUM(clicks) ck, "
                f"SUM(cost_micros)/1e6 co, SUM(primary_conv_count) cv "
                f"FROM keyword_performance_daily WHERE instance_id IN "
                f"({ph}) " + filt + f"AND report_date >= ? "
                f"GROUP BY 1, 2", list(chunk) + [REF[0]]):
            bk = book_of_inst[r["iid"]]
            for w, (s, e) in (("BASE28", BASE28), ("REF", REF)):
                if s <= r["d"] <= e:
                    c = agg[w][bk]
                    c[0] += r["im"] or 0
                    c[1] += r["ck"] or 0
                    c[2] += r["co"] or 0
                    c[3] += r["cv"] or 0
    stamp = dt.datetime.now().isoformat(timespec="seconds")
    lines = [f"S6 BASELINE PIN — captured {stamp}",
             f"frozen edition: {meta}",
             f"data through: {g_max} | BASE28 = {BASE28[0]}..{BASE28[1]}"
             f" (the 'before') | REF context = {REF[0]}..{REF[1]}",
             f"instances mapped: {len(book_of_inst):,} "
             f"(campaign-agnostic book grain)", ""]
    for w in ("BASE28", "REF"):
        g, b, fl = agg[w]["GENERIC"], agg[w]["BRAND-HOME"], agg[w]["FLAGSHIP"]
        tot_im = g[0] + b[0] + fl[0]
        bl = [g[i] + b[i] + fl[i] for i in range(4)]
        lines.append(f"=== {w} ===")
        lines.append(f"  1. generic CPC: "
                     f"${g[2]/g[1] if g[1] else 0:.2f}  "
                     f"({g[1]:,.0f} clicks, ${g[2]:,.0f})")
        lines.append(f"  2. generic CPL: "
                     f"${g[2]/g[3] if g[3] else 0:.2f}  "
                     f"({g[3]:,.0f} leads)")
        lines.append(f"  3. generic impressions: {g[0]:,.0f} raw | "
                     f"share-of-class {g[0]/tot_im if tot_im else 0:.1%}"
                     f"  [denominator = generic+brand-home+flagship "
                     f"impressions, live frozen-tag instances, this "
                     f"same computation at every read]")
        lines.append(f"  4. flagship CPL: "
                     f"${fl[2]/fl[3] if fl[3] else 0:.2f}  "
                     f"({fl[3]:,.0f} leads)")
        lines.append(f"  5. blended CPL: "
                     f"${bl[2]/bl[3] if bl[3] else 0:.2f}  "
                     f"(${bl[2]:,.0f} / {bl[3]:,.0f} leads)")
        lines.append("")
    lines.append(PRE_DECLARED)
    out = "\n".join(lines)
    print(out)
    os.makedirs("output", exist_ok=True)
    path = f"output/s6_baseline_{g_max}.txt"
    with open(path, "w") as fh:
        fh.write(out + "\n")
    print(f"\n[PINNED] {path}")


if __name__ == "__main__":
    main()
