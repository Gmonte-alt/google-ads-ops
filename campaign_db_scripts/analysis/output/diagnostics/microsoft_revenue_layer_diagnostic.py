"""
revenue_layer_diagnostic.py
===========================
Confirm where Microsoft revenue actually lives, given that an OFFLINE UPLOAD
posts near-official revenue back into the platform. Compares the two revenue
columns (primary_conv_value = pixel-time, all_conv_value = offline-inclusive)
across four layers x network x month, so we can see:

  1. Whether primary and all conversion VALUE actually differ (offline upload
     should make all_conv_value > primary_conv_value once offline posts).
  2. Whether that differs by LAYER (campaign / geo / ad-group / keyword) — the
     offline upload may reconcile at some grains and not others.
  3. Whether it differs by MONTH (May/June/July) — offline posts with a lag, so
     recent months may not have reconciled yet while older months have.
  4. The search-vs-audience revenue split at each layer, by month.

Read-only. Introspects columns per table and adapts.

Usage:
    python revenue_layer_diagnostic.py
"""

import sqlite3
import pandas as pd
from pathlib import Path

DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
ACCT = 2
MONTHS = ["2026-05", "2026-06", "2026-07"]
AUDIENCE = "Audience"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 30)


def cols(table):
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    except Exception:
        return []


def date_col(c):
    for cand in ("report_date", "date", "day", "stat_date"):
        if cand in c:
            return cand
    return None


def val_cols(c):
    """Return (primary_value_col, all_value_col) present in this table."""
    pv = "primary_conv_value" if "primary_conv_value" in c else None
    av = "all_conv_value" if "all_conv_value" in c else (
        "conversion_value" if "conversion_value" in c else (
        "conversions_value" if "conversions_value" in c else None))
    return pv, av


def lead_cols(c):
    pl = "primary_conv_count" if "primary_conv_count" in c else None
    al = "all_conv_count" if "all_conv_count" in c else (
        "conversions" if "conversions" in c else None)
    return pl, al


def bucket(alias="t"):
    return f"CASE WHEN {alias}.network='{AUDIENCE}' THEN 'audience' ELSE 'search' END"


def analyze(label, table, join_sql, where_sql, has_network, geo_all_guard=""):
    c = cols(table)
    if not c:
        print(f"\n### {label}: table {table} NOT FOUND\n")
        return
    dc = date_col(c)
    pv, av = val_cols(c)
    pl, al = lead_cols(c)
    print(f"\n{'='*78}\n### LAYER: {label}   (table: {table})")
    print(f"    date={dc}  primary_val={pv}  all_val={av}  primary_leads={pl}  all_leads={al}")
    if not dc or av is None:
        print("    -> missing date or value column; cannot analyze")
        return

    net_sel = f"{bucket()} AS bucket," if has_network else "'(no network dim)' AS bucket,"
    net_grp = ", bucket" if True else ""
    pv_sel = f"SUM(t.{pv}) AS rev_primary," if pv else "NULL AS rev_primary,"
    pl_sel = f"SUM(t.{pl}) AS leads_primary," if pl else "NULL AS leads_primary,"

    sql = f"""
        SELECT substr(t.{dc},1,7) AS mon,
               {net_sel}
               SUM(t.cost_micros)/1e6 AS spend,
               {pv_sel}
               SUM(t.{av})            AS rev_all,
               {pl_sel}
               SUM(t.{al})            AS leads_all
        FROM {table} t
        {join_sql}
        WHERE {where_sql}
          AND substr(t.{dc},1,7) IN ('2026-05','2026-06','2026-07')
          AND t.device='ALL'{geo_all_guard}
        GROUP BY mon{', bucket' if has_network else ''}
        ORDER BY mon{', bucket' if has_network else ''}
    """
    df = pd.read_sql_query(sql, conn)
    if df.empty:
        print("    -> no rows in May-Jul window")
        return
    # divergence between primary and all value = the offline upload signature
    if "rev_primary" in df and df["rev_primary"].notna().any():
        df["offline_uplift"] = df["rev_all"] - df["rev_primary"]
        df["uplift_pct"] = (df["offline_uplift"] / df["rev_primary"].where(df["rev_primary"]!=0) * 100).round(1)
    df["rpl_all"] = (df["rev_all"] / df["leads_all"].replace(0, pd.NA)).round(2)
    for col in ("spend", "rev_primary", "rev_all", "offline_uplift"):
        if col in df.columns and pd.api.types.is_numeric_dtype(df[col]):
            df[col] = pd.to_numeric(df[col], errors="coerce").round(0)
    print()
    print(df.to_string(index=False))


# -------- Layer 1: campaign (network table) --------
analyze("Campaign x network", "campaign_performance_network_daily",
        "" if "account_id" in cols("campaign_performance_network_daily")
           else "JOIN campaigns c ON c.campaign_id=t.campaign_id",
        (f"t.account_id={ACCT}" if "account_id" in cols("campaign_performance_network_daily")
           else f"c.account_id={ACCT}"),
        has_network=True)

# -------- Layer 2: geo (network) --------
gc = cols("geo_performance_daily")
geo_guard = ""  # geo has its own geo_state; account totals need the state rollup or sum
analyze("Geo x network", "geo_performance_daily",
        "" if "account_id" in gc else "JOIN campaigns c ON c.campaign_id=t.campaign_id",
        (f"t.account_id={ACCT}" if "account_id" in gc else f"c.account_id={ACCT}"),
        has_network=("network" in gc))

# -------- Layer 3: ad group (via keyword network table aggregated) --------
# ad_group_performance_daily has no network; use keyword network table grouped
# to ad group as the ad-group-grain network revenue source.
kc = cols("keyword_performance_network_daily")
kikey = "id" if "id" in cols("keyword_instances") else "instance_id"
if kc:
    print(f"\n{'='*78}\n### LAYER: Ad group x network   "
          f"(keyword_performance_network_daily -> keyword_instances.ad_group_name)")
    pv = "primary_conv_value" if "primary_conv_value" in kc else None
    av = "all_conv_value" if "all_conv_value" in kc else None
    dc = date_col(kc)
    sql = f"""
        SELECT substr(t.{dc},1,7) AS mon,
               {bucket()} AS bucket,
               COUNT(DISTINCT ki.ad_group_name) AS ad_groups,
               SUM(t.cost_micros)/1e6 AS spend,
               SUM(t.primary_conv_value) AS rev_primary,
               SUM(t.all_conv_value)     AS rev_all,
               SUM(t.primary_conv_count) AS leads
        FROM keyword_performance_network_daily t
        JOIN keyword_instances ki ON ki.{kikey}=t.instance_id
        WHERE ki.account_id={ACCT}
          AND substr(t.{dc},1,7) IN ('2026-05','2026-06','2026-07')
          AND t.device='ALL'
        GROUP BY mon, bucket ORDER BY mon, bucket
    """
    df = pd.read_sql_query(sql, conn)
    if not df.empty:
        df["offline_uplift"] = (df["rev_all"] - df["rev_primary"]).round(0)
        df["uplift_pct"] = ((df["rev_all"]-df["rev_primary"])/df["rev_primary"].where(df["rev_primary"]!=0)*100).round(1)
        df["rpl"] = (df["rev_all"]/df["leads"].where(df["leads"]!=0)).round(2)
        for col in ("spend","rev_primary","rev_all"):
            df[col] = df[col].round(0)
        print()
        print(df.to_string(index=False))

# -------- Layer 4: keyword (network table, no ad-group grouping) --------
analyze("Keyword x network (table total)", "keyword_performance_network_daily",
        f"JOIN keyword_instances ki ON ki.{kikey}=t.instance_id",
        f"ki.account_id={ACCT}", has_network=True)

# -------- Cross-layer reconciliation: total all_conv_value by month, all layers --------
print(f"\n{'='*78}\n### CROSS-LAYER total revenue (all_conv_value) by month — should the")
print("### layers agree, or does each capture a different slice?\n")
def layer_total(table, join, where, valcol, dc, extra=""):
    try:
        q = f"""SELECT substr(t.{dc},1,7) mon, SUM(t.{valcol}) rev
                FROM {table} t {join}
                WHERE {where} AND t.device='ALL'
                  AND substr(t.{dc},1,7) IN ('2026-05','2026-06','2026-07'){extra}
                GROUP BY mon ORDER BY mon"""
        return pd.read_sql_query(q, conn).set_index("mon")["rev"]
    except Exception as e:
        return pd.Series(dtype=float, name=str(e)[:40])

tots = {}
tots["campaign_net"] = layer_total("campaign_performance_network_daily",
    "" if "account_id" in cols("campaign_performance_network_daily") else "JOIN campaigns c ON c.campaign_id=t.campaign_id",
    (f"t.account_id={ACCT}" if "account_id" in cols("campaign_performance_network_daily") else f"c.account_id={ACCT}"),
    val_cols(cols("campaign_performance_network_daily"))[1], date_col(cols("campaign_performance_network_daily")))
tots["keyword_net"] = layer_total("keyword_performance_network_daily",
    f"JOIN keyword_instances ki ON ki.{kikey}=t.instance_id", f"ki.account_id={ACCT}",
    "all_conv_value", date_col(kc))
if "network" in gc:
    tots["geo"] = layer_total("geo_performance_daily",
        "" if "account_id" in gc else "JOIN campaigns c ON c.campaign_id=t.campaign_id",
        (f"t.account_id={ACCT}" if "account_id" in gc else f"c.account_id={ACCT}"),
        val_cols(gc)[1], date_col(gc))
print(pd.DataFrame(tots).round(0).to_string())

conn.close()
print("\nDone.")
