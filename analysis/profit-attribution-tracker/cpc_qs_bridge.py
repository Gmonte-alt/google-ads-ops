#!/usr/bin/env python3
"""
cpc_qs_bridge.py — how much of the WoW CPC increase runs through Quality Score?
v2 — QS side rebuilt per DB architect guidance (8/19):
  * QS source PINNED: keyword_quality_daily.historical_quality_score,
    PK (instance_id, report_date), maintained daily by kw 06. The four
    QS-named columns on keyword_performance_daily are superseded/NULL — never read.
  * QS joined at DAILY grain (LEFT JOIN on instance_id AND report_date) —
    never latest-per-key (look-ahead + survivorship). Join miss = unrated day.
  * Canonical weighting: iw_QS = SUM(qs*impressions)/SUM(impressions) over
    rated rows only; rated_share ships alongside every iw_QS.
  * match_type held as a dimension (EXACT has different QS and auction
    dynamics; without it the CPC-QS relation is confounded).

Framing: QS is a slow monthly variable — it did not move this week, so it
cannot CAUSE a WoW CPC jump. QS prices the cohorts. Decomposition (exact,
computed at QS-bucket x match_type cell grain):
    dCPC = WITHIN (each cell's CPC change at prev click mix)
         + MIX    (click share shifting toward differently-priced cells)
         + CROSS
Usage: python cpc_qs_bridge.py [--db PATH] [--no-save]
"""

import argparse
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
ACCOUNT_ID = 1
OUTPUT_DIR = (Path(r"C:\Projects\google-ads-ops\analysis"
                   r"\profit-attribution-tracker\output") / "diagnostics")
LOOKBACK_DAYS = 16

SQL = """
SELECT p.report_date            AS date,
       k.match_type             AS match_type,
       {carrier_expr}           AS carrier_src,
       q.historical_quality_score AS qs,
       SUM(p.impressions)       AS impressions,
       SUM(p.clicks)            AS clicks,
       SUM(p.cost_micros)/1e6   AS cost
FROM keyword_performance_daily p
JOIN keyword_instances k ON k.id = p.instance_id
LEFT JOIN keyword_quality_daily q
       ON q.instance_id = p.instance_id AND q.report_date = p.report_date
WHERE k.account_id = {account_id} AND p.device = 'ALL'
  AND p.report_date >= :min_date
GROUP BY 1, 2, 3
"""


CARRIERS = ["Geico", "Farmers", "Hartford", "Kin", "Travelers", "Amica",
            "AAA", "State Farm", "Allstate", "Progressive",
            "American Family", "Plymouth Rock", "NerdWallet", "Policygenius",
            "Bamboo", "Costco", "USAA", "Liberty Mutual", "Nationwide",
            "Lemonade", "Hippo", "AARP", "Safeco", "Erie", "Mercury",
            "Chubb", "Wawanesa", "CSAA", "21st Century", "Allied",
            "Universal Property", "American Home"]
import re as _re
_CARRIER_RX = [(c, _re.compile(_re.escape(c.lower()).replace(r"\ ", r"\s*")))
               for c in CARRIERS]


def carrier_of(text):
    t = (text or "").lower()
    for name, rx in _CARRIER_RX:
        if rx.search(t):
            return name
    return "(non-carrier)"


def bucket(q):
    if q is None or (isinstance(q, float) and np.isnan(q)):
        return "unrated"
    if q <= 3:
        return "QS<=3"
    if q <= 6:
        return "QS 4-6"
    return "QS 7+"


def week_windows(latest):
    monday = latest - timedelta(days=latest.weekday())
    this_days = [monday + timedelta(days=i)
                 for i in range((latest - monday).days + 1)]
    return this_days, [d - timedelta(days=7) for d in this_days]


def fmtd(days):
    return f"{days[0].month}/{days[0].day}-{days[-1].month}/{days[-1].day}"


def iw_qs(d):
    """Canonical: SUM(qs*impressions)/SUM(impressions) over rated rows;
    returns (iw_qs, rated_share)."""
    imps = d["impressions"].sum()
    rated = d[d["qs"].notna()]
    r_imps = rated["impressions"].sum()
    share = r_imps / imps if imps else np.nan
    val = ((rated["qs"] * rated["impressions"]).sum() / r_imps
           if r_imps else np.nan)
    return val, share


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db")
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()
    db = Path(args.db) if args.db else DB_PATH
    if not db.exists():
        sys.exit(f"DB not found at {db}")
    con = sqlite3.connect(db)
    out = ["=" * 78,
           f"CPC x QS BRIDGE — account {ACCOUNT_ID} — run "
           f"{date.today().isoformat()}",
           "=" * 78,
           "framing: QS did not change this week — it prices the cohorts. "
           "WITHIN = auction",
           "pressure inside each cell; MIX = clicks shifting toward "
           "differently-priced cells.",
           "cells = QS bucket x match_type (per architect: EXACT has "
           "different QS and auction",
           "dynamics — unconditioned CPC-QS relations are confounded).",
           "sources: keyword_performance_daily JOIN keyword_instances "
           "[pinned]; QS =",
           "keyword_quality_daily.historical_quality_score, DAILY-grain "
           "LEFT JOIN on",
           "(instance_id, report_date) [pinned per architect]. iw_QS = "
           "SUM(qs*impr)/SUM(impr)",
           "over rated rows; rated_share shown with every iw_QS."]
    try:
        min_date = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
        ki_cols = [r[1] for r in con.execute(
            "PRAGMA table_info(keyword_instances)").fetchall()]
        brand_col = next((c for c in ("carrier_brand", "brand_name", "brand")
                          if c in ki_cols), None)
        text_col = next((c for c in ("keyword_text", "text", "keyword")
                         if c in ki_cols), None)
        carrier_expr = ("k." + brand_col) if brand_col else (
            ("k." + text_col) if text_col else "''")
        carrier_method = (f"keyword_instances.{brand_col} (tagger column)"
                          if brand_col else
                          (f"regex on keyword_instances.{text_col}"
                           if text_col else "unavailable"))
        df = pd.read_sql_query(SQL.format(carrier_expr=carrier_expr,
                                  account_id=ACCOUNT_ID), con,
                               params={"min_date": min_date})
        if df.empty:
            sys.exit("No rows in window.")
        df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
        df = df.dropna(subset=["date"])
        df["qs"] = pd.to_numeric(df["qs"], errors="coerce")
        df["bucket"] = df["qs"].map(bucket)
        df["match_type"] = df["match_type"].astype(str).str.upper().str.strip()
        if brand_col:
            df["carrier"] = (df["carrier_src"].astype(str)
                             .replace({"nan": "", "None": ""}))
            df.loc[df["carrier"] == "", "carrier"] = "(non-carrier)"
        else:
            df["carrier"] = df["carrier_src"].astype(str).map(carrier_of)

        latest = df["date"].max()
        this_days, last_days = week_windows(latest)
        cur = df[df["date"].isin(this_days)]
        prv = df[df["date"].isin(last_days)]

        # headline iw_QS + rated_share per window
        for lbl, d in (("prev wk " + fmtd(last_days), prv),
                       ("this wk " + fmtd(this_days), cur)):
            v, s = iw_qs(d)
            out.append(f"\naccount iw_QS {lbl}: "
                       + (f"{v:.2f}" if not np.isnan(v) else "n/a")
                       + f"  (rated_share {s:.0%})")
        out.append("  (a QS move with a rated_share move is composition "
                   "until proven otherwise)")

        # bridge at bucket x match cells
        dims = ["bucket", "match_type"]
        c = cur.groupby(dims)[["clicks", "cost", "impressions"]].sum()
        v = prv.groupby(dims)[["clicks", "cost", "impressions"]].sum()
        m = c.join(v, lsuffix="_cur", rsuffix="_prv", how="outer").fillna(0)
        Kp, Kc = m["clicks_prv"].sum(), m["clicks_cur"].sum()
        Cp, Cc = m["cost_prv"].sum(), m["cost_cur"].sum()
        cpc_p, cpc_c = Cp / Kp, Cc / Kc
        m["s_prv"] = m["clicks_prv"] / Kp
        m["s_cur"] = m["clicks_cur"] / Kc
        m["cpc_prv"] = (m["cost_prv"] / m["clicks_prv"]).replace(
            [np.inf, -np.inf], np.nan)
        m["cpc_cur"] = (m["cost_cur"] / m["clicks_cur"]).replace(
            [np.inf, -np.inf], np.nan)
        cp0 = m["cpc_prv"].fillna(cpc_p)
        cc0 = m["cpc_cur"].fillna(cpc_c)
        m["within"] = m["s_prv"] * (cc0 - cp0)
        m["mix"] = (m["s_cur"] - m["s_prv"]) * (cp0 - cpc_p)
        m["cross"] = (m["s_cur"] - m["s_prv"]) * (cc0 - cp0)

        out.append(f"\nBLENDED CPC: ${cpc_p:.2f} -> ${cpc_c:.2f}  "
                   f"(dCPC ${cpc_c - cpc_p:+.2f}, {cpc_c / cpc_p - 1:+.1%})")
        bucket_order = ["QS<=3", "QS 4-6", "QS 7+", "unrated"]
        out.append("\nPER CELL (QS bucket x match):")
        out.append("  cell                clicks p->c    share p->c     "
                   "CPC p->c        within     mix   cross")
        idx = sorted(m.index, key=lambda t: (bucket_order.index(t[0])
                                             if t[0] in bucket_order else 9,
                                             t[1]))
        for cell in idx:
            r = m.loc[cell]
            name = f"{cell[0]} {cell[1]}"
            out.append(f"  {name:<18}{r['clicks_prv']:>6,.0f}->"
                       f"{r['clicks_cur']:<6,.0f}"
                       f"{r['s_prv']:>6.1%}->{r['s_cur']:<6.1%}"
                       f"  ${(0 if np.isnan(r['cpc_prv']) else r['cpc_prv']):>5.2f}->"
                       f"${(0 if np.isnan(r['cpc_cur']) else r['cpc_cur']):<5.2f}"
                       f"  ${r['within']:>+5.2f} ${r['mix']:>+5.2f} "
                       f"${r['cross']:>+5.2f}")
        # rollups
        out.append("\nROLLUP BY QS BUCKET (contributions to dCPC):")
        rb = m.groupby(level=0)[["within", "mix", "cross"]].sum().reindex(
            [b for b in bucket_order if b in m.index.get_level_values(0)])
        for b, r in rb.iterrows():
            out.append(f"  {b:<10} within ${r['within']:>+5.2f}  "
                       f"mix ${r['mix']:>+5.2f}  cross ${r['cross']:>+5.2f}")
        out.append("ROLLUP BY MATCH TYPE:")
        rm = m.groupby(level=1)[["within", "mix", "cross"]].sum()
        for b, r in rm.iterrows():
            out.append(f"  {b:<10} within ${r['within']:>+5.2f}  "
                       f"mix ${r['mix']:>+5.2f}  cross ${r['cross']:>+5.2f}")
        w, x, cr = m["within"].sum(), m["mix"].sum(), m["cross"].sum()
        out.append(f"  TOTAL      within ${w:>+5.2f}  mix ${x:>+5.2f}  "
                   f"cross ${cr:>+5.2f}   (sum ${w + x + cr:+.2f} = dCPC)")

        # ---- carrier breakdown ----
        out.append(f"\nBY CARRIER (carrier mapping: {carrier_method}) — "
                   "contributions to account dCPC, carrier-grain cells:")
        cc_ = cur.groupby("carrier")[["clicks", "cost", "impressions"]].sum()
        cv_ = prv.groupby("carrier")[["clicks", "cost", "impressions"]].sum()
        cm = cc_.join(cv_, lsuffix="_cur", rsuffix="_prv", how="outer").fillna(0)
        cm["s_prv"] = cm["clicks_prv"] / Kp
        cm["s_cur"] = cm["clicks_cur"] / Kc
        cm["cpc_prv"] = (cm["cost_prv"] / cm["clicks_prv"]).replace(
            [np.inf, -np.inf], np.nan)
        cm["cpc_cur"] = (cm["cost_cur"] / cm["clicks_cur"]).replace(
            [np.inf, -np.inf], np.nan)
        cp1 = cm["cpc_prv"].fillna(cpc_p)
        cc1 = cm["cpc_cur"].fillna(cpc_c)
        cm["within"] = cm["s_prv"] * (cc1 - cp1)
        cm["mix"] = (cm["s_cur"] - cm["s_prv"]) * (cp1 - cpc_p)
        cm["cross"] = (cm["s_cur"] - cm["s_prv"]) * (cc1 - cp1)
        cm["contrib"] = cm["within"] + cm["mix"] + cm["cross"]
        cm = cm.sort_values("contrib", ascending=False)
        out.append("  carrier            clicks p->c     CPC p->c        "
                   "dCPC   within    mix  cross  contrib | iw_QS(cur) "
                   "rated  QS<=3 clk%")
        for car, r in cm.iterrows():
            if r["cost_cur"] < 25 and r["cost_prv"] < 25:
                continue
            sub = cur[cur["carrier"] == car]
            v_, s_ = iw_qs(sub)
            low = sub[sub["bucket"] == "QS<=3"]["clicks"].sum()
            lowshare = low / sub["clicks"].sum() if sub["clicks"].sum() else 0
            dc = ((r["cpc_cur"] - r["cpc_prv"])
                  if not (np.isnan(r["cpc_cur"]) or np.isnan(r["cpc_prv"]))
                  else np.nan)
            out.append(
                f"  {str(car):<18}{r['clicks_prv']:>5,.0f}->"
                f"{r['clicks_cur']:<6,.0f}"
                f"${(0 if np.isnan(r['cpc_prv']) else r['cpc_prv']):>5.2f}->"
                f"${(0 if np.isnan(r['cpc_cur']) else r['cpc_cur']):<6.2f}"
                f"{('%+5.2f' % dc) if not np.isnan(dc) else '   --':>7}"
                f"  ${r['within']:>+5.2f} ${r['mix']:>+5.2f} "
                f"${r['cross']:>+5.2f} ${r['contrib']:>+5.2f} | "
                + (f"{v_:>5.2f}" if not np.isnan(v_) else "   --")
                + f" {s_:>5.0%}  {lowshare:>5.0%}")
        out.append("  (contrib sums to dCPC across carriers; within = that "
                   "carrier repricing, mix = click share")
        out.append("   moving toward/away from it; iw_QS/rated_share/QS<=3 "
                   "share are this-week, click-weighted cells)")

        out.append("\nCPC-BY-QS CURVE, this week (within match type — "
                   "iw_QS with rated_share):")
        for cell in idx:
            r = m.loc[cell]
            if r["clicks_cur"] <= 0 or np.isnan(r["cpc_cur"]):
                continue
            sub = cur[(cur["bucket"] == cell[0])
                      & (cur["match_type"] == cell[1])]
            v_, s_ = iw_qs(sub)
            out.append(f"  {cell[0]:<8} {cell[1]:<7} CPC ${r['cpc_cur']:>5.2f}"
                       f"  ({r['cpc_cur'] / cpc_c - 1:+.0%} vs blended)  "
                       f"{r['clicks_cur']:>6,.0f} clicks  "
                       + (f"iw_QS {v_:.2f} (rated {s_:.0%})"
                          if not np.isnan(v_) else "unrated"))
        out.append("\ncaveats: 'unrated' = no keyword_quality_daily row that "
                   "day (rated coverage collapsed ~130K->~9K kw-days/mo "
                   "across the consolidation, so unrated is a real cohort, "
                   "not noise); QS misses search-partner/close-variant "
                   "pricing; comparable with the QS thread's band economics "
                   "since both use the canonical iw_QS.")
        report = "\n".join(out) + "\n"
        print(report)
        if not args.no_save:
            try:
                OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
                path = OUTPUT_DIR / f"cpc_qs_bridge_{date.today().isoformat()}.txt"
                path.write_text(report, encoding="utf-8")
                print(f"saved -> {path}")
            except Exception as e:
                print(f"(could not save: {e})", file=sys.stderr)
    finally:
        con.close()


if __name__ == "__main__":
    main()
