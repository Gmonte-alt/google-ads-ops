#!/usr/bin/env python3
"""
core_overlap_check.py — are 'home insurance' [BROAD] and 'homeowners
insurance' [BROAD] redundant, or routing distinct query families?

For each keyword (all account-1 BROAD instances of the exact text),
pull its in-window queries (pinned conventions: sqp.instance_id join,
platform google, device ALL rollup, search_queries.normalized_text).
Report per window (REF = 2026-03-01..07-28, L90):
  - each keyword's stream: queries / clicks / cost / leads
  - SHARED queries (matched by both): each keyword's share of volume
    on them
  - EXCLUSIVE streams: size + top queries by leads
  - theme mix via search_queries.theme (the tagged dimension), so the
    two streams' composition is visible, not just their overlap
Decision guide printed at the end. Read-only DB.

Usage: python core_overlap_check.py --db paid_search.db
"""
import argparse
import datetime as dt
import re
import sqlite3
import sys
from collections import defaultdict

KW_A = "home insurance"
KW_B = "homeowners insurance"
MATCH = "BROAD"
REF = ("2026-03-01", "2026-07-28")


def die(m):
    print(f"*** HARD FAIL: {m}")
    sys.exit(1)


def norm_kw(t):
    t = re.sub(r"[+\[\]\"]", " ", (t or "").lower())
    return " ".join(t.split())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    a = ap.parse_args()
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    ids = {KW_A: [], KW_B: []}
    for r in con.execute(
            "SELECT id, keyword_text, match_type FROM keyword_instances "
            "WHERE account_id=1"):
        k = norm_kw(r["keyword_text"])
        if k in ids and (r["match_type"] or "").upper().startswith(MATCH):
            ids[k].append(r["id"])
    for k, v in ids.items():
        if not v:
            die(f"no BROAD instances for '{k}'")
        print(f"[IDS] '{k}': {len(v)} instances")
    id_of = {}
    for k, v in ids.items():
        for i in v:
            id_of[i] = k

    devs = [str(r[0]) for r in con.execute(
        "SELECT DISTINCT device FROM search_query_performance "
        "WHERE LOWER(platform)='google'")]
    has_all = next((d for d in devs if d.upper() == "ALL"), None)
    dev_f = f"AND p.device='{has_all}' " if has_all else ""
    g_max = con.execute(
        "SELECT MAX(report_date) FROM search_query_performance "
        "WHERE LOWER(platform)='google'").fetchone()[0]
    L90 = ((dt.date.fromisoformat(g_max)
            - dt.timedelta(days=89)).isoformat(), g_max)

    all_ids = ids[KW_A] + ids[KW_B]
    ph = ",".join("?" * len(all_ids))
    # (kw, win) -> {query: [im, ck, co, cv]}; theme mix per kw/win
    qs = {(k, w): defaultdict(lambda: [0.0] * 4)
          for k in (KW_A, KW_B) for w in ("REF", "L90")}
    theme = {(k, w): defaultdict(float)
             for k in (KW_A, KW_B) for w in ("REF", "L90")}
    for r in con.execute(
            f"SELECT sq.normalized_text nq, sq.theme th, "
            f"p.report_date d, p.instance_id iid, "
            f"SUM(p.clicks) ck, SUM(p.impressions) im, "
            f"SUM(p.cost_micros)/1e6 co, SUM(p.primary_conv_count) cv "
            f"FROM search_query_performance p "
            f"JOIN search_queries sq ON sq.id = p.query_id "
            f"WHERE p.instance_id IN ({ph}) "
            f"AND LOWER(p.platform)='google' " + dev_f +
            f"AND p.report_date >= ? GROUP BY 1, 2, 3, 4",
            all_ids + [REF[0]]):
        k = id_of[r["iid"]]
        for w, (s, e) in (("REF", REF), ("L90", L90)):
            if s <= r["d"] <= e:
                cell = qs[(k, w)][r["nq"] or ""]
                cell[0] += r["im"] or 0
                cell[1] += r["ck"] or 0
                cell[2] += r["co"] or 0
                cell[3] += r["cv"] or 0
                theme[(k, w)][r["th"] or "(untagged)"] += r["ck"] or 0

    print(f"\n[OVERLAP CHECK] '{KW_A}' vs '{KW_B}' | REF "
          f"{REF[0]}..{REF[1]} | L90 {L90[0]}..{L90[1]} "
          f"(data through {g_max})")
    print("[CAVEAT] shares over reportable queries; suppression hides "
          "the low-volume tail (~15% of conversions historically).")
    for w in ("REF", "L90"):
        A, B = qs[(KW_A, w)], qs[(KW_B, w)]
        shared = set(A) & set(B)
        def tot(d, i, keys=None):
            src = d if keys is None else {q: d[q] for q in keys if q in d}
            return sum(v[i] for v in src.values())
        print(f"\n=== {w} ===")
        for k, d, other in ((KW_A, A, B), (KW_B, B, A)):
            excl = set(d) - set(other)
            ck, cv = tot(d, 1), tot(d, 3)
            sck, scv = tot(d, 1, shared), tot(d, 3, shared)
            print(f"'{k}': {len(d):,} queries | {ck:,.0f} clicks | "
                  f"${tot(d,2):,.0f} | {cv:,.0f} leads")
            print(f"   on SHARED queries: {sck:,.0f} clicks "
                  f"({sck/ck if ck else 0:.0%}), {scv:,.0f} leads "
                  f"({scv/cv if cv else 0:.0%})")
            etop = sorted(((q, d[q]) for q in excl),
                          key=lambda kv: -kv[1][3])[:6]
            print(f"   EXCLUSIVE: {len(excl):,} queries, "
                  f"{tot(d,1,excl):,.0f} clicks, "
                  f"{tot(d,3,excl):,.0f} leads; top:")
            for q, v in etop:
                print(f"      {q[:48]:48} {v[1]:>6,.0f} clk "
                      f"{v[3]:>5,.1f} leads")
        for k in (KW_A, KW_B):
            mix = sorted(theme[(k, w)].items(),
                         key=lambda kv: -kv[1])[:5]
            s = sum(theme[(k, w)].values()) or 1
            print(f"theme mix '{k}': " + ", ".join(
                f"{t}={c/s:.0%}" for t, c in mix))
    print("\nDECISION GUIDE: redundancy = BOTH keywords' volume "
          "concentrated on SHARED queries (>=~70%) AND the "
          "candidate-to-pause's exclusive stream small and "
          "survivor-eligible. Distinct theme mixes or a fat exclusive "
          "stream = routing, not redundancy — keep both. Under "
          "campaign tROAS the bidding-signal gain from consolidation "
          "is minor; the exclusive tails and per-keyword Expected-CTR "
          "ledgers are what's actually at stake.")


if __name__ == "__main__":
    main()
