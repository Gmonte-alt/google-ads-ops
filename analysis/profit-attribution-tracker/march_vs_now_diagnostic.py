#!/usr/bin/env python3
"""
march_vs_now_diagnostic.py -- one-shot prep for the "what changed since
March" question (Google Home, account 1, read-only).

Three sections:

1. MONTHLY SERIES (Jan..current): clicks, cost, CPC, leads, CvR, revenue,
   RPL, GP -- verifies the premises ("lowest clicks all year", "RPL ~2x
   the prior low-volume period") against the keyword-lens truth.

2. MIX-FIXED RPL: recomputes the current period's RPL holding the
   BASELINE month's category x match mix constant.
     measured RPL uplift = mix-fixed uplift (real payout change)
                           + mix component (composition/survivorship)
   If mix-fixed ~ flat, the "2x RPL" is composition, not richer payouts,
   and is NOT available at the margin.

3. CvR SHIFT-SHARE (baseline month vs current period), category x match:
     dCvR = MIX (share shifts across cells, at baseline cell CvRs)
          + RATE (within-cell CvR changes, at current shares)
   Answers "is the CR fall keyword mix?" with a number.

USAGE
-----
    python march_vs_now_diagnostic.py --db "C:\\...\\paid_search.db"
    python march_vs_now_diagnostic.py --db ... --baseline 2026-03 \\
        --current-start 2026-07-01 --current-end 2026-07-19
"""

from __future__ import annotations

import argparse
import calendar
import datetime as dt
import os
import sqlite3
import sys

import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ACCOUNT_ID = 1


def fail(msg):
    print("\n[FAIL] " + msg)
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--baseline", default="2026-03",
                    help="Baseline month YYYY-MM (default 2026-03)")
    ap.add_argument("--current-start", default=None,
                    help="Current period start (default: 1st of this month)")
    ap.add_argument("--current-end", default=None,
                    help="Current period end (default: yesterday)")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        fail(f"DB not found: {args.db}")
    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA query_only = ON")

    today = dt.date.today()
    cur_end = (dt.date.fromisoformat(args.current_end) if args.current_end
               else today - dt.timedelta(days=1))
    cur_start = (dt.date.fromisoformat(args.current_start)
                 if args.current_start else cur_end.replace(day=1))
    by, bm = map(int, args.baseline.split("-"))
    base_start = dt.date(by, bm, 1)
    base_end = dt.date(by, bm, calendar.monthrange(by, bm)[1])

    kpd_cols = [r[1] for r in conn.execute(
        "PRAGMA table_info('keyword_performance_daily')")]
    clicks_c = "clicks" if "clicks" in kpd_cols else None
    impr_c = next((c for c in ("impressions", "impr") if c in kpd_cols), None)

    # ---------------- 1. Monthly series -------------------------------------
    sel_extra = ""
    if clicks_c:
        sel_extra += f", SUM(p.{clicks_c}) AS clicks"
    if impr_c:
        sel_extra += f", SUM(p.{impr_c}) AS impressions"
    ser = pd.read_sql_query(f"""
        SELECT substr(p.report_date, 1, 7) AS month,
               COUNT(DISTINCT p.report_date) AS days,
               SUM(p.cost_micros)/1e6 AS cost,
               SUM(p.primary_conv_value) AS revenue,
               SUM(p.primary_conv_count) AS leads
               {sel_extra}
        FROM keyword_performance_daily p
        JOIN keyword_instances ki ON ki.id = p.instance_id
        WHERE ki.account_id = :a AND p.device = 'ALL'
          AND p.report_date >= :s AND p.report_date <= :e
        GROUP BY 1 ORDER BY 1
    """, conn, params={"a": ACCOUNT_ID, "s": f"{by}-01-01",
                       "e": cur_end.isoformat()})
    if ser.empty:
        fail("No keyword data in range.")
    ser["gp"] = ser.revenue - ser.cost
    ser["rpl"] = ser.revenue / ser.leads
    ser["cpl"] = ser.cost / ser.leads
    if clicks_c:
        ser["cpc"] = ser.cost / ser.clicks
        ser["cvr%"] = ser.leads / ser.clicks * 100
        ser["clicks_d"] = ser.clicks / ser.days
    ser["gp_d"] = ser.gp / ser.days
    ser["leads_d"] = ser.leads / ser.days
    print("=" * 100)
    print("1. MONTHLY SERIES (keyword lens, device=ALL) -- premise check")
    print("=" * 100)
    cols = ["month", "days", "clicks_d", "cpc", "cvr%", "leads_d",
            "rpl", "cpl", "gp_d"]
    cols = [c for c in cols if c in ser.columns]
    print(ser[cols].round(2).to_string(index=False))
    if impr_c:
        ser["impr_d"] = ser.impressions / ser.days
        print("impressions/day:",
              {r.month: round(r.impr_d) for r in ser.itertuples()})

    # ---------------- pulls for 2 & 3 ---------------------------------------
    def cell_pull(s, e):
        return pd.read_sql_query(f"""
            SELECT m.keyword_category,
                   COALESCE(m.match_type, 'n/a') AS match_type,
                   SUM(p.cost_micros)/1e6 AS cost,
                   SUM(p.primary_conv_value) AS revenue,
                   SUM(p.primary_conv_count) AS leads
                   {', SUM(p.clicks) AS clicks' if clicks_c else ''}
            FROM keyword_performance_daily p
            JOIN keyword_instances ki ON ki.id = p.instance_id
            JOIN keyword_category_map_current m
              ON m.ad_group_criterion_id = ki.ad_group_criterion_id
             AND m.account_id = ki.account_id
            WHERE ki.account_id = :a AND p.device != 'ALL'
              AND p.report_date BETWEEN :s AND :e
            GROUP BY 1, 2
        """, conn, params={"a": ACCOUNT_ID, "s": s.isoformat(),
                           "e": e.isoformat()})

    base = cell_pull(base_start, base_end)
    cur = cell_pull(cur_start, cur_end)
    conn.close()
    if base.empty or cur.empty:
        fail(f"No mapped cell data for baseline {args.baseline} or current "
             f"period. NOTE: the category map classifies CURRENT placements; "
             f"baseline-month traffic on since-removed placements is "
             f"excluded -- coverage %% is printed below when computable.")

    key = ["keyword_category", "match_type"]
    m = base.merge(cur, on=key, how="outer",
                   suffixes=("_b", "_c")).fillna(0.0)

    # ---------------- 2. Mix-fixed RPL --------------------------------------
    print("\n" + "=" * 100)
    print(f"2. MIX-FIXED RPL -- is the RPL uplift real payout or "
          f"composition?  ({args.baseline} mix held fixed)")
    print("=" * 100)
    rpl_b = base.revenue.sum() / base.leads.sum()
    rpl_c = cur.revenue.sum() / cur.leads.sum()
    cell_rpl_c = (m.revenue_c / m.leads_c.replace(0, pd.NA)).fillna(0)
    w_b = m.leads_b / m.leads_b.sum()
    mixfix = float((cell_rpl_c * w_b)[m.leads_c > 0].sum() /
                   w_b[m.leads_c > 0].sum())
    print(f"  baseline measured RPL : {rpl_b:7.2f}")
    print(f"  current  measured RPL : {rpl_c:7.2f}   "
          f"({(rpl_c/rpl_b-1)*100:+.0f}% vs baseline)")
    print(f"  current  MIX-FIXED RPL: {mixfix:7.2f}   "
          f"({(mixfix/rpl_b-1)*100:+.0f}% vs baseline -- today's payouts "
          f"at the baseline lead mix)")
    comp = rpl_c - mixfix
    print(f"  -> composition/survivorship component: {comp:+.2f} of the "
          f"{rpl_c-rpl_b:+.2f} total uplift "
          f"({(comp/(rpl_c-rpl_b)*100 if rpl_c != rpl_b else 0):.0f}%)")
    print("  Reading: if mix-fixed ~ baseline, the RPL rise is WHICH leads "
          "remain, not richer payouts,\n  and is not available at the "
          "margin -- expanding volume re-buys the low-RPL mix.")

    # ---------------- 3. CvR shift-share ------------------------------------
    if clicks_c:
        print("\n" + "=" * 100)
        print(f"3. CvR SHIFT-SHARE ({args.baseline} vs "
              f"{cur_start}..{cur_end}) -- is the CR fall keyword mix?")
        print("=" * 100)
        cvr_b = base.leads.sum() / base.clicks.sum()
        cvr_c = cur.leads.sum() / cur.clicks.sum()
        cell_b = (m.leads_b / m.clicks_b.replace(0, pd.NA)).fillna(0)
        cell_c = (m.leads_c / m.clicks_c.replace(0, pd.NA)).fillna(0)
        sh_b = m.clicks_b / m.clicks_b.sum()
        sh_c = m.clicks_c / m.clicks_c.sum()
        mix_comp = float(((sh_c - sh_b) * cell_b).sum())
        rate_comp = float((sh_c * (cell_c - cell_b)).sum())
        print(f"  CvR: {cvr_b*100:.1f}% -> {cvr_c*100:.1f}%   "
              f"(d {100*(cvr_c-cvr_b):+.1f}pts)")
        print(f"  MIX  component (share shifts across category x match): "
              f"{mix_comp*100:+.1f}pts")
        print(f"  RATE component (within-cell CvR change):               "
              f"{rate_comp*100:+.1f}pts")
        print("  Cells with the largest within-cell CvR change "
              "(click-weighted):")
        m["rate_contrib"] = sh_c * (cell_c - cell_b) * 100
        top = m.reindex(m.rate_contrib.abs().sort_values(
            ascending=False).index).head(8)
        for _, r in top.iterrows():
            print(f"    {r.keyword_category:<22} x {r.match_type:<7} "
                  f"CvR {cell_b[r.name]*100:5.1f}% -> "
                  f"{cell_c[r.name]*100:5.1f}%   "
                  f"contribution {r.rate_contrib:+.1f}pts")
        print("  Reading: RATE >> MIX -> the CR fall is NOT keyword mix; "
              "it's within-cell conversion\n  (funnel / marginal-click "
              "quality). MIX >> RATE -> composition shifted toward "
              "lower-converting cells.")
    else:
        print("\n[NOTE] keyword table has no clicks column -- CvR "
              "shift-share skipped.")


if __name__ == "__main__":
    main()
