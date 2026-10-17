#!/usr/bin/env python3
"""Geo-modified query analysis for a carrier's ad groups over a long window (default 18 months).

Question: do city/state-modified queries (e.g. 'farmers insurance el paso') underperform enough to
justify a geo negative list on the carrier's core ad groups?

Method: pull all queries served against keywords containing --term (same joins/house rules as
carrier_query_analysis), strip the carrier+product tokens from each query, then match the RESIDUAL
tokens against a US city/state reference (us_cities_reference.csv: 39k cities incl. aliases, states,
metro abbreviations; insurance-vocab collisions stoplisted -- 'farmers branch' never matches).
Classes: LOCATOR (near me/office/agent -- handled by the existing negative list, reported separately),
CITY_STATE, CITY, STATE, ZIP, NON_GEO.

Outputs (--out-dir):
  geo_summary__<term>.csv      economics by geo class + monthly stability check
  city_persistence__<term>.csv one row per detected city: 18-month spend/leads/GP + verdict
                               (negate >= $--negate-cost and < --negate-leads leads; watch; keep)
  geo_negative_import__<term>.csv  Editor-ready negative rows for the 'negate' verdicts (exact match,
                               scoped to the campaign(s) where the city bled)
Run:
  python geo_query_analysis.py --db C:\\Projects\\google-ads-ops\\db\\paid_search.db --term farmers ^
      --cities C:\\Projects\\google-ads-ops\\analysis\\reference\\us_cities_reference.csv
"""
import argparse
import csv
import os
import re
import sqlite3
from collections import defaultdict

ACCOUNT_ID = 1
LOCATOR = re.compile(r"\b(near me|nearby|in my area|within \d+ mi|office|offices|agent|agents|agency|agencies|locations?)\b")
ZIP_RE = re.compile(r"\b\d{5}\b")
PRODUCT_TOKENS = {"insurance","ins","home","homeowner","homeowners","house","auto","car","renters",
                  "rental","condo","life","quote","quotes","rate","rates","cost","price","company",
                  "coverage","policy","cheap","best","compare","group","mutual","bureau","com","www",
                  "state","farm","farms"}


def load_ref(path):
    cities, states = {}, {}
    for r in csv.DictReader(open(path, encoding="utf-8")):
        if r["kind"] == "city":
            cities[r["token"]] = r["canonical"]
        elif r["kind"] in ("state", "state_abbr", "metro_abbr"):
            states[r["token"]] = r["canonical"]
    return cities, states


def classify(q, term, cities, states):
    if LOCATOR.search(q):
        return "LOCATOR", ""
    if ZIP_RE.search(q):
        return "ZIP", ZIP_RE.search(q).group(0)
    # residual = query minus carrier + product vocabulary
    toks = [t for t in re.findall(r"[a-z]+", q) if t not in PRODUCT_TOKENS and term not in t]
    if not toks:
        return "NON_GEO", ""
    resid = " ".join(toks)
    hit_state = next((states[t] for t in toks if t in states), "")
    # longest city match within residual (multi-word first)
    hit_city = ""
    for n in (3, 2, 1):
        for i in range(len(toks) - n + 1):
            cand = " ".join(toks[i:i + n])
            if cand in cities:
                hit_city = cities[cand]
                break
        if hit_city:
            break
    if hit_city and hit_state:
        return "CITY_STATE", "%s, %s" % (hit_city, hit_state)
    if hit_city:
        return "CITY", hit_city
    if hit_state:
        return "STATE", hit_state
    return "NON_GEO", ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--term", default="farmers")
    ap.add_argument("--start", default="2025-02-01")
    ap.add_argument("--end", default="2026-12-31")
    ap.add_argument("--cities", default="us_cities_reference.csv")
    ap.add_argument("--negate-cost", type=float, default=100.0)
    ap.add_argument("--negate-leads", type=float, default=1.0)
    ap.add_argument("--out-dir", default=r"C:\Projects\google-ads-ops\analysis\google-home-kw-analysis\farmers-kw-review\geo_output")
    a = ap.parse_args()
    term = a.term.strip().lower()
    cities, states = load_ref(a.cities)
    print("reference: %d city tokens, %d state/abbr tokens" % (len(cities), len(states)))
    conn = sqlite3.connect("file:%s?mode=ro" % a.db, uri=True)
    os.makedirs(a.out_dir, exist_ok=True)

    q = ("SELECT LOWER(TRIM(sq.query_text)) qt, c.campaign_name camp, SUBSTR(p.report_date,1,7) ym, "
         " SUM(p.clicks), SUM(p.cost_micros)/1000000.0, SUM(p.primary_conv_count), SUM(p.primary_conv_value) "
         " FROM search_query_performance p "
         " JOIN search_queries sq ON sq.id = p.query_id "
         " JOIN keyword_instances ki ON ki.id = p.instance_id AND ki.account_id = ? "
         " JOIN campaigns c ON c.campaign_id = p.campaign_id AND c.account_id = ? "
         " WHERE p.platform='google' AND p.report_date >= ? AND p.report_date <= ? "
         " AND LOWER(ki.keyword_text) LIKE ? GROUP BY 1,2,3")
    cls_agg = defaultdict(lambda: dict(clicks=0, cost=0.0, leads=0.0, val=0.0, queries=set()))
    city_agg = defaultdict(lambda: dict(cost=0.0, leads=0.0, val=0.0, camps=defaultdict(float), months=set()))
    n = 0
    for qt, camp, ym, clicks, cost, leads, val in conn.execute(q, (ACCOUNT_ID, ACCOUNT_ID, a.start, a.end, "%" + term + "%")):
        n += 1
        cl, geo = classify(qt, term, cities, states)
        x = cls_agg[cl]
        x["clicks"] += clicks or 0; x["cost"] += cost or 0; x["leads"] += leads or 0; x["val"] += val or 0
        x["queries"].add(qt)
        if cl in ("CITY", "CITY_STATE") and geo:
            g = city_agg[geo.split(",")[0]]  # aggregate by city; CITY and CITY_STATE merge
            g["cost"] += cost or 0; g["leads"] += leads or 0; g["val"] += val or 0
            g["camps"][camp] += cost or 0; g["months"].add(ym)
    print("rows: %d | window %s..%s" % (n, a.start, a.end))

    with open(os.path.join(a.out_dir, "geo_summary__%s.csv" % term), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["geo_class", "distinct_queries", "clicks", "cost", "leads", "value", "GP", "ROAS", "CPL"])
        print("\n%-11s %8s %9s %8s %9s %7s %8s" % ("class", "queries", "cost", "leads", "GP", "ROAS", "CPL"))
        for cl, x in sorted(cls_agg.items(), key=lambda kv: -kv[1]["cost"]):
            gp = x["val"] - x["cost"]
            roas = x["val"] / x["cost"] if x["cost"] else 0
            cpl = x["cost"] / x["leads"] if x["leads"] else 0
            w.writerow([cl, len(x["queries"]), x["clicks"], round(x["cost"]), round(x["leads"], 1),
                        round(x["val"]), round(gp), round(roas, 2), round(cpl, 2)])
            print("%-11s %8d %9.0f %8.1f %9.0f %6.2fx %8.2f" % (cl, len(x["queries"]), x["cost"], x["leads"], gp, roas, cpl))

    rows, neg = [], []
    for geo, g in sorted(city_agg.items(), key=lambda kv: -(kv[1]["cost"])):
        gp = g["val"] - g["cost"]
        if g["cost"] >= a.negate_cost and g["leads"] < a.negate_leads:
            verdict = "negate"
        elif g["cost"] >= a.negate_cost / 2 and gp < 0:
            verdict = "watch"
        else:
            verdict = "keep"
        top_camp = max(g["camps"], key=g["camps"].get) if g["camps"] else ""
        rows.append([geo, round(g["cost"], 2), round(g["leads"], 1), round(g["val"], 2), round(gp, 2),
                     len(g["months"]), verdict, top_camp])
        if verdict == "negate":
            neg.append([top_camp, geo.split(",")[0], "Exact"])
    with open(os.path.join(a.out_dir, "city_persistence__%s.csv" % term), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["city", "cost", "leads", "value", "GP", "active_months", "verdict", "top_campaign"])
        w.writerows(rows)
    with open(os.path.join(a.out_dir, "geo_negative_import__%s.csv" % term), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Campaign", "Negative keyword", "Match type"])
        # note: negative text is '<term> insurance <city>' pattern queries; import as exact query negatives
        for camp, city, mt in neg:
            w.writerow([camp, "%s insurance %s" % (term, city), mt])
    nn = len(neg)
    print("\ncities detected: %d | negate: %d | watch: %d | keep: %d" % (
        len(rows), nn, sum(1 for r in rows if r[6] == "watch"), sum(1 for r in rows if r[6] == "keep")))
    print("wrote -> %s" % os.path.abspath(a.out_dir))
    conn.close()


if __name__ == "__main__":
    main()
