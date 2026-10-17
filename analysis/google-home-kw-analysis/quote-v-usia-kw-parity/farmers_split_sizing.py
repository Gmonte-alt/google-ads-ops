#!/usr/bin/env python3
"""Size the Core vs Flagship farmers split per campaign: daily impressions, clicks,
cost, leads over the last 90 days, plus conv/30d vs the ~15 Smart Bidding comfort floor.

Buckets (by serving keyword text):
  FLAGSHIP = farmers/farmer brand terms WITHOUT a product token (home/homeowner/house/auto)
  HOME     = farmers brand terms WITH a home-product token
Note: volumes come from search_query_performance, which excludes sub-threshold queries --
impressions are therefore a floor, not a census (conversions are near-complete).

Run:
  python farmers_split_sizing.py --db C:\\Projects\\google-ads-ops\\db\\paid_search.db
"""
import argparse, re, sqlite3
from collections import defaultdict

ACCOUNT_ID = 1
PRODUCT = re.compile(r"\b(home|homeowner|homeowners|house)\b")

def bucket(kw):
    k = kw.lower()
    if "farmer" not in k:
        return None
    return "HOME" if PRODUCT.search(k) else "FLAGSHIP"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--days", type=int, default=90)
    a = ap.parse_args()
    conn = sqlite3.connect("file:%s?mode=ro" % a.db, uri=True)
    q = ("SELECT c.campaign_name, LOWER(ki.keyword_text), "
         " SUM(p.impressions), SUM(p.clicks), SUM(p.cost_micros)/1000000.0, "
         " SUM(p.primary_conv_count) "
         " FROM search_query_performance p "
         " JOIN keyword_instances ki ON ki.id = p.instance_id AND ki.account_id = ? "
         " JOIN campaigns c ON c.campaign_id = p.campaign_id AND c.account_id = ? "
         " WHERE p.platform='google' AND p.report_date >= date('now', ?) "
         " AND LOWER(ki.keyword_text) LIKE '%farmer%' GROUP BY 1,2")
    agg = defaultdict(lambda: dict(impr=0, clicks=0, cost=0.0, leads=0.0))
    for camp, kw, impr, clicks, cost, leads in conn.execute(q, (ACCOUNT_ID, ACCOUNT_ID, "-%d day" % a.days)):
        b = bucket(kw)
        if not b:
            continue
        x = agg[(camp, b)]
        x["impr"] += impr or 0; x["clicks"] += clicks or 0
        x["cost"] += cost or 0; x["leads"] += leads or 0
    d = float(a.days)
    print("%-52s %-9s %9s %8s %8s %8s %9s %6s" % (
        "campaign", "family", "impr/day", "clk/day", "$/day", "lds/day", "conv/30d", "bid?"))
    for (camp, b), x in sorted(agg.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        c30 = x["leads"] / d * 30
        print("%-52s %-9s %9.1f %8.2f %8.2f %8.2f %9.1f %6s" % (
            camp[:52], b, x["impr"] / d, x["clicks"] / d, x["cost"] / d,
            x["leads"] / d, c30, "OK" if c30 >= 15 else "THIN"))
    tot = defaultdict(lambda: dict(leads=0.0, cost=0.0))
    for (camp, b), x in agg.items():
        tot[b]["leads"] += x["leads"]; tot[b]["cost"] += x["cost"]
    print("\naccount totals (/day): " + " | ".join(
        "%s %.1f leads, $%.0f" % (b, t["leads"] / d, t["cost"] / d) for b, t in sorted(tot.items())))
    conn.close()

if __name__ == "__main__":
    main()
