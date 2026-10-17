"""
microsoft_campaign_audience_persistence.py  (v2 - correct network pair)
=======================================================================
Campaign-level weekly search vs audience revenue share, sourced from the correct
network PAIR:
  search   = campaign_performance_daily          (search-only table, no network col)
  audience = campaign_performance_network_daily   (audience-only table)
Both carry conversion_value as revenue; UNION them, tag network, then compare.
(Verified: search+audience reconciles to geo_performance_daily to within $1/mo.)

Confirms the pattern: search and audience both rise one week; the next week
search revenue declines but audience continues to drive a higher share.
Surfaces the Tier 2 audience exclusion in early July.

Read-only.
"""
import sqlite3
import pandas as pd
from pathlib import Path

DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
ACCT = 2
conn = sqlite3.connect(DB)
pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 40)
pd.set_option("display.max_rows", 300)

# --- search half: campaign_performance_daily (no network column -> tag 'search')
search_sql = f"""
    SELECT t.campaign_id, t.date,
           'search' AS bucket,
           t.cost_micros, t.conversion_value AS revenue
    FROM campaign_performance_daily t
    WHERE t.account_id={ACCT} AND t.device='ALL'
      AND substr(t.date,1,7) IN ('2026-05','2026-06','2026-07')
"""
# --- audience half: campaign_performance_network_daily (audience-only)
aud_sql = f"""
    SELECT t.campaign_id, t.date,
           'audience' AS bucket,
           t.cost_micros, t.conversion_value AS revenue
    FROM campaign_performance_network_daily t
    WHERE t.account_id={ACCT} AND t.device='ALL'
      AND t.network='Audience'
      AND substr(t.date,1,7) IN ('2026-05','2026-06','2026-07')
"""
s = pd.read_sql_query(search_sql, conn)
a = pd.read_sql_query(aud_sql, conn)
df = pd.concat([s, a], ignore_index=True)
df["date"] = pd.to_datetime(df["date"])

# campaign names
names = pd.read_sql_query(
    f"SELECT campaign_id, campaign_name FROM campaigns WHERE account_id={ACCT}", conn)
df = df.merge(names, on="campaign_id", how="left")

# ISO week start (Monday)
df["week_start"] = (df["date"] - pd.to_timedelta(df["date"].dt.weekday, unit="D")).dt.date

grp = (df.groupby(["campaign_name", "campaign_id", "week_start", "bucket"])["revenue"]
         .sum().reset_index())
piv = grp.pivot_table(index=["campaign_name", "campaign_id", "week_start"],
                      columns="bucket", values="revenue", fill_value=0.0).reset_index()
for b in ("search", "audience"):
    if b not in piv.columns:
        piv[b] = 0.0
piv["total"] = piv["search"] + piv["audience"]
piv["aud_share_pct"] = (piv["audience"] / piv["total"].where(piv["total"] != 0) * 100).round(1)
piv = piv.sort_values(["campaign_name", "week_start"])
piv["search_wow"] = piv.groupby("campaign_id")["search"].diff().round(0)
piv["aud_wow"] = piv.groupby("campaign_id")["audience"].diff().round(0)
piv["share_wow"] = piv.groupby("campaign_id")["aud_share_pct"].diff().round(1)
piv["PATTERN"] = ((piv["search_wow"] < 0) & (piv["share_wow"] > 0)).map(
    {True: "search DOWN / aud-share UP", False: ""})
for c in ("search", "audience", "total"):
    piv[c] = piv[c].round(0)

# only campaigns with meaningful total revenue
tot_by_camp = piv.groupby("campaign_id")["total"].sum()
active = tot_by_camp[tot_by_camp > 1000].index
for cid in active:
    sub = piv[piv["campaign_id"] == cid]
    print(f"\n{'='*95}\n{sub['campaign_name'].iloc[0]}  (id={cid})")
    print(sub[["week_start", "search", "audience", "total", "aud_share_pct",
               "search_wow", "aud_wow", "share_wow", "PATTERN"]].to_string(index=False))

# Tier 2 exclusion
print(f"\n{'='*95}\nTIER 2 AUDIENCE EXCLUSION (audience -> ~0 from early July):")
t2 = piv[piv["campaign_name"].str.contains("Tier2", case=False, na=False)]
if not t2.empty:
    print(t2[["week_start", "search", "audience", "aud_share_pct"]].to_string(index=False))

# account level
print(f"\n{'='*95}\nACCOUNT-LEVEL weekly search vs audience revenue:")
acct = piv.groupby("week_start")[["search", "audience", "total"]].sum().reset_index()
acct["aud_share_pct"] = (acct["audience"] / acct["total"].where(acct["total"] != 0) * 100).round(1)
acct["search_wow"] = acct["search"].diff().round(0)
acct["share_wow"] = acct["aud_share_pct"].diff().round(1)
acct["PATTERN"] = ((acct["search_wow"] < 0) & (acct["share_wow"] > 0)).map(
    {True: "search DOWN / aud-share UP", False: ""})
for c in ("search", "audience", "total"):
    acct[c] = acct[c].round(0)
print(acct.to_string(index=False))
conn.close()
print("\nDone.")
