#!/usr/bin/env python3
"""
expansion_class_sizing.py — size the widening in dollars, by query class

Question: where did the extra search-term spend go, by intent class?
WoW comparison (this partial week Mon..latest vs same days last week) of
impressions / clicks / cost grouped by query class, plus the top terms by
cost growth inside the negative-leaning classes = the thematic negatives
shortlist, sized.

Pinned source (per DB architect):
  search_query_performance p JOIN search_queries sq ON sq.id = p.query_id
  WHERE p.platform='google' AND p.device='ALL' [AND p.account_id=1 if present]

Classification: uses the house tagger's columns on search_queries
(intent_type / intent_action, script 08) when present — that's the
authoritative taxonomy. Falls back to a built-in regex classifier
(clearly labeled) only if those columns don't exist.

Suppression framing: sqp omits below-threshold terms, so all dollars here
are LOWER BOUNDS. The report prints sqp-vs-kpd cost coverage for the same
window so you know how much spend is visible at term grain.

Usage: python expansion_class_sizing.py [--db PATH] [--no-save]
"""

import argparse
import re
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
TOP_TERMS = 12

DATE_CANDS = ["report_date", "date", "day"]
COST_CANDS = ["cost", "cost_micros", "spend"]
LEAD_CANDS = ["primary_conv_count", "primary_conversions", "conversions"]
REV_CANDS = ["primary_conv_value", "conversion_value"]
TERM_CANDS = ["search_term", "query", "search_query", "query_text", "text",
              "term"]
INTENT_TYPE_CANDS = ["intent_type"]
INTENT_ACTION_CANDS = ["intent_action"]

# Fallback regex classifier — used ONLY if the tagger's columns are absent.
STATE_ABBR = ("al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks|ky|la|me|md|"
              "ma|mi|mn|ms|mo|mt|ne|nv|nh|nj|nm|ny|nc|nd|oh|ok|or|pa|ri|sc|"
              "sd|tn|tx|ut|vt|va|wa|wv|wi|wy")
FALLBACK_CLASSES = [
    ("research_reviews", re.compile(
        r"\b(reviews?|complaints?|legit|scam|ratings?|am best|bbb|"
        r"better business)\b", re.I)),
    ("navigate_local", re.compile(
        r"\b(agenc(y|ies)|login|log in|phone number|customer service|"
        r"claims number|office|agent)\b"
        r"|insurance\s+\w+\s+(" + STATE_ABBR + r")\b", re.I)),
    ("specialty_coverage", re.compile(
        r"\b(earthquake|flood|liquefaction|sinkhole|wildfire|landlord|"
        r"airbnb|vacant|manufactured|mobile home park|hoa)\b", re.I)),
    ("commercial_core", re.compile(
        r"\b(quotes?|cost|cheap(est)?|best|rates?|estimate|calculator|"
        r"compare|companies|coverage|policy|buy)\b", re.I)),
]


def cols(con, t):
    try:
        return [r[1] for r in con.execute(f"PRAGMA table_info({t})").fetchall()]
    except sqlite3.Error:
        return []


def first(cands, available):
    for c in cands:
        if c in available:
            return c
    return None


def money(col):
    return f"{col} / 1000000.0" if col.endswith("_micros") else col


def week_windows(latest):
    monday = latest - timedelta(days=latest.weekday())
    this_days = [monday + timedelta(days=i)
                 for i in range((latest - monday).days + 1)]
    return this_days, [d - timedelta(days=7) for d in this_days]


def fmt_days(days):
    return f"{days[0].month}/{days[0].day}-{days[-1].month}/{days[-1].day}"


def fallback_class(term):
    for name, rx in FALLBACK_CLASSES:
        if rx.search(term):
            return name
    return "other"


def kpd_cost(con, days):
    """Total account-1 search cost from the pinned keyword source, for the
    sqp-visibility coverage line."""
    p = cols(con, "keyword_performance_daily")
    k = cols(con, "keyword_instances")
    if not p or not k or "device" not in p or "account_id" not in k:
        return None
    dcol = first(DATE_CANDS, p)
    ccol = first(COST_CANDS, p)
    if not (dcol and ccol):
        return None
    ds = ",".join(f"'{d.isoformat()}'" for d in days)
    row = con.execute(
        f"SELECT SUM({money('p.' + ccol) if ccol.endswith('_micros') else 'p.' + ccol}) "
        f"FROM keyword_performance_daily p "
        f"JOIN keyword_instances k ON k.id = p.instance_id "
        f"WHERE k.account_id = {ACCOUNT_ID} AND p.device = 'ALL' "
        f"AND p.{dcol} IN ({ds})").fetchone()
    return row[0] or 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db")
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()
    db = Path(args.db) if args.db else DB_PATH
    if not db.exists():
        sys.exit(f"DB not found at {db}")
    con = sqlite3.connect(db)
    out = ["=" * 76,
           f"EXPANSION CLASS SIZING — account {ACCOUNT_ID} — run "
           f"{date.today().isoformat()}",
           "=" * 76]
    try:
        p = cols(con, "search_query_performance")
        sq = cols(con, "search_queries")
        if not p or not sq:
            sys.exit("Pinned tables search_query_performance / search_queries "
                     "not found.")
        dcol = first(DATE_CANDS, p)
        ccol = first(COST_CANDS, p)
        term = first(TERM_CANDS, sq)
        it = first(INTENT_TYPE_CANDS, sq)
        ia = first(INTENT_ACTION_CANDS, sq)
        lead = first(LEAD_CANDS, p)
        rev = first(REV_CANDS, p)
        if not (dcol and term and "query_id" in p and "id" in sq):
            sys.exit(f"Expected columns missing on pinned tables "
                     f"(date={dcol}, term={term}, query_id present="
                     f"{'query_id' in p}).")
        filt = ["1=1"]
        if "platform" in p:
            filt.append("p.platform = 'google'")
        if "device" in p:
            filt.append("p.device = 'ALL'")
        if "account_id" in p:
            filt.append(f"p.account_id = {ACCOUNT_ID}")
        min_date = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
        sel_extra = ""
        if it:
            sel_extra += f", sq.{it} AS intent_type"
        if ia:
            sel_extra += f", sq.{ia} AS intent_action"
        if ccol:
            cexpr = money("p." + ccol) if ccol.endswith("_micros") else "p." + ccol
            sel_extra += f", SUM({cexpr}) AS cost"
        if lead:
            sel_extra += f", SUM(p.{lead}) AS leads"
        if rev:
            rexpr = money("p." + rev) if rev.endswith("_micros") else "p." + rev
            sel_extra += f", SUM({rexpr}) AS revenue"
        grp_extra = (f", sq.{it}" if it else "") + (f", sq.{ia}" if ia else "")
        df = pd.read_sql_query(
            f"SELECT p.{dcol} AS date, sq.{term} AS term, "
            f"SUM(p.impressions) AS impressions, SUM(p.clicks) AS clicks"
            f"{sel_extra} "
            f"FROM search_query_performance p "
            f"JOIN search_queries sq ON sq.id = p.query_id "
            f"WHERE p.{dcol} >= '{min_date}' AND " + " AND ".join(filt) +
            f" GROUP BY p.{dcol}, sq.{term}{grp_extra}", con)
        out.append(f"source: search_query_performance JOIN search_queries "
                   f"({', '.join(filt[1:]) or 'no filters'}) [pinned]")
        if df.empty:
            sys.exit("Pinned query returned no rows in window.")
        df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
        df = df.dropna(subset=["date"])
        df["term"] = df["term"].astype(str).str.lower().str.strip()
        if "cost" not in df:
            df["cost"] = np.nan
            out.append("NOTE: no cost column on search_query_performance — "
                       "dollars unavailable at term grain; showing clicks. "
                       "Tell me the cost column name if one exists.")
        # classification
        if ia:
            df["qclass"] = (df["intent_action"].astype(str).str.lower()
                            .replace({"nan": "untagged", "none": "untagged",
                                      "": "untagged"}))
            if it:
                df.loc[df["qclass"] == "untagged", "qclass"] = (
                    df.loc[df["qclass"] == "untagged", "intent_type"]
                    .astype(str).str.lower()
                    .replace({"nan": "untagged", "none": "untagged"}))
            method = "house tagger (intent_action, script 08)"
        else:
            df["qclass"] = df["term"].map(fallback_class)
            method = "FALLBACK regex classifier (tagger columns not found)"
        out.append(f"classification: {method}")

        latest = df["date"].max()
        this_days, last_days = week_windows(latest)
        cur = df[df["date"].isin(this_days)]
        prv = df[df["date"].isin(last_days)]

        # coverage line: how much of true (kpd) spend is visible in sqp
        kpd_cur = kpd_cost(con, this_days)
        if kpd_cur and "cost" in df and df["cost"].notna().any():
            cov = cur["cost"].sum() / kpd_cur if kpd_cur else np.nan
            out.append(f"visibility: sqp shows ${cur['cost'].sum():,.0f} of "
                       f"${kpd_cur:,.0f} keyword-grain cost this week "
                       f"({cov:.0%}) — all class dollars below are LOWER "
                       f"BOUNDS; scale mentally by ~1/{cov:.0%}.")

        agg_cols = {"impressions": "sum", "clicks": "sum", "cost": "sum"}
        if "leads" in df:
            agg_cols["leads"] = "sum"
        if "revenue" in df:
            agg_cols["revenue"] = "sum"
        c = cur.groupby("qclass").agg(agg_cols)
        v = prv.groupby("qclass").agg(agg_cols)
        m = c.join(v, lsuffix="_cur", rsuffix="_prv", how="outer").fillna(0)
        m["cost_growth"] = m["cost_cur"] - m["cost_prv"]
        tot_growth = m["cost_growth"].sum()
        m = m.sort_values("cost_growth", ascending=False)

        out.append(f"\nWoW BY QUERY CLASS ({fmt_days(this_days)} vs "
                   f"{fmt_days(last_days)}, same partial window):")
        hdr = ("  " + "class".ljust(18) + "cost prev".rjust(10)
               + "cost cur".rjust(10) + "growth".rjust(9)
               + "% growth".rjust(9) + "clicks".rjust(12) + "impr".rjust(14))
        if "leads_cur" in m:
            hdr += "leads".rjust(9)
        out.append(hdr)
        for cls, r in m.iterrows():
            shr = r["cost_growth"] / tot_growth if tot_growth else 0
            line = ("  " + str(cls).ljust(18)
                    + f"${r['cost_prv']:>8,.0f}${r['cost_cur']:>8,.0f}"
                    + f"{r['cost_growth']:>+9,.0f}{shr:>9.0%}"
                    + f"{r['clicks_prv']:>5,.0f}->{r['clicks_cur']:<5,.0f}"
                    + f"{r['impressions_prv']:>6,.0f}->{r['impressions_cur']:<7,.0f}")
            if "leads_cur" in m:
                line += f"{r['leads_prv']:>3,.0f}->{r['leads_cur']:<4,.0f}"
            out.append(line)

        # negatives shortlist: top terms by cost growth in negative-leaning classes
        neg_classes = [c_ for c_ in m.index
                       if str(c_).lower() in
                       ("navigate", "navigate_local", "review",
                        "research_reviews", "answer", "checklist",
                        "coverage-lookup", "informational", "specialty_coverage")]
        if neg_classes:
            out.append("\nNEGATIVES SHORTLIST — top terms by cost growth in "
                       "negative-leaning classes:")
            tc = cur.groupby(["qclass", "term"]).agg(agg_cols)
            tp = prv.groupby(["qclass", "term"]).agg(agg_cols)
            tm = tc.join(tp, lsuffix="_cur", rsuffix="_prv",
                         how="outer").fillna(0)
            tm["cost_growth"] = tm["cost_cur"] - tm["cost_prv"]
            for cls in neg_classes:
                if cls not in tm.index.get_level_values(0):
                    continue
                sub = tm.loc[cls].sort_values("cost_growth",
                                              ascending=False).head(TOP_TERMS)
                sub = sub[sub["cost_growth"] > 0]
                if sub.empty:
                    continue
                out.append(f"\n  [{cls}]  (class growth "
                           f"${m.loc[cls, 'cost_growth']:+,.0f})")
                for t_, r in sub.iterrows():
                    out.append(f"    {r['cost_growth']:>+7,.1f}  "
                               f"(${r['cost_cur']:,.1f} cur, "
                               f"{r['clicks_cur']:.0f} clicks)  {t_}")
        out.append("\nreminders: dollars are lower bounds (suppression); "
                   "negate as THEMES/patterns, not just listed terms — the "
                   "suppressed tail shares the pattern. Leads column, if "
                   "shown, protects against negating anything that converts.")
        report = "\n".join(out) + "\n"
        print(report)
        if not args.no_save:
            try:
                OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
                path = OUTPUT_DIR / (f"expansion_class_sizing_"
                                     f"{date.today().isoformat()}.txt")
                path.write_text(report, encoding="utf-8")
                print(f"saved -> {path}")
            except Exception as e:
                print(f"(could not save: {e})", file=sys.stderr)
    finally:
        con.close()


if __name__ == "__main__":
    main()
