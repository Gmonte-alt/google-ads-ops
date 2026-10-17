#!/usr/bin/env python3
"""
review_negation_check.py — should carrier review/research traffic truly be negated?

Adjudicates the negation with history instead of a 2-day sample: monthly
performance of the REVIEW intent class over the trailing N months, split by
carrier brand, against the QUOTE class as the benchmark.

Pinned source (per DB architect):
  search_query_performance p JOIN search_queries sq ON sq.id = p.query_id
  WHERE p.platform='google' AND p.device='ALL' [AND p.account_id=1 if present]
Classification: house tagger columns (intent_action). Brand: tagger's brand
column on search_queries if present, else regex against the carrier list.

Decision framing printed with the output:
  NEGATE a brand's review traffic if, over the trailing window, its GP is
  negative AND its CvR sits well below the quote-class benchmark with enough
  clicks to mean it (>=80). PRESERVE (or exact-only fence) any brand whose
  review traffic is GP-positive. Suppression => dollars are lower bounds,
  but CvR among reported rows is the best available estimate.

Usage: python review_negation_check.py [--db PATH] [--months 12] [--no-save]
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
MIN_CLICKS_FOR_VERDICT = 80

DATE_CANDS = ["report_date", "date", "day"]
COST_CANDS = ["cost", "cost_micros", "spend"]
LEAD_CANDS = ["primary_conv_count", "primary_conversions", "conversions"]
REV_CANDS = ["primary_conv_value", "conversion_value"]
TERM_CANDS = ["search_term", "query", "search_query", "query_text", "text",
              "term"]
BRAND_CANDS = ["brand", "brand_name", "carrier_brand", "matched_brand"]

CARRIERS = ["Geico", "Farmers", "Hartford", "Kin", "Travelers", "Amica",
            "AAA", "State Farm", "Allstate", "Progressive", "American Family",
            "Plymouth Rock", "NerdWallet", "Policygenius", "Bamboo", "Costco",
            "USAA", "Liberty Mutual", "Nationwide", "Lemonade", "Hippo",
            "AARP", "Matic", "Savemax", "Vermont Mutual", "Safeco", "Erie",
            "Mercury", "Chubb"]
CARRIER_RX = [(c, re.compile(re.escape(c.lower()).replace(r"\ ", r"\s*"),
                             re.I)) for c in CARRIERS]

REVIEW_RX = re.compile(r"\b(?:reviews?|complaints?|legit|scam|ratings?|"
                       r"am best|bbb|consumer reports)\b", re.I)


def cols(con, t):
    try:
        return [r[1] for r in con.execute(f"PRAGMA table_info({t})").fetchall()]
    except sqlite3.Error:
        return []


def first(cands, avail):
    for c in cands:
        if c in avail:
            return c
    return None


def money(col):
    return f"{col} / 1000000.0" if col.endswith("_micros") else col


def brand_of(term):
    for name, rx in CARRIER_RX:
        if rx.search(term):
            return name
    return "(no carrier)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db")
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()
    db = Path(args.db) if args.db else DB_PATH
    if not db.exists():
        sys.exit(f"DB not found at {db}")
    con = sqlite3.connect(db)
    out = ["=" * 78,
           f"REVIEW-CLASS NEGATION CHECK — account {ACCOUNT_ID} — trailing "
           f"{args.months} months — run {date.today().isoformat()}",
           "=" * 78]
    try:
        p = cols(con, "search_query_performance")
        sq = cols(con, "search_queries")
        if not p or not sq:
            sys.exit("Pinned tables not found.")
        dcol = first(DATE_CANDS, p)
        ccol = first(COST_CANDS, p)
        lead = first(LEAD_CANDS, p)
        rev = first(REV_CANDS, p)
        term = first(TERM_CANDS, sq)
        bcol = first(BRAND_CANDS, sq)
        ia = "intent_action" if "intent_action" in sq else None
        if not (dcol and term and "query_id" in p):
            sys.exit("Expected columns missing on pinned tables.")
        filt = ["1=1"]
        if "platform" in p:
            filt.append("p.platform = 'google'")
        if "device" in p:
            filt.append("p.device = 'ALL'")
        if "account_id" in p:
            filt.append(f"p.account_id = {ACCOUNT_ID}")
        min_date = (date.today()
                    - timedelta(days=args.months * 31)).isoformat()
        sel = (f"SELECT substr(p.{dcol},1,7) AS ym, sq.{term} AS term, "
               f"SUM(p.impressions) AS impressions, SUM(p.clicks) AS clicks")
        grp = f" GROUP BY 1, sq.{term}"
        if ia:
            sel += f", sq.{ia} AS intent_action"
            grp += f", sq.{ia}"
        if bcol:
            sel += f", sq.{bcol} AS brand"
            grp += f", sq.{bcol}"
        if ccol:
            sel += f", SUM({money('p.' + ccol) if ccol.endswith('_micros') else 'p.' + ccol}) AS cost"
        if lead:
            sel += f", SUM(p.{lead}) AS leads"
        if rev:
            sel += f", SUM({money('p.' + rev) if rev.endswith('_micros') else 'p.' + rev}) AS revenue"
        df = pd.read_sql_query(
            sel + " FROM search_query_performance p "
            "JOIN search_queries sq ON sq.id = p.query_id "
            f"WHERE p.{dcol} >= '{min_date}' AND " + " AND ".join(filt) + grp,
            con)
        out.append(f"source: search_query_performance JOIN search_queries "
                   f"({', '.join(filt[1:])}) [pinned]")
        if df.empty:
            sys.exit("No rows in window.")
        df["term"] = df["term"].astype(str).str.lower().str.strip()
        for c in ["cost", "leads", "revenue"]:
            if c not in df:
                df[c] = 0.0
        df["gp"] = df["revenue"] - df["cost"]
        # class flags
        if ia:
            df["is_review"] = (df["intent_action"].astype(str)
                               .str.lower() == "review")
            df["is_quote"] = (df["intent_action"].astype(str)
                              .str.lower() == "quote")
            out.append("class: house tagger intent_action ('review' vs "
                       "'quote' benchmark); regex adds terms the tagger "
                       "missed" )
            df.loc[~df["is_review"] & df["term"].str.contains(REVIEW_RX),
                   "is_review"] = True
        else:
            df["is_review"] = df["term"].str.contains(REVIEW_RX)
            df["is_quote"] = ~df["is_review"]
            out.append("class: FALLBACK regex (tagger columns not found); "
                       "'quote' benchmark = all non-review")
        # brand
        if bcol and df.get("brand") is not None and df["brand"].notna().any():
            df["carrier"] = (df["brand"].astype(str)
                             .replace({"nan": "", "None": ""}))
            df.loc[df["carrier"] == "", "carrier"] = df.loc[
                df["carrier"] == "", "term"].map(brand_of)
            out.append(f"brand: tagger column '{bcol}' (regex fill for "
                       "untagged)")
        else:
            df["carrier"] = df["term"].map(brand_of)
            out.append("brand: regex against carrier list")

        rv = df[df["is_review"]]
        qt = df[df["is_quote"]]

        def stats(d):
            clk, cst = d["clicks"].sum(), d["cost"].sum()
            lds, rvn = d["leads"].sum(), d["revenue"].sum()
            return dict(clicks=clk, cost=cst, leads=lds, revenue=rvn,
                        gp=rvn - cst,
                        cvr=lds / clk if clk else np.nan,
                        cpl=cst / lds if lds else np.nan,
                        rpl=rvn / lds if lds else np.nan,
                        cpc=cst / clk if clk else np.nan)

        R, Q = stats(rv), stats(qt)
        out.append(f"\nTRAILING {args.months}-MONTH TOTALS "
                   "(dollars are LOWER BOUNDS — suppression):")
        out.append("  class    clicks     cost    leads  revenue       GP"
                   "     CvR     CPL     RPL    CPC")
        for name, s in (("REVIEW", R), ("QUOTE", Q)):
            out.append(f"  {name:<7}{s['clicks']:>8,.0f} ${s['cost']:>7,.0f}"
                       f"{s['leads']:>8,.1f} ${s['revenue']:>7,.0f} "
                       f"${s['gp']:>+7,.0f}{s['cvr']:>8.1%}"
                       f"{('$%.0f' % s['cpl']) if s['leads'] else '   --':>8}"
                       f"{('$%.0f' % s['rpl']) if s['leads'] else '   --':>8}"
                       f"  ${s['cpc']:.2f}")

        out.append("\nREVIEW CLASS BY MONTH:")
        m = rv.groupby("ym").agg(clicks=("clicks", "sum"),
                                 cost=("cost", "sum"),
                                 leads=("leads", "sum"),
                                 revenue=("revenue", "sum"))
        m["gp"] = m["revenue"] - m["cost"]
        m["cvr"] = m["leads"] / m["clicks"]
        for ym, r in m.sort_index().iterrows():
            out.append(f"  {ym}  clicks {r['clicks']:>5,.0f}  cost "
                       f"${r['cost']:>6,.0f}  leads {r['leads']:>5,.1f}  "
                       f"GP ${r['gp']:>+7,.0f}  CvR {r['cvr']:.1%}")

        out.append(f"\nREVIEW CLASS BY CARRIER (trailing {args.months}mo, "
                   "sorted by cost):")
        b = rv.groupby("carrier").agg(clicks=("clicks", "sum"),
                                      cost=("cost", "sum"),
                                      leads=("leads", "sum"),
                                      revenue=("revenue", "sum"))
        b["gp"] = b["revenue"] - b["cost"]
        b["cvr"] = b["leads"] / b["clicks"]
        bench = Q["cvr"]
        out.append("  carrier              clicks     cost   leads       GP"
                   "     CvR   verdict")
        for car, r in b.sort_values("cost", ascending=False).iterrows():
            if r["clicks"] >= MIN_CLICKS_FOR_VERDICT:
                if r["gp"] > 0:
                    v = "PRESERVE (GP+)"
                elif not np.isnan(bench) and r["cvr"] < 0.6 * bench:
                    v = "NEGATE"
                else:
                    v = "borderline"
            else:
                v = f"thin (<{MIN_CLICKS_FOR_VERDICT} clicks)"
            out.append(f"  {str(car):<20}{r['clicks']:>7,.0f} "
                       f"${r['cost']:>7,.0f}{r['leads']:>8,.1f} "
                       f"${r['gp']:>+8,.0f}{r['cvr']:>8.1%}   {v}")
        out.append(f"\n  benchmark: quote-class CvR {bench:.1%}; NEGATE = "
                   f"GP<0 and CvR < 60% of benchmark on >={MIN_CLICKS_FOR_VERDICT} "
                   "clicks; PRESERVE = GP-positive regardless of CvR.")
        out.append("  reminders: dollars are lower bounds; a PRESERVE brand "
                   "can still be fenced to exact rather than left on broad; "
                   "verdicts are per-brand so negate as '<brand> + review "
                   "modifiers', not blanket 'reviews'." )
        report = "\n".join(out) + "\n"
        print(report)
        if not args.no_save:
            try:
                OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
                path = OUTPUT_DIR / (f"review_negation_check_"
                                     f"{date.today().isoformat()}.txt")
                path.write_text(report, encoding="utf-8")
                print(f"saved -> {path}")
            except Exception as e:
                print(f"(could not save: {e})", file=sys.stderr)
    finally:
        con.close()


if __name__ == "__main__":
    main()
