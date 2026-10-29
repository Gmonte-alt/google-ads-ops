#!/usr/bin/env python3
"""
phase1_geo_split.py — where the nationwide Generic campaign's spend and
leads actually land, Texas (incumbent geo) vs the 44 new states.

Tests the geo-expansion confound behind the CVR/CPL WATCHes: if Texas
held its economics and the new states run lower, the quality dip is the
expansion finding its level, not a routing defect.

Sections:
  [A] Texas vs new states — spend/leads/CPL (+ROAS where the table has
      value), pre (Oct 3-12) vs post (Oct 14-max).
  [B] Top states post-launch, by spend.
  [C] States spending with zero leads post (watch list).
  [D] Integrity: spend in states that must NOT serve (CA, FL and the
      surge removals MN, NM, AR). Expect $0.

Geo table convention (architect): native device splits, no ALL rollup —
rows are summed, never filtered to a device.

Usage: python phase1_geo_split.py --db paid_search.db
"""
import argparse
import sqlite3
import sys
from collections import defaultdict

PRE = ("2026-10-03", "2026-10-12")
POST_START = "2026-10-14"
NATL = 23394808297
EXCLUDED = {"California", "Florida", "Minnesota", "New Mexico",
            "Arkansas"}


def die(m):
    print(f"*** HARD FAIL: {m}")
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    a = ap.parse_args()
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    gt = "campaign_performance_geo_daily_search"
    tset = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    if gt not in tset:
        die(f"{gt} not found")
    gc = {r[1] for r in con.execute(f"PRAGMA table_info({gt})")}
    dcol = "report_date" if "report_date" in gc else "date"
    cvc = "conversions" if "conversions" in gc else "primary_conv_count"
    rvc = next((x for x in ("conversion_value", "conversions_value",
                            "primary_conv_value") if x in gc), None)
    ckc = "clicks" if "clicks" in gc else None
    g_max = con.execute(
        f"SELECT MAX({dcol}) FROM {gt} WHERE campaign_id IN "
        f"({NATL},'{NATL}')").fetchone()[0]
    if not g_max or g_max < POST_START:
        die(f"no post-launch geo rows for the nationwide campaign "
            f"(max date {g_max})")
    POST = (POST_START, g_max)

    def ndays(w):
        import datetime as dt
        return (dt.date.fromisoformat(w[1])
                - dt.date.fromisoformat(w[0])).days + 1

    nd = {"pre": ndays(PRE), "post": ndays(POST)}
    print(f"[WINDOWS] PRE {PRE[0]}..{PRE[1]} | POST {POST[0]}..{POST[1]}"
          f" | value column: {rvc or 'NONE'} | clicks: "
          f"{'yes' if ckc else 'no'}")

    sel = f"SUM(cost_micros)/1e6 co, SUM({cvc}) cv"
    if rvc:
        sel += f", SUM({rvc}) rv"
    if ckc:
        sel += f", SUM({ckc}) ck"
    st = defaultdict(lambda: {"pre": [0.0] * 4, "post": [0.0] * 4})
    for wn, w in (("pre", PRE), ("post", POST)):
        for r in con.execute(
                f"SELECT geo_state g, {sel} FROM {gt} WHERE campaign_id "
                f"IN ({NATL},'{NATL}') AND {dcol} BETWEEN ? AND ? "
                f"GROUP BY 1", w):
            e = st[r["g"]][wn]
            e[0] += r["co"] or 0
            e[1] += r["cv"] or 0
            e[2] += (r["rv"] or 0) if rvc else 0
            e[3] += (r["ck"] or 0) if ckc else 0

    def line(lbl, p, q):
        def f(v, w):
            co, cv, rv, ck = v
            n = nd[w]
            s = (f"${co/n:>7,.0f}/d {cv/n:>5.1f} ld/d "
                 f"CPL ${co/cv if cv else 0:>5,.0f}")
            if rvc:
                s += f" ROAS {rv/co if co else 0:4.2f}"
            if ckc:
                s += f" CVR {cv/ck if ck else 0:5.1%}"
            return s
        print(f"   {lbl:28} PRE  {f(p, 'pre')}")
        print(f"   {'':28} POST {f(q, 'post')}")

    tx_p = st.get("Texas", {"pre": [0.0] * 4})["pre"]
    tx_q = st.get("Texas", {"post": [0.0] * 4})["post"]
    new_p, new_q = [0.0] * 4, [0.0] * 4
    for g, e in st.items():
        if g == "Texas":
            continue
        for i in range(4):
            new_p[i] += e["pre"][i]
            new_q[i] += e["post"][i]
    print("\n[A] TEXAS (incumbent) vs NEW STATES")
    line("Texas", tx_p, tx_q)
    line("New states (44)", new_p, new_q)
    tot_q = [tx_q[i] + new_q[i] for i in range(4)]
    if tot_q[0]:
        print(f"   New-state share of post spend: "
              f"{new_q[0]/tot_q[0]:.0%} | of post leads: "
              f"{new_q[1]/tot_q[1] if tot_q[1] else 0:.0%}")

    print("\n[B] TOP STATES POST-LAUNCH (by spend)")
    tops = sorted(st.items(), key=lambda kv: -kv[1]["post"][0])[:15]
    for g, e in tops:
        q = e["post"]
        if q[0] < 1:
            continue
        s = (f"   {g:22} ${q[0]/nd['post']:>6,.0f}/d "
             f"{q[1]/nd['post']:>5.1f} ld/d "
             f"CPL ${q[0]/q[1] if q[1] else 0:>5,.0f}")
        if rvc:
            s += f" ROAS {q[2]/q[0] if q[0] else 0:4.2f}"
        print(s)

    print("\n[C] STATES SPENDING WITH ZERO LEADS POST")
    dead = [(g, e["post"][0]) for g, e in st.items()
            if e["post"][0] >= 20 and e["post"][1] == 0]
    for g, co in sorted(dead, key=lambda t: -t[1]):
        print(f"   {g:22} ${co:,.0f} total, 0 leads")
    if not dead:
        print("   none above $20")
    else:
        print(f"   total: ${sum(c for _, c in dead):,.0f} across "
              f"{len(dead)} states")

    print("\n[D] INTEGRITY — excluded states must show $0 post")
    bad = 0
    for g in sorted(EXCLUDED):
        co = st.get(g, {"post": [0.0] * 4})["post"][0]
        flag = "OK" if co < 5 else "*** SERVING — CHECK GEO TARGETING"
        if co >= 5:
            bad += 1
        print(f"   {g:22} ${co:>7,.0f}  {flag}")
    print("   " + ("ALL CLEAN" if bad == 0 else
                   f"{bad} EXCLUDED STATE(S) SPENDING — fix before "
                   f"Phase 2"))


if __name__ == "__main__":
    main()
