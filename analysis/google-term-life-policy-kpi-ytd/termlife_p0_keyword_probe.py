#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
termlife_p0_keyword_probe.py  (v2 — schema-corrected per S0 echo 10/08)
=======================================================================
Term Life (account_id=29) — P0 campaign inventory + keyword concentration +
search-query universe dynamics. READ-ONLY probe; console output only.

v2 fixes: cost_micros/1e6; report_date in kpd/sqp; query_id + dynamic
query-text lookup; cpd conversions labeled native-only (routed leads from
kpd); kpd geo_state grain self-check with kpd-vs-cpd reconciliation.

Sections:
  S0  Environment, scale gates, kpd geo-grain check, reconciliation
  S1  P0 campaign inventory (all campaigns ever seen, spans, Tier1/Tier2)
  S1b Change-history: Tier mentions + recent consolidation dating
  S2  Keyword spend/conversion concentration (ethos vs rest; top keywords)
  S3  Query-universe dynamics for ethos keywords
  S4  New-query appearance by month (left-censored at window start)
"""

import sqlite3
import pandas as pd

DB_PATH = r"C:\Projects\google-ads-ops\db\paid_search.db"
ACCOUNT_ID = 29
WINDOW_START = "2025-08-01"   # clean routed history
YTD_START = "2026-01-01"

pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 60)
pd.set_option("display.max_rows", 300)
pd.set_option("display.float_format", lambda x: f"{x:,.2f}")


def connect():
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


def cols(con, table):
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]


def hr(title):
    print("\n" + "=" * 100)
    print(title)
    print("=" * 100)


def find_query_text_table(con):
    """Locate the normalized query dimension (query_id -> text)."""
    names = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')")]
    text_cands = ["query_text", "search_query", "search_term", "query",
                  "normalized_query", "text"]
    hits = []
    for n in names:
        if n == "search_query_performance":
            continue
        cs = cols(con, n)
        if "query_id" in cs or ("id" in cs and "quer" in n.lower()):
            for tc in text_cands:
                if tc in cs:
                    key = "query_id" if "query_id" in cs else "id"
                    hits.append((n, key, tc))
                    break
    # prefer a dedicated query table over query_classification
    hits.sort(key=lambda h: (0 if "quer" in h[0].lower()
                             and "class" not in h[0].lower() else 1))
    return hits


def main():
    con = connect()

    # ------------------------------------------------------------------ S0
    hr("S0  ENVIRONMENT / SCALE GATES / GRAIN CHECKS")
    n_ki = con.execute(
        "SELECT COUNT(*) FROM keyword_instances WHERE account_id=?",
        (ACCOUNT_ID,)).fetchone()[0]
    n_kpd = con.execute(
        "SELECT COUNT(*) FROM keyword_performance_daily p "
        "JOIN keyword_instances k ON k.id=p.instance_id "
        "WHERE k.account_id=?", (ACCOUNT_ID,)).fetchone()[0]
    n_cam = con.execute(
        "SELECT COUNT(*) FROM campaigns WHERE account_id=?",
        (ACCOUNT_ID,)).fetchone()[0]
    print(f"  keyword_instances (acct 29): {n_ki:,}   (gate ~95K)")
    print(f"  kpd rows (acct 29): {n_kpd:,}   (gate ~755K)")
    print(f"  campaigns registry rows: {n_cam:,}   (architect said 73)")
    sep26 = con.execute(
        "SELECT SUM(cost_micros)/1e6 FROM campaign_performance_daily "
        "WHERE account_id=? AND device='ALL' "
        "AND date>='2026-09-01' AND date<'2026-10-01'",
        (ACCOUNT_ID,)).fetchone()[0]
    print(f"  Sep-2026 account spend (cpd, device=ALL): ${(sep26 or 0):,.0f} "
          f"  (gate $100-150K/mo)")

    # kpd geo-grain self-check -----------------------------------------
    print("\n  kpd geo_state values under device='ALL' (grain check):")
    gg = pd.read_sql_query(
        "SELECT p.geo_state, COUNT(*) AS rows_ "
        "FROM keyword_performance_daily p "
        "JOIN keyword_instances k ON k.id=p.instance_id "
        "WHERE k.account_id=? AND p.device='ALL' "
        "GROUP BY 1 ORDER BY rows_ DESC LIMIT 12", con, params=(ACCOUNT_ID,))
    print(gg.to_string(index=False))
    geo_vals = set(gg["geo_state"].astype(str))
    rollup_markers = {"ALL", "", "None", "nan", "<NA>"}
    if len(geo_vals) == 1:
        GEO_PRED = ""  # single grain; nothing to filter
        print("  -> single geo grain under device='ALL'; no filter needed.")
    elif geo_vals & rollup_markers:
        mk = sorted(geo_vals & rollup_markers)[0]
        GEO_PRED = ("AND (p.geo_state IS NULL OR p.geo_state='' "
                    "OR p.geo_state='ALL')")
        print(f"  -> geo splits coexist with a rollup marker ({mk!r}); "
              f"filtering to rollup rows to avoid double-count.")
    else:
        GEO_PRED = ""
        print("  !! multiple geo values, no rollup marker — rows are geo "
              "splits; SUM across geo is the correct total (Google geo "
              "grammar). Verify with reconciliation below; escalate if "
              "recon is off >2%.")

    # Reconciliation: kpd vs cpd, Sep 2026, clicks + spend --------------
    print("\n  Reconciliation kpd (device=ALL{} ) vs cpd (device=ALL), "
          "Sep 2026:".format(", geo-handled" if GEO_PRED else ""))
    kk = con.execute(
        f"SELECT SUM(p.clicks), SUM(p.cost_micros)/1e6, "
        f"SUM(p.primary_conv_count) "
        f"FROM keyword_performance_daily p "
        f"JOIN keyword_instances k ON k.id=p.instance_id "
        f"WHERE k.account_id=? AND p.device='ALL' {GEO_PRED} "
        f"AND p.report_date>='2026-09-01' AND p.report_date<'2026-10-01'",
        (ACCOUNT_ID,)).fetchone()
    cc = con.execute(
        "SELECT SUM(clicks), SUM(cost_micros)/1e6, SUM(conversions) "
        "FROM campaign_performance_daily "
        "WHERE account_id=? AND device='ALL' "
        "AND date>='2026-09-01' AND date<'2026-10-01'",
        (ACCOUNT_ID,)).fetchone()
    print(f"    kpd: clicks={kk[0]:,}  spend=${kk[1]:,.0f}  "
          f"routed_leads={kk[2]:,.0f}")
    print(f"    cpd: clicks={cc[0]:,}  spend=${cc[1]:,.0f}  "
          f"native_conv={cc[2]:,.0f}  (native = blended, context only)")

    # ------------------------------------------------------------------ S1
    hr("S1  P0 CAMPAIGN INVENTORY — every campaign ever seen")
    inv = pd.read_sql_query("""
        SELECT c.campaign_id,
               c.campaign_name,
               c.status,
               c.campaign_type,
               c.bidding_strategy_type,
               MIN(p.date)                                   AS first_spend,
               MAX(p.date)                                   AS last_spend,
               COUNT(DISTINCT p.date)                        AS days_served,
               SUM(p.cost_micros)/1e6                        AS spend_life,
               SUM(CASE WHEN p.date>=:ytd THEN p.cost_micros END)/1e6
                                                             AS spend_2026,
               SUM(CASE WHEN p.date>=:aug THEN p.cost_micros END)/1e6
                                                             AS spend_aug25p,
               SUM(p.impressions)                            AS impr_life,
               SUM(p.conversions)                            AS native_conv
        FROM campaigns c
        LEFT JOIN campaign_performance_daily p
               ON p.campaign_id=c.campaign_id
              AND p.account_id=c.account_id
              AND p.device='ALL'
        WHERE c.account_id=:acct
        GROUP BY 1,2,3,4,5
        ORDER BY spend_life DESC
    """, con, params={"ytd": YTD_START, "aug": WINDOW_START,
                      "acct": ACCOUNT_ID})
    # routed leads per campaign from kpd (routing ruling)
    leads = pd.read_sql_query(f"""
        SELECT p.campaign_id,
               SUM(p.primary_conv_count) AS routed_leads_life,
               SUM(p.primary_conv_value)/1.0 AS routed_value_life
        FROM keyword_performance_daily p
        JOIN keyword_instances k ON k.id=p.instance_id
        WHERE k.account_id={ACCOUNT_ID} AND p.device='ALL' {GEO_PRED}
        GROUP BY 1
    """, con)
    inv = inv.merge(leads, on="campaign_id", how="left")
    tot = inv["spend_life"].sum()
    inv["spend_share_%"] = 100 * inv["spend_life"] / tot if tot else 0
    print(inv.to_string(index=False))
    print("\n  Status census:")
    print(inv.groupby("status")["campaign_id"].count().to_string())
    print("\n  NOTE: status is CURRENT; historical activity = spend spans "
          "+ S1b change history.")

    tier = inv[inv["campaign_name"].str.contains("tier", case=False,
                                                 na=False)]
    print("\n  Tier-named campaigns:")
    print(tier.to_string(index=False) if len(tier)
          else "    (none matched 'tier' — identify the two unknowns from "
               "the inventory above)")

    print("\n  Monthly spend by campaign, 2026 (device=ALL):")
    m = pd.read_sql_query("""
        SELECT c.campaign_name, SUBSTR(p.date,1,7) AS month,
               SUM(p.cost_micros)/1e6 AS spend
        FROM campaign_performance_daily p
        JOIN campaigns c ON c.campaign_id=p.campaign_id
                         AND c.account_id=p.account_id
        WHERE p.account_id=? AND p.device='ALL' AND p.date>=?
        GROUP BY 1,2
    """, con, params=(ACCOUNT_ID, YTD_START))
    piv = m.pivot_table(index="campaign_name", columns="month",
                        values="spend", aggfunc="sum").fillna(0)
    print(piv.round(0).to_string())

    # ------------------------------------------------------------------ S1b
    hr("S1b CHANGE HISTORY — Tier mentions + consolidation dating")
    ce = pd.read_sql_query(
        "SELECT * FROM campaign_change_events WHERE account_id=? "
        "ORDER BY change_date", con, params=(ACCOUNT_ID,))
    print(f"  acct-29 rows: {len(ce)}  (architect: 209)")
    blob = ce.astype(str).agg(" | ".join, axis=1)
    cmap = dict(zip(inv["campaign_id"], inv["campaign_name"]))
    ce["campaign_name"] = ce["campaign_id"].map(cmap)
    tmask = (blob.str.contains("tier", case=False) |
             ce["campaign_name"].astype(str).str.contains("tier", case=False))
    print(f"\n  Rows mentioning 'tier': {tmask.sum()}")
    if tmask.sum():
        print(ce[tmask].to_string(index=False))
    recent = ce[ce["change_date"].astype(str) >= "2026-09-10"]
    print(f"\n  Rows since 2026-09-10 (consolidation window): {len(recent)}")
    print(recent.to_string(index=False) if len(recent) else "    (none)")
    print("\n  change_type census (full history):")
    print(ce.groupby("change_type")["id"].count()
          .sort_values(ascending=False).to_string())

    # ------------------------------------------------------------------ S2
    hr("S2  KEYWORD CONCENTRATION — ethos vs rest (kpd x ki, device=ALL)")
    base = f"""
        FROM keyword_performance_daily p
        JOIN keyword_instances k ON k.id=p.instance_id
        WHERE k.account_id={ACCOUNT_ID} AND p.device='ALL' {GEO_PRED}
    """
    ethos_expr = ("CASE WHEN LOWER(k.keyword_text) LIKE '%ethos%' "
                  "THEN 'ethos' ELSE 'rest' END")

    print("\n  (a) Window splits, summed-numerator ratios:")
    for label, start in [("2025-08+", WINDOW_START), ("2026 YTD", YTD_START)]:
        g = pd.read_sql_query(f"""
            SELECT {ethos_expr} AS grp,
                   COUNT(DISTINCT p.instance_id) AS n_keywords,
                   SUM(p.cost_micros)/1e6 AS spend,
                   SUM(p.impressions) AS impressions,
                   SUM(p.clicks) AS clicks,
                   SUM(p.primary_conv_count) AS leads,
                   SUM(p.primary_conv_value) AS lead_value
            {base} AND p.report_date>='{start}'
            GROUP BY 1 ORDER BY spend DESC
        """, con)
        g["spend_%"] = 100 * g["spend"] / g["spend"].sum()
        g["leads_%"] = 100 * g["leads"] / g["leads"].sum()
        g["cpc"] = g["spend"] / g["clicks"]
        g["cpl"] = g["spend"] / g["leads"]
        print(f"\n    -- {label} --")
        print(g.to_string(index=False))

    print("\n  (b) Monthly ethos share of spend & leads (drift check):")
    mm = pd.read_sql_query(f"""
        SELECT SUBSTR(p.report_date,1,7) AS month, {ethos_expr} AS grp,
               SUM(p.cost_micros)/1e6 AS spend,
               SUM(p.primary_conv_count) AS leads
        {base} AND p.report_date>='{WINDOW_START}'
        GROUP BY 1,2
    """, con)
    for metric in ["spend", "leads"]:
        pv = mm.pivot_table(index="month", columns="grp", values=metric,
                            aggfunc="sum").fillna(0)
        denom = pv.sum(axis=1)
        pv["ethos_%"] = 100 * pv.get("ethos", 0) / denom
        print(f"\n    {metric}:")
        print(pv.round(1).to_string())

    print("\n  (c) Top 15 keywords by 2026 spend:")
    top = pd.read_sql_query(f"""
        SELECT k.keyword_text AS keyword, k.match_type,
               k.campaign_name,
               SUM(p.cost_micros)/1e6 AS spend,
               SUM(p.impressions) AS impr, SUM(p.clicks) AS clicks,
               SUM(p.primary_conv_count) AS leads,
               SUM(p.primary_conv_value) AS lead_value
        {base} AND p.report_date>='{YTD_START}'
        GROUP BY 1,2,3
        ORDER BY spend DESC LIMIT 15
    """, con)
    ytd_spend = con.execute(
        f"SELECT SUM(p.cost_micros)/1e6 {base} "
        f"AND p.report_date>='{YTD_START}'").fetchone()[0] or 1
    top["spend_%_of_acct"] = 100 * top["spend"] / ytd_spend
    top["cum_%"] = top["spend_%_of_acct"].cumsum()
    print(top.to_string(index=False))

    # ------------------------------------------------------------------ S3
    hr("S3  QUERY UNIVERSE — ethos keywords (sqp; suppression caveat)")
    print("  CAVEAT: sqp hides low-volume terms (~15% of conversions "
          "historically; worse on short windows). Distinct-query counts "
          "are LOWER BOUNDS; month-to-month direction at identical grain "
          "is valid. Query totals never reconcile to keyword totals, by "
          "construction.")

    qhits = find_query_text_table(con)
    if qhits:
        QTAB, QKEY, QTXT = qhits[0]
        print(f"  [resolve] query text dimension -> {QTAB}.{QTXT} "
              f"(join on {QKEY}); other candidates: "
              f"{[h[0] for h in qhits[1:]] or 'none'}")
        qtext_sel = f"q.{QTXT}"
        qtext_join = f"LEFT JOIN {QTAB} q ON q.{QKEY}=p.query_id"
    else:
        print("  [resolve] query text dimension -> NOT FOUND; displaying "
              "query_id. (Counts unaffected.) Escalate table name to "
              "architect if text needed.")
        qtext_sel = "p.query_id"
        qtext_join = ""

    sq_base = f"""
        FROM search_query_performance p
        JOIN campaigns c ON c.campaign_id=p.campaign_id
                         AND c.account_id={ACCOUNT_ID}
        JOIN keyword_instances k ON k.id=p.instance_id
                                 AND k.account_id={ACCOUNT_ID}
        WHERE p.platform='google' AND p.device='ALL'
    """
    s3 = pd.read_sql_query(f"""
        SELECT SUBSTR(p.report_date,1,7) AS month,
               {ethos_expr} AS grp,
               COUNT(DISTINCT p.query_id) AS distinct_queries,
               SUM(p.impressions) AS sqp_impr,
               SUM(p.clicks) AS sqp_clicks,
               SUM(p.primary_conv_count) AS leads
        {sq_base} AND p.report_date>='{WINDOW_START}'
        GROUP BY 1,2 ORDER BY 1,2
    """, con)
    pv3 = s3[s3["grp"] == "ethos"].set_index("month").drop(columns=["grp"])
    pv3["impr_per_query"] = pv3["sqp_impr"] / pv3["distinct_queries"]
    print("\n  ETHOS keywords — monthly query universe:")
    print(pv3.round(1).to_string())
    pr = s3[s3["grp"] == "rest"].set_index("month").drop(columns=["grp"])
    if len(pr):
        print("\n  REST keywords — monthly (contrast):")
        print(pr.round(1).to_string())

    print("\n  Ethos: top-1 / top-10 query impression share by month:")
    qq = pd.read_sql_query(f"""
        SELECT SUBSTR(p.report_date,1,7) AS month,
               p.query_id, MAX({qtext_sel}) AS qtext,
               SUM(p.impressions) AS impr
        FROM search_query_performance p
        JOIN campaigns c ON c.campaign_id=p.campaign_id
                         AND c.account_id={ACCOUNT_ID}
        JOIN keyword_instances k ON k.id=p.instance_id
                                 AND k.account_id={ACCOUNT_ID}
        {qtext_join}
        WHERE p.platform='google' AND p.device='ALL'
          AND p.report_date>='{WINDOW_START}'
          AND LOWER(k.keyword_text) LIKE '%ethos%'
        GROUP BY 1,2
    """, con)
    rows = []
    for mth, gdf in qq.groupby("month"):
        gdf = gdf.sort_values("impr", ascending=False)
        ti = gdf["impr"].sum()
        rows.append({
            "month": mth,
            "top1_query": str(gdf.iloc[0]["qtext"])[:40],
            "top1_%": 100 * gdf.iloc[0]["impr"] / ti,
            "top10_%": 100 * gdf.head(10)["impr"].sum() / ti,
            "tail_q_cnt(rank>10)": max(len(gdf) - 10, 0),
            "tail_impr_%": (100 * gdf.iloc[10:]["impr"].sum() / ti
                            if len(gdf) > 10 else 0.0),
        })
    print(pd.DataFrame(rows).round(1).to_string(index=False))

    print("\n  Suppressed-tail proxy (ethos): kpd vs sqp impressions; "
          "gap = Google-suppressed volume:")
    kk2 = pd.read_sql_query(f"""
        SELECT SUBSTR(p.report_date,1,7) AS month,
               SUM(p.impressions) AS kpd_impr
        {base} AND p.report_date>='{WINDOW_START}'
          AND LOWER(k.keyword_text) LIKE '%ethos%'
        GROUP BY 1
    """, con).set_index("month")
    merged = kk2.join(pv3[["sqp_impr"]], how="left")
    merged["suppressed_impr"] = merged["kpd_impr"] - merged["sqp_impr"]
    merged["suppressed_%"] = (100 * merged["suppressed_impr"]
                              / merged["kpd_impr"])
    print(merged.round(1).to_string())

    # ------------------------------------------------------------------ S4
    hr("S4  NEW-QUERY APPEARANCE BY MONTH (ethos; left-censored at window "
       "start — first ~2 months inflated by design)")
    qq["first_month"] = qq.groupby("query_id")["month"].transform("min")
    new = (qq[qq["month"] == qq["first_month"]]
           .groupby("month")
           .agg(new_queries=("query_id", "nunique"),
                new_query_impr=("impr", "sum")))
    allq = qq.groupby("month").agg(total_queries=("query_id", "nunique"),
                                   total_impr=("impr", "sum"))
    s4 = allq.join(new).fillna(0)
    s4["new_%_of_queries"] = 100 * s4["new_queries"] / s4["total_queries"]
    s4["new_%_of_impr"] = 100 * s4["new_query_impr"] / s4["total_impr"]
    print(s4.round(1).to_string())
    print("\n  Reading guide: EXPANSION = distinct & new-query counts "
          "rising with impressions; STALE CORE = impressions rising while "
          "distinct queries flat and top-1 share rising; CONTRACTION = "
          "distinct queries falling. Mind the suppression lower-bound and "
          "S4's left-censor.")

    print("\nDone. Paste full output back to the analysis thread.")
    con.close()


if __name__ == "__main__":
    main()
