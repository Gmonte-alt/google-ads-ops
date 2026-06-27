#!/usr/bin/env python
"""Realized economics from existing Quote.com Home (account_id=1) to set USIA launch budgets
(expected CPC, CTR) and portfolio max-CPC caps (CvR, RPL = breakeven). Splits GENERIC vs BRAND
via search-query brand tags. Read-only, device='ALL', 90d. Mirrors brand_term_performance.py joins."""
import argparse, sqlite3

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--db", default=r"C:\Projects\google-ads-ops\db\paid_search.db")
    p.add_argument("--account", type=int, default=1)
    p.add_argument("--days", type=int, default=90)
    a=p.parse_args()
    c=sqlite3.connect(a.db)

    seg=("CASE WHEN (sq.modifier_brand IS NOT NULL AND TRIM(sq.modifier_brand)<>'') "
         "OR (sq.brand_category IS NOT NULL AND TRIM(sq.brand_category)<>'') "
         "THEN 'BRAND' ELSE 'GENERIC' END")
    q=f"""
    SELECT {seg} AS segment,
           SUM(p.impressions) imp, SUM(p.clicks) clk, SUM(p.cost_micros)/1e6 cost,
           SUM(p.primary_conv_count) conv, SUM(p.primary_conv_value) val
    FROM search_query_performance p
    JOIN search_queries sq ON sq.id = p.query_id
    WHERE p.device='ALL'
      AND p.campaign_id IN (SELECT DISTINCT campaign_id FROM keyword_instances WHERE account_id=?)
      AND p.report_date >= date('now', ?)
    GROUP BY 1"""
    rows=list(c.execute(q,(a.account, f"-{a.days} day")))
    c.close()

    def line(name,imp,clk,cost,conv,val):
        ctr=100*clk/imp if imp else 0; cpc=cost/clk if clk else 0
        cvr=100*conv/clk if clk else 0; rpl=val/conv if conv else 0
        print("%-8s %10d %9d %12.0f %9.0f %12.0f | %6.2f%% %6.2f %6.1f%% %8.2f"%(
            name,imp,clk,cost,conv,val,ctr,cpc,cvr,rpl))
    print("=== Quote.com Home (account %d) realized economics, last %dd, device=ALL ===\n"%(a.account,a.days))
    print("%-8s %10s %9s %12s %9s %12s | %7s %6s %7s %8s"%(
        "segment","impr","clicks","cost","conv","value","CTR","CPC","CvR","RPL"))
    tot=[0,0,0,0,0]
    for segn,imp,clk,cost,conv,val in sorted(rows):
        line(segn,imp,clk,cost,conv,val)
        for i,v in enumerate([imp,clk,cost,conv,val]): tot[i]+=v
    print("-"*86); line("TOTAL",*tot)
    print("\nbreakeven max-CPC cap = RPL x CvR (per segment).  expected CPC for budget = CPC column above.")

if __name__=="__main__":
    main()
