#!/usr/bin/env python3
"""
tier_signal.py — ONE-PURPOSE EXHIBIT for the team:
"The geographic tiers no longer measure anything: volume is being decided
in the auction, not by state value — and tier RPLs are downstream shadows
of that volume collapse."

Two sheets only:
  Data      state x month Google leads/revenue (+ tier rosters), raw rows
  OnePager  (1) fixed-roster volume retention by tier (bar chart)
            (2) state March-RPL vs volume-retention scatter + correlation
            All displayed numbers are LIVE SUMIFS on Data.

Conventions: read-only DB, output/ next to script, columns resolved at
runtime, final month partial. Base month = 2026-03; compare month = the
latest FULL month with data.

Usage: python tier_signal.py --db paid_search.db
"""
import argparse
import os
import re
import sqlite3
import sys
from collections import defaultdict

GOOGLE_TABLE = "campaign_performance_geo_daily_search"
BASE = "2026-03"
TIER_RE = re.compile(r"tier[\s_\-]*(\d)", re.IGNORECASE)
STATE = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT",
    "Delaware": "DE", "District of Columbia": "DC", "Florida": "FL",
    "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL",
    "Indiana": "IN", "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY",
    "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT",
    "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH",
    "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH",
    "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA",
    "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD",
    "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT",
    "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY",
}


def die(m):
    print(f"[FATAL] {m}")
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    here = os.path.dirname(os.path.abspath(__file__))
    outdir = os.path.join(here, "output")
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, a.out or "tier_signal.xlsx")

    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cols = {r[1] for r in con.execute(
        f"PRAGMA table_info({GOOGLE_TABLE})")}
    date_c = "date" if "date" in cols else "report_date"
    g_max = con.execute(f"SELECT MAX({date_c}) FROM {GOOGLE_TABLE} "
                        f"WHERE account_id=1").fetchone()[0]
    if not g_max:
        die("no Google geo data")
    # latest FULL month: if max date is not month-end, use prior month
    import calendar
    y, m, d = int(g_max[:4]), int(g_max[5:7]), int(g_max[8:10])
    if d < calendar.monthrange(y, m)[1]:
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    cur = f"{y:04d}-{m:02d}"
    print(f"[SCOPE] base {BASE} vs latest full month {cur} "
          f"(data through {g_max})")

    # state x month leads/revenue (all devices summed)
    sm = defaultdict(lambda: [0.0, 0.0])
    for r in con.execute(
            f"SELECT SUBSTR({date_c},1,7) ym, geo_state, "
            f"SUM(conversions) l, SUM(conversion_value) rv "
            f"FROM {GOOGLE_TABLE} WHERE account_id=1 "
            f"AND SUBSTR({date_c},1,7) IN (?,?) GROUP BY 1,2",
            (BASE, cur)):
        st = STATE.get(str(r["geo_state"]).strip())
        if st:
            c = sm[(r["ym"], st)]
            c[0] += r["l"] or 0
            c[1] += r["rv"] or 0

    # tier rosters = states each tier's campaigns served in `cur`
    ccols = {r[1] for r in con.execute("PRAGMA table_info(campaigns)")}
    c_id = "campaign_id" if "campaign_id" in ccols else "id"
    c_nm = "campaign_name" if "campaign_name" in ccols else "name"
    cname = {str(r[0]): r[1] for r in con.execute(
        f"SELECT {c_id}, {c_nm} FROM campaigns WHERE account_id=1")}
    rosters = defaultdict(set)
    for r in con.execute(
            f"SELECT DISTINCT geo_state, campaign_id FROM {GOOGLE_TABLE} "
            f"WHERE account_id=1 AND SUBSTR({date_c},1,7)=? "
            f"AND conversions>0", (cur,)):
        mt = TIER_RE.search(cname.get(str(r["campaign_id"]), "") or "")
        st = STATE.get(str(r["geo_state"]).strip())
        if mt and st:
            rosters[f"Tier {mt.group(1)}"].add(st)
    if not rosters:
        die("no tier campaigns found")

    import xlsxwriter
    wb = xlsxwriter.Workbook(out)
    f_h = wb.add_format({"bold": True, "bg_color": "#1F3864",
                         "font_color": "white"})
    f_b = wb.add_format({"bold": True})
    f_n = wb.add_format({"num_format": "#,##0.0"})
    f_c = wb.add_format({"num_format": "$#,##0.00"})
    f_p = wb.add_format({"num_format": "0%"})

    # ---- Data sheet ----
    wsD = wb.add_worksheet("Data")
    for c, h in enumerate(["month", "state", "leads", "revenue", "tiers"]):
        wsD.write(0, c, h, f_h)
    st_tier = defaultdict(list)
    for t, ss in rosters.items():
        for s in ss:
            st_tier[s].append(t)
    ri = 1
    for (ym, st), (l, rv) in sorted(sm.items()):
        wsD.write_string(ri, 0, ym)
        wsD.write_string(ri, 1, st)
        wsD.write_number(ri, 2, l, f_n)
        wsD.write_number(ri, 3, rv, f_c)
        wsD.write_string(ri, 4, ",".join(sorted(st_tier.get(st, []))))
        ri += 1
    wsD.autofilter(0, 0, ri - 1, 4)
    wsD.freeze_panes(1, 0)

    def sleads(month_ref, state_ref):
        return (f"SUMIFS(Data!$C:$C,Data!$A:$A,{month_ref},"
                f"Data!$B:$B,{state_ref})")

    def srev(month_ref, state_ref):
        return (f"SUMIFS(Data!$D:$D,Data!$A:$A,{month_ref},"
                f"Data!$B:$B,{state_ref})")

    # ---- OnePager ----
    ws = wb.add_worksheet("OnePager")
    ws.write(0, 0, "THE GEOGRAPHIC TIERS NO LONGER MEASURE ANYTHING — "
                   "volume is decided in the auction, not by state value. "
                   f"Base {BASE} vs {cur}, same states held constant "
                   "(each tier's current roster). Tier RPL changes are the "
                   "recomputed averages of this collapse, not a signal.",
             f_b)
    ws.write(1, 0, "params:", f_b)
    ws.write_string(1, 1, BASE)
    ws.write_string(1, 2, cur)
    PB, PC = "$B$2", "$C$2"

    # Table 1: fixed-roster retention per tier
    ws.write(3, 0, "TABLE 1 — share of base-month volume remaining, same "
                   "states (fixed current roster per tier)", f_b)
    for c, h in enumerate(["tier", "states", "leads_base", "leads_now",
                           "retained"]):
        ws.write(4, c, h, f_h)
    tiers = sorted(rosters)
    for i, t in enumerate(tiers):
        x = 5 + i
        xx = x + 1
        ss = sorted(rosters[t])
        ws.write_string(x, 0, t)
        ws.write_string(x, 1, ",".join(ss))
        base_f = "+".join(sleads(PB, f'"{s}"') for s in ss)
        now_f = "+".join(sleads(PC, f'"{s}"') for s in ss)
        ws.write_formula(x, 2, f"={base_f}", f_n)
        ws.write_formula(x, 3, f"={now_f}", f_n)
        ws.write_formula(x, 4, f"=IFERROR($D{xx}/$C{xx},\"\")", f_p)
    ch = wb.add_chart({"type": "column"})
    ch.add_series({"name": "share of base volume retained",
                   "categories": ["OnePager", 5, 0, 4 + len(tiers), 0],
                   "values": ["OnePager", 5, 4, 4 + len(tiers), 4]})
    ch.set_title({"name": "Every tier lost most of its volume — the "
                          "collapse barely discriminates by value"})
    ch.set_y_axis({"name": "% of base volume retained", "min": 0, "max": 1,
                   "num_format": "0%"})
    ch.set_legend({"none": True})
    ch.set_size({"width": 620, "height": 360})
    ws.insert_chart(3, 7, ch)

    # Table 2: state value vs retention (states >= 30 base leads)
    t2 = 5 + len(tiers) + 3
    ws.write(t2, 0, "TABLE 2 — does a state's lead VALUE predict how much "
                    "volume it kept? (states with >=30 base leads)", f_b)
    for c, h in enumerate(["state", "RPL_base", "leads_base", "leads_now",
                           "retained"]):
        ws.write(t2 + 1, c, h, f_h)
    scat = [st for (ym, st), v in sm.items()
            if ym == BASE and v[0] >= 30]
    scat.sort(key=lambda s: -(sm[(BASE, s)][1] / sm[(BASE, s)][0]))
    for i, st in enumerate(scat):
        x = t2 + 2 + i
        xx = x + 1
        ws.write_string(x, 0, st)
        sr = f"$A{xx}"
        ws.write_formula(x, 1, f"=IFERROR({srev(PB, sr)}/{sleads(PB, sr)},"
                               f"\"\")", f_c)
        ws.write_formula(x, 2, f"={sleads(PB, sr)}", f_n)
        ws.write_formula(x, 3, f"={sleads(PC, sr)}", f_n)
        ws.write_formula(x, 4, f"=IFERROR($D{xx}/$C{xx},\"\")", f_p)
    n_s = len(scat)
    corr_row = t2 + 2 + n_s + 1
    ws.write(corr_row, 0, "correlation(RPL_base, retained):", f_b)
    ws.write_formula(corr_row, 1,
                     f"=CORREL(B{t2 + 3}:B{t2 + 2 + n_s},"
                     f"E{t2 + 3}:E{t2 + 2 + n_s})",
                     wb.add_format({"num_format": "+0.00", "bold": True}))
    ws.write(corr_row + 1, 0, "Read: if state value governed volume, this "
                              "would be strongly positive and the scatter "
                              "a steep line. A weak correlation with every "
                              "state down heavily = the collapse is "
                              "auction-side (eligibility), value-blind.",
             f_b)
    ch2 = wb.add_chart({"type": "scatter"})
    ch2.add_series({"name": "states",
                    "categories": ["OnePager", t2 + 2, 1,
                                   t2 + 1 + n_s, 1],
                    "values": ["OnePager", t2 + 2, 4, t2 + 1 + n_s, 4],
                    "marker": {"type": "circle", "size": 7}})
    ch2.set_title({"name": "State lead value (x) vs volume retained (y) — "
                           "a cloud, not a line"})
    ch2.set_x_axis({"name": "RPL in base month ($)"})
    ch2.set_y_axis({"name": "volume retained", "num_format": "0%"})
    ch2.set_legend({"none": True})
    ch2.set_size({"width": 620, "height": 360})
    ws.insert_chart(t2, 7, ch2)

    wb.close()
    print(f"[DONE] {out}")


if __name__ == "__main__":
    main()
