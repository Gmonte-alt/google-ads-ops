#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
termlife_p2_account.py  (v1)
============================
P2 — account-wide extension, Term Life (account_id=29). READ-ONLY, console.

Does CR12's story generalize, and how much of September was bought?
  A1  CR3 & CR45 weekly panels (exposure + levers + outcomes, W1-style)
  A2  CR3 & CR45 serving mix (match_type_served) + query tail, weekly
  A3  Broad/phrase ethos instances — who served when (the on/off bursts
      and the September broad reactivation, dated and named)
  A4  CR12 rest-side September surge — which keywords appeared/quintupled
  A5  Tier1/Tier2 overlap — weekly ethos impressions by campaign, Apr-Sep
      2026: did the Tiers cannibalize the trio's core-term auctions?

Conventions: device='ALL'; Mon-Sat weeks; routed = cpd conversions
(verified == kpd primary, V0 of P1); eligible = impr/IS daily; censored
bound rows (0.0999/0.9001) included in weighted aggregates, counts shown;
levers reconstructed from campaign_change_events (cpd *_that_day columns
are snapshot-suspect on this account).
"""

import sqlite3
import pandas as pd
import numpy as np

DB_PATH = r"C:\Projects\google-ads-ops\db\paid_search.db"
ACCOUNT_ID = 29
WINDOW_START = "2025-08-01"

CAMPS = {
    "CR12": 22899566624,
    "CR3": 23075071366,
    "CR45": 22849317525,
}
TIERS = {
    "TIER1": 23848293998,
    "TIER2SG": 23890445236,
    "TIER2_2025": 22880226919,
    "TIER1DMA": 23291968518,
}

EVENTS = {
    "2026-05-25": "Memorial Day marker",
    "2026-08-14": "Limited-by-budget flag (CR12)",
    "2026-08-17": "Google update + Tier/CR12 budget cuts",
    "2026-09-10": "Budget raises begin",
    "2026-09-18": "tROAS cuts CR3/CR45",
    "2026-09-29": "SCHEDULE-CUT INCIDENT (contaminated ->)",
}

pd.set_option("display.width", 240)
pd.set_option("display.max_columns", 60)
pd.set_option("display.max_rows", 500)
pd.set_option("display.float_format", lambda x: f"{x:,.2f}")


def connect():
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


def hr(t):
    print("\n" + "=" * 112)
    print(t)
    print("=" * 112)


def week_start(s):
    d = pd.to_datetime(s)
    return (d - pd.to_timedelta(d.dt.dayofweek % 7, unit="D")).dt.date


def event_label(wk):
    d0 = pd.Timestamp(wk)
    labs = [v for k, v in EVENTS.items()
            if d0 <= pd.Timestamp(k) <= d0 + pd.Timedelta(days=5)]
    return "; ".join(labs)


def lever_series(con, campaign_id, mx):
    ev = pd.read_sql_query(
        "SELECT change_date, change_type, old_value, new_value "
        "FROM campaign_change_events "
        "WHERE account_id=? AND campaign_id=? ORDER BY change_date",
        con, params=(ACCOUNT_ID, campaign_id))

    def step(kind):
        e = ev[ev["change_type"] == kind]
        if not len(e):
            return None
        idx = pd.date_range(WINDOW_START, mx, freq="D")
        s = pd.Series(np.nan, index=idx)
        s.iloc[0] = e.iloc[0]["old_value"]
        for _, r in e.iterrows():
            ts = pd.Timestamp(r["change_date"])
            if ts < idx[0]:
                s.iloc[0] = r["new_value"]
            elif ts <= idx[-1]:
                s.loc[ts] = r["new_value"]
        return s.ffill()

    def weekly_last(s):
        key = (s.index - pd.to_timedelta(s.index.dayofweek % 7,
                                         unit="D")).date
        return s.groupby(key).last()

    out = {}
    b = step("DAILY_BUDGET")
    t = step("TARGET_ROAS")
    if b is not None:
        out["budget_hist"] = weekly_last(b)
    if t is not None:
        out["troas_hist"] = weekly_last(t)
    return out


def weekly_panel(con, name, campaign_id):
    mx = con.execute(
        "SELECT MAX(date) FROM campaign_performance_daily "
        "WHERE account_id=? AND campaign_id=?",
        (ACCOUNT_ID, campaign_id)).fetchone()[0]
    d = pd.read_sql_query("""
        SELECT date, impressions, clicks, cost_micros/1e6 AS spend,
               conversions AS leads, conversion_value AS lead_value,
               search_impression_share AS is_share,
               search_budget_lost_is AS bl, search_rank_lost_is AS rl
        FROM campaign_performance_daily
        WHERE account_id=? AND campaign_id=? AND device='ALL' AND date>=?
        ORDER BY date
    """, con, params=(ACCOUNT_ID, campaign_id, WINDOW_START))
    if not len(d):
        print(f"  (no cpd rows for {name})")
        return None
    d["week"] = week_start(d["date"])
    ok = d["is_share"].notna() & (d["is_share"] > 0)
    d["eligible"] = np.where(ok, d["impressions"] / d["is_share"], np.nan)
    d["impr_rated"] = np.where(ok, d["impressions"], np.nan)
    d["bl_elig"] = d["bl"] * d["eligible"]
    d["rl_elig"] = d["rl"] * d["eligible"]
    d["cens"] = ((d["is_share"] == 0.0999) | (d["bl"] == 0.9001)
                 | (d["rl"] == 0.9001)).astype(int)

    w = d.groupby("week").agg(
        days=("date", "nunique"), impressions=("impressions", "sum"),
        clicks=("clicks", "sum"), spend=("spend", "sum"),
        leads=("leads", "sum"), lead_value=("lead_value", "sum"),
        impr_rated=("impr_rated", "sum"), eligible=("eligible", "sum"),
        bl_elig=("bl_elig", "sum"), rl_elig=("rl_elig", "sum"),
        cens_days=("cens", "sum")).reset_index()
    w["IS_%"] = 100 * w["impr_rated"] / w["eligible"]
    w["budget_lost_%"] = 100 * w["bl_elig"] / w["eligible"]
    w["rank_lost_%"] = 100 * w["rl_elig"] / w["eligible"]
    w["cpc"] = w["spend"] / w["clicks"]
    w["val_per_lead"] = w["lead_value"] / w["leads"]
    for k, s in lever_series(con, campaign_id, mx).items():
        w[k] = w["week"].map(s)
    w["event"] = w["week"].map(event_label)

    lever = [c for c in ["budget_hist", "troas_hist"] if c in w.columns]
    colsx = (["week", "days", "impressions", "eligible", "IS_%",
              "budget_lost_%", "rank_lost_%"] + lever +
             ["clicks", "spend", "cpc", "leads", "val_per_lead",
              "cens_days", "event"])
    print(f"\n  {name} weekly panel (campaign {campaign_id}; "
          f"max date {mx}):")
    print(w[colsx].round(2).to_string(index=False))
    return w


def serving_mix(con, name, campaign_id):
    sq = pd.read_sql_query("""
        SELECT p.report_date AS date, p.query_id, p.match_type_served,
               SUM(p.impressions) AS impressions
        FROM search_query_performance p
        JOIN keyword_instances ki ON ki.id=p.instance_id
                                  AND ki.account_id=?
        WHERE p.campaign_id=? AND p.platform='google' AND p.device='ALL'
          AND p.report_date>=?
          AND LOWER(ki.keyword_text) LIKE '%ethos%'
        GROUP BY 1,2,3
    """, con, params=(ACCOUNT_ID, campaign_id, WINDOW_START))
    if not len(sq):
        print(f"  (no ethos sqp rows for {name})")
        return
    sq["week"] = week_start(sq["date"])
    mt = (sq.groupby(["week", "match_type_served"])["impressions"].sum()
            .unstack(fill_value=0))
    mt_share = (100 * mt.div(mt.sum(axis=1), axis=0)).round(1)
    print(f"\n  {name} — match_type_served share of ethos impressions (%):")
    print(mt_share.to_string())

    rows = []
    for wk, g in sq.groupby("week"):
        q = g.groupby("query_id")["impressions"].sum() \
             .sort_values(ascending=False)
        ti = q.sum()
        rows.append({"week": wk, "distinct_q": len(q),
                     "top1_%": 100 * q.iloc[0] / ti if ti else np.nan,
                     "top10_%": 100 * q.head(10).sum() / ti if ti else np.nan,
                     "tail_%": (100 * q.iloc[10:].sum() / ti
                                if len(q) > 10 else 0.0),
                     "sqp_impr": ti})
    tail = pd.DataFrame(rows)
    tail["event"] = tail["week"].map(event_label)
    print(f"\n  {name} — ethos query tail by week:")
    print(tail.round(1).to_string(index=False))


def main():
    con = connect()

    # ------------------------------------------------------------------ A1/A2
    for name in ["CR3", "CR45"]:
        hr(f"A1/A2  {name} — EXPOSURE PANEL + SERVING MIX")
        weekly_panel(con, name, CAMPS[name])
        serving_mix(con, name, CAMPS[name])

    # ------------------------------------------------------------------ A3
    hr("A3  BROAD/PHRASE ETHOS INSTANCES — serving spans "
       "(who produced the on/off broad bursts and the Sep reactivation)")
    bp = pd.read_sql_query(f"""
        SELECT ki.keyword_text, ki.match_type, ki.campaign_name,
               ki.ad_group_name, ki.status AS kw_status,
               MIN(p.report_date) AS first_serve,
               MAX(p.report_date) AS last_serve,
               SUM(p.impressions) AS impr_total
        FROM keyword_performance_daily p
        JOIN keyword_instances ki ON ki.id=p.instance_id
        WHERE ki.account_id={ACCOUNT_ID} AND p.device='ALL'
          AND p.report_date>='{WINDOW_START}'
          AND LOWER(ki.keyword_text) LIKE '%ethos%'
          AND ki.match_type IN ('BROAD','PHRASE')
          AND p.impressions>0
        GROUP BY 1,2,3,4,5
        ORDER BY impr_total DESC
    """, con)
    print("\n  Instance spans (impressions>0 days only):")
    print(bp.to_string(index=False))

    print("\n  Monthly impressions, 2026, per broad/phrase ethos keyword "
          "x campaign:")
    bm = pd.read_sql_query(f"""
        SELECT ki.keyword_text || ' [' || ki.match_type || '] ' ||
               ki.campaign_name AS kw,
               SUBSTR(p.report_date,1,7) AS month,
               SUM(p.impressions) AS impr
        FROM keyword_performance_daily p
        JOIN keyword_instances ki ON ki.id=p.instance_id
        WHERE ki.account_id={ACCOUNT_ID} AND p.device='ALL'
          AND p.report_date>='2026-01-01'
          AND LOWER(ki.keyword_text) LIKE '%ethos%'
          AND ki.match_type IN ('BROAD','PHRASE')
        GROUP BY 1,2
    """, con)
    if len(bm):
        pv = bm.pivot_table(index="kw", columns="month", values="impr",
                            aggfunc="sum").fillna(0)
        print(pv.round(0).to_string())
    else:
        print("    (none)")

    print("\n  Daily serving around the Sep broad burst (Sep 1 - Sep 20), "
          "broad ethos instances, CR12+CR3+CR45:")
    bd = pd.read_sql_query(f"""
        SELECT p.report_date, ki.keyword_text, ki.campaign_name,
               SUM(p.impressions) AS impr, SUM(p.clicks) AS clicks
        FROM keyword_performance_daily p
        JOIN keyword_instances ki ON ki.id=p.instance_id
        WHERE ki.account_id={ACCOUNT_ID} AND p.device='ALL'
          AND p.report_date BETWEEN '2026-09-01' AND '2026-09-20'
          AND LOWER(ki.keyword_text) LIKE '%ethos%'
          AND ki.match_type='BROAD' AND p.impressions>0
        GROUP BY 1,2,3 ORDER BY 1
    """, con)
    print(bd.to_string(index=False) if len(bd) else "    (none)")

    # ------------------------------------------------------------------ A4
    hr("A4  CR12 REST-SIDE SEPTEMBER SURGE — which keywords appeared")
    a4 = pd.read_sql_query(f"""
        SELECT ki.keyword_text, ki.match_type, ki.ad_group_name,
               ki.status AS kw_status,
               MIN(p.report_date) AS first_serve,
               SUM(CASE WHEN p.report_date BETWEEN '2026-08-01'
                        AND '2026-08-31' THEN p.impressions ELSE 0 END)
                   AS impr_aug,
               SUM(CASE WHEN p.report_date BETWEEN '2026-09-01'
                        AND '2026-09-30' THEN p.impressions ELSE 0 END)
                   AS impr_sep,
               SUM(CASE WHEN p.report_date BETWEEN '2026-09-01'
                        AND '2026-09-30' THEN p.cost_micros ELSE 0 END)/1e6
                   AS spend_sep,
               SUM(CASE WHEN p.report_date BETWEEN '2026-09-01'
                        AND '2026-09-30' THEN p.primary_conv_count
                        ELSE 0 END) AS leads_sep
        FROM keyword_performance_daily p
        JOIN keyword_instances ki ON ki.id=p.instance_id
        WHERE ki.account_id={ACCOUNT_ID} AND p.device='ALL'
          AND p.campaign_id={CAMPS['CR12']}
          AND LOWER(ki.keyword_text) NOT LIKE '%ethos%'
        GROUP BY 1,2,3,4
        HAVING impr_sep >= 100
        ORDER BY impr_sep DESC
        LIMIT 30
    """, con)
    a4["new_in_sep"] = a4["first_serve"] >= "2026-09-01"
    print(a4.round(1).to_string(index=False))

    # ------------------------------------------------------------------ A5
    hr("A5  TIER OVERLAP — weekly ethos impressions by campaign, "
       "Apr-Sep 2026")
    ov = pd.read_sql_query(f"""
        SELECT p.report_date AS date, ki.campaign_name,
               SUM(p.impressions) AS impr, SUM(p.clicks) AS clicks,
               SUM(p.cost_micros)/1e6 AS spend
        FROM keyword_performance_daily p
        JOIN keyword_instances ki ON ki.id=p.instance_id
        WHERE ki.account_id={ACCOUNT_ID} AND p.device='ALL'
          AND p.report_date BETWEEN '2026-04-01' AND '2026-09-30'
          AND LOWER(ki.keyword_text) LIKE '%ethos%'
        GROUP BY 1,2
    """, con)
    ov["week"] = week_start(ov["date"])
    short = {"LIFE INSURANCE-CR12-SEARCH-ALLDEVICE-USA-STE": "CR12",
             "LIFE INSURANCE-CR3-SEARCH-ALLDEVICE-USA-STE": "CR3",
             "LIFE INSURANCE-CR45-SEARCH-ALLDEVICE-USA-STE": "CR45",
             "LIFE INSURANCE-TIER1-SEARCH-ALLDEVICE-USA-STE": "TIER1",
             "LIFE INSURANCE-TIER2STATEGROUPED-SEARCH-ALLDEVICE-USA-STE":
                 "TIER2SG",
             "LIFE INSURANCE-CR12-GENERIC-SEARCH-ALLDEVICE_USA_STE":
                 "CR12GEN"}
    ov["camp"] = ov["campaign_name"].map(short).fillna(ov["campaign_name"])
    pv = ov.pivot_table(index="week", columns="camp", values="impr",
                        aggfunc="sum").fillna(0)
    tiercols = [c for c in ["TIER1", "TIER2SG"] if c in pv.columns]
    if tiercols:
        pv["tier_share_%"] = (100 * pv[tiercols].sum(axis=1)
                              / pv.sum(axis=1))
    print("\n  Weekly ethos-keyword impressions by campaign:")
    print(pv.round(1).to_string())

    sp = ov.pivot_table(index="week", columns="camp", values="spend",
                        aggfunc="sum").fillna(0)
    print("\n  Weekly ethos-keyword spend by campaign ($):")
    print(sp.round(0).to_string())

    print("\n  Reading: if CR12's ethos impressions/IS dip while Tier "
          "columns are nonzero and recover after Aug 18-21, the Tiers were "
          "cannibalizing the same auctions (they ran the same core EXACT "
          "term). Tier spend ended Aug 18/21.")

    print("\nDone. Paste full output back to the analysis thread.")
    con.close()


if __name__ == "__main__":
    main()
