"""
microsoft_verify_search_table.py
================================
Confirm campaign_performance_daily is the SEARCH counterpart to the audience-only
campaign_performance_network_daily, and that search + audience reconciles to the
geo table's total (both networks).

Answers:
  1. campaign_performance_daily columns (network dim? conv/revenue col names?)
  2. Its network composition (should be search networks only, no 'Audience')
  3. Monthly revenue: search table vs audience table vs their sum vs geo total
     -> if search+audience ~= geo, we have the correct network pair.
"""

import sqlite3
import pandas as pd
from pathlib import Path

DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
ACCT = 2
conn = sqlite3.connect(DB)
pd.set_option("display.width", 200)

def cols(t):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]

sc = cols("campaign_performance_daily")
print("### campaign_performance_daily columns:")
print("   ", sc)
print(f"   has 'network'? {'network' in sc}")
rev_col = "conversion_value" if "conversion_value" in sc else (
    "all_conv_value" if "all_conv_value" in sc else (
    "primary_conv_value" if "primary_conv_value" in sc else "conversions_value"))
lead_col = "primary_conv_count" if "primary_conv_count" in sc else (
    "conversions" if "conversions" in sc else "all_conv_count")
dc = "date" if "date" in sc else "report_date"
print(f"   using date={dc}, revenue={rev_col}, leads={lead_col}")

# network composition of the search table (if it has a network column)
if "network" in sc:
    print("\n### campaign_performance_daily network composition:")
    for r in conn.execute(f"""SELECT network, COUNT(*), ROUND(SUM(cost_micros)/1e6) spend,
                                     ROUND(SUM({rev_col})) rev
                              FROM campaign_performance_daily
                              WHERE account_id={ACCT} AND device='ALL'
                              GROUP BY network"""):
        print("   ", r)
else:
    print("\n### campaign_performance_daily has NO network column (pure search table).")

# monthly reconciliation: search vs audience vs sum vs geo
def monthly(table, valcol, datecol, extra=""):
    q = f"""SELECT substr({datecol},1,7) mon, ROUND(SUM({valcol})) rev
            FROM {table} WHERE account_id={ACCT} AND device='ALL'
              AND substr({datecol},1,7) IN ('2026-05','2026-06','2026-07'){extra}
            GROUP BY mon ORDER BY mon"""
    return pd.read_sql_query(q, conn).set_index("mon")["rev"]

search = monthly("campaign_performance_daily", rev_col, dc)
aud = monthly("campaign_performance_network_daily", "conversion_value", "date")
gc = cols("geo_performance_daily")
geo_rev = "conversion_value" if "conversion_value" in gc else "all_conv_value"
geo_dc = "date" if "date" in gc else "report_date"
geo = monthly("geo_performance_daily", geo_rev, geo_dc)

recon = pd.DataFrame({
    "search_table": search,
    "audience_table": aud,
    "search+audience": search.add(aud, fill_value=0),
    "geo_table (both nets)": geo,
})
recon["sum_vs_geo_delta"] = recon["search+audience"] - recon["geo_table (both nets)"]
print("\n### Monthly revenue reconciliation:")
print(recon.round(0).to_string())
print("\nIf search+audience ~= geo_table, the two campaign tables are the correct")
print("network pair and geo is their both-networks rollup. Small deltas = grain/coverage diffs.")

conn.close()
