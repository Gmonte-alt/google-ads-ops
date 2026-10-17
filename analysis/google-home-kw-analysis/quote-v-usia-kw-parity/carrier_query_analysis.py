#!/usr/bin/env python3
"""Search-query + keyword analysis for a carrier's ad groups (default: farmers), built for negative-list
decisions. Overall and per-campaign, query grain and keyword grain.

Scope: queries served against keywords containing --term (i.e., the carrier's ad groups' traffic),
account_id=1, platform='google', via search_query_performance -> keyword_instances (instance_id) and
campaigns (stored-key joins per house rule).

Outputs (--out-dir):
  q_overall__<term>.csv        query grain: funnel + GP + intent flag, across all campaigns
  q_by_campaign__<term>.csv    query x campaign: where each query bleeds or earns
  kw_overall__<term>.csv       keyword grain: which keywords/match types attract the traffic
  kw_by_campaign__<term>.csv   keyword x campaign
  negative_candidates__<term>.csv  rule-based candidates with suggested scope + reason
  routing__<term>.csv          query x SERVING keyword: which keyword captured each query, with per-pair
                               economics + a cannibalized flag (query names a product the serving keyword
                               lacks, e.g. 'farmers home insurance' served by bare 'farmers insurance')

Negative rules (tunable): cost >= --cost-floor with zero leads, or GP < --gp-floor (negative) with leads.
Intent flags mark the classic conquest leaks: SERVICE (login/claims/phone/pay), OTHER_PRODUCT (auto/life/
renters...), AGENT (agent/agency/near me), NON_TERM (query lacks the carrier token = broad leakage),
OTHER_CARRIER (another registry brand present). PURCHASE-intent queries are never suggested as negatives
regardless of window performance -- flag them for bid/copy work instead.

Notes: Search campaigns only (no PMax); sub-threshold queries absent (Google privacy thresholds);
device handling auto-detected and printed.

Run:
  python carrier_query_analysis.py --db C:\\Projects\\google-ads-ops\\db\\paid_search.db
  python carrier_query_analysis.py --db ... --term farmers --start 2026-05-01 --end 2026-08-10
"""
import argparse
import csv
import os
import re
import sqlite3
from collections import defaultdict

ACCOUNT_ID = 1
DEFAULT_OUT = r"C:\Projects\google-ads-ops\analysis\google-home-kw-analysis\farmers-kw-review\output"

SERVICE = re.compile(r"\b(login|log in|sign in|claim|claims|phone|number|customer service|pay|payment|"
                     r"bill|billing|cancel|cancellation|roadside|id card|app|careers|jobs|address)\b")
OTHER_PRODUCT = re.compile(r"\b(auto|car|vehicle|life|renters|rental|pet|business|commercial|flood|"
                           r"umbrella|motorcycle|boat|rv)\b")
AGENT = re.compile(r"\b(agent|agents|agency|agencies|broker|near me|office|local)\b")
PURCHASE = re.compile(r"\b(quote|quotes|rate|rates|cost|price|pricing|cheap|compare|comparison|"
                      r"discount|estimate|coverage|policy|insurance)\b")


def load_brands(brand_csv, term):
    toks = set()
    if brand_csv and os.path.exists(brand_csv):
        for r in csv.DictReader(open(brand_csv, encoding="utf-8-sig")):
            v = re.sub(r"[^a-z0-9 ]", "", (r.get("normalized_name") or r.get("brand_name") or "").lower()).strip()
            if len(v) >= 4 and term not in v:
                toks.add(v)
    return toks


def intent(q, term, other_brands):
    has_term = term in q
    if not has_term:
        for b in other_brands:
            if b in q:
                return "OTHER_CARRIER"
        return "NON_TERM_LEAK"
    if SERVICE.search(q):
        return "SERVICE"
    if OTHER_PRODUCT.search(q):
        return "OTHER_PRODUCT"
    if AGENT.search(q):
        return "AGENT"
    if PURCHASE.search(q):
        return "PURCHASE"
    return "BRAND_BARE"


def device_clause(conn):
    devs = [r[0] for r in conn.execute(
        "SELECT DISTINCT device FROM search_query_performance LIMIT 10")]
    if devs and "ALL" in devs and len(devs) > 1:
        print("device values %s -> filtering device='ALL' (authoritative rows)" % devs)
        return " AND p.device = 'ALL' "
    print("device values %s -> no ALL/per-device split detected; no device filter" % devs)
    return ""


def main():
    ap = argparse.ArgumentParser(description="Carrier ad-group query + keyword analysis for negatives")
    ap.add_argument("--db", required=True)
    ap.add_argument("--term", default="farmers")
    ap.add_argument("--start", default="2026-05-01")
    ap.add_argument("--end", default="2026-12-31")
    ap.add_argument("--brand-csv", default=r"C:\Projects\google-ads-ops\brand_registry_export.csv")
    ap.add_argument("--cost-floor", type=float, default=50.0)
    ap.add_argument("--gp-floor", type=float, default=0.0)
    ap.add_argument("--out-dir", default=DEFAULT_OUT)
    a = ap.parse_args()
    term = a.term.strip().lower()
    conn = sqlite3.connect("file:%s?mode=ro" % a.db, uri=True)
    os.makedirs(a.out_dir, exist_ok=True)
    other_brands = load_brands(a.brand_csv, term)
    dc = device_clause(conn)
    maxd = conn.execute("SELECT MAX(report_date) FROM search_query_performance WHERE platform='google'").fetchone()[0]
    print("data through %s | window %s..%s | term %r | scope: queries matched to %%%s%% keywords"
          % (maxd, a.start, a.end, term, term))

    q = ("SELECT LOWER(TRIM(sq.query_text)) qtext, LOWER(TRIM(ki.keyword_text)) ktext, "
         " UPPER(ki.match_type) mt, COALESCE(p.match_type_served,'') mts, c.campaign_name camp, "
         " SUM(p.impressions) impr, SUM(p.clicks) clicks, SUM(p.cost_micros)/1000000.0 cost, "
         " SUM(p.secondary_conv_count) zips, SUM(p.primary_conv_count) leads, SUM(p.primary_conv_value) val "
         " FROM search_query_performance p "
         " JOIN search_queries sq ON sq.id = p.query_id "
         " JOIN keyword_instances ki ON ki.id = p.instance_id AND ki.account_id = ? "
         " JOIN campaigns c ON c.campaign_id = p.campaign_id AND c.account_id = ? "
         " WHERE p.platform='google' AND p.report_date >= ? AND p.report_date <= ? "
         " AND LOWER(ki.keyword_text) LIKE ? " + dc +
         " GROUP BY 1,2,3,4,5")
    rows = conn.execute(q, (ACCOUNT_ID, ACCOUNT_ID, a.start, a.end, "%" + term + "%")).fetchall()
    print("raw rows (query x keyword x campaign): %d" % len(rows))
    if not rows:
        raise SystemExit("No rows -- check term/window. Keywords LIKE %%%s%% in window may not exist." % term)

    def agg_into(d, key, impr, clicks, cost, zips, leads, val):
        x = d[key]
        x["impr"] += impr or 0; x["clicks"] += clicks or 0; x["cost"] += cost or 0
        x["zips"] += zips or 0; x["leads"] += leads or 0; x["val"] += val or 0

    def newd():
        return defaultdict(lambda: dict(impr=0, clicks=0, cost=0.0, zips=0.0, leads=0.0, val=0.0))

    q_all, q_camp, k_all, k_camp = newd(), newd(), newd(), newd()
    camps_per_query = defaultdict(set)
    for qt, kt, mt, mts, camp, impr, clicks, cost, zips, leads, val in rows:
        agg_into(q_all, qt, impr, clicks, cost, zips, leads, val)
        agg_into(q_camp, (qt, camp), impr, clicks, cost, zips, leads, val)
        agg_into(k_all, (kt, mt), impr, clicks, cost, zips, leads, val)
        agg_into(k_camp, (kt, mt, camp), impr, clicks, cost, zips, leads, val)
        if (cost or 0) > 0:
            camps_per_query[qt].add(camp)

    def metrics(x):
        gp = x["val"] - x["cost"]
        return [x["impr"], x["clicks"], round(x["cost"], 2), round(x["zips"], 1), round(x["leads"], 1),
                round(x["val"], 2), round(gp, 2),
                round(x["val"] / x["cost"], 2) if x["cost"] else 0,
                round(x["cost"] / x["leads"], 2) if x["leads"] else ""]

    MH = ["impr", "clicks", "cost", "zips", "leads", "value", "GP", "ROAS", "CPL"]

    def write(name, header, data):
        p = os.path.join(a.out_dir, "%s__%s.csv" % (name, term))
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f); w.writerow(header); w.writerows(data)
        print("  wrote %s (%d rows)" % (os.path.basename(p), len(data)))

    # query overall (with intent)
    out = []
    for qt, x in sorted(q_all.items(), key=lambda kv: kv[1]["val"] - kv[1]["cost"]):
        out.append([qt, intent(qt, term, other_brands), len(camps_per_query.get(qt, []))] + metrics(x))
    write("q_overall", ["query", "intent", "campaigns_with_cost"] + MH, out)

    # query x campaign
    out = []
    for (qt, camp), x in sorted(q_camp.items(), key=lambda kv: kv[1]["val"] - kv[1]["cost"]):
        out.append([qt, camp, intent(qt, term, other_brands)] + metrics(x))
    write("q_by_campaign", ["query", "campaign", "intent"] + MH, out)

    # keyword overall / by campaign
    out = [[kt, mt] + metrics(x) for (kt, mt), x in
           sorted(k_all.items(), key=lambda kv: kv[1]["val"] - kv[1]["cost"])]
    write("kw_overall", ["keyword", "match_type"] + MH, out)
    out = [[kt, mt, camp] + metrics(x) for (kt, mt, camp), x in
           sorted(k_camp.items(), key=lambda kv: kv[1]["val"] - kv[1]["cost"])]
    write("kw_by_campaign", ["keyword", "match_type", "campaign"] + MH, out)

    # routing: query x serving keyword (aggregated across campaigns)
    r_pair = newd()
    kw_of_pair = {}
    for qt, kt, mt, mts, camp, impr, clicks, cost, zips, leads, val in rows:
        agg_into(r_pair, (qt, kt, mt), impr, clicks, cost, zips, leads, val)
    PRODUCT_TOKENS = ("home", "homeowner", "homeowners", "house", "renters", "auto", "condo", "life")
    out = []
    for (qt, kt, mt), x in r_pair.items():
        q_products = [t for t in PRODUCT_TOKENS if re.search(r"\b%s" % t, qt)]
        k_has = any(re.search(r"\b%s" % t, kt) for t in q_products) if q_products else True
        cannib = "yes" if (q_products and not k_has) else ""
        out.append([qt, kt, mt, cannib] + metrics(x))
    out.sort(key=lambda r: (r[3] != "yes", r[10]))  # cannibalized first, worst GP first
    write("routing", ["query", "serving_keyword", "match_type", "cannibalized"] + MH, out)
    can = [r for r in out if r[3] == "yes"]
    can_cost = sum(r[6] for r in can); can_gp = sum(r[10] for r in can)
    can_leads = sum(r[8] for r in can)
    print("\nrouting: %d query-keyword pairs | CANNIBALIZED: %d pairs, cost %.0f, leads %.0f, GP %.0f, CPL %s"
          % (len(out), len(can), can_cost, can_leads, can_gp,
             ("%.2f" % (can_cost / can_leads)) if can_leads else "n/a"))
    print("top 10 cannibalized pairs by GP drag:")
    for r in can[:10]:
        print("  %-38s <- %-28s %-6s cost=%-7s GP=%s" % (r[0][:38], r[1][:28], r[2], r[6], r[10]))

    # negative candidates
    cand = []
    for qt, x in q_all.items():
        it = intent(qt, term, other_brands)
        gp = x["val"] - x["cost"]
        zero_lead = x["cost"] >= a.cost_floor and x["leads"] < 1
        gp_neg = x["cost"] >= a.cost_floor and gp < a.gp_floor
        if not (zero_lead or gp_neg):
            continue
        if it == "PURCHASE":
            continue  # never negate purchase intent on performance alone -- that's bid/copy work
        ncamps = len(camps_per_query.get(qt, []))
        scope = "campaign_or_shared_list" if ncamps >= 3 else "ad_group"
        reason = "zero_leads" if zero_lead else "gp_negative"
        cand.append([qt, it, reason, scope, ncamps] + metrics(x))
    cand.sort(key=lambda r: r[11])  # GP ascending (worst drag first)
    write("negative_candidates", ["query", "intent", "reason", "suggested_scope", "campaigns_with_cost"] + MH, cand)

    tot_gp = sum(r[11] for r in cand)
    tot_cost = sum(r[7] for r in cand)
    print("\nnegative candidates: %d queries | combined cost %.0f | combined GP %.0f" % (len(cand), tot_cost, tot_gp))
    print("top 12 by GP drag:")
    for r in cand[:12]:
        print("  %-44s %-14s %-11s cost=%-8s GP=%-8s -> %s" % (r[0][:44], r[1], r[2], r[7], r[11], r[3]))
    conn.close()
    print("\nwrote -> %s" % os.path.abspath(a.out_dir))


if __name__ == "__main__":
    main()
