#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
termlife_p3_final.py  (v2 — 10/08 evening)
==========================================
v2 changes: team's IS observation re-anchored to post-Labor-Day Sep 7
(confirmed by Gilbert; diagnosis = denominator growth, not rank loss);
category-vs-brand Trends divergence added (5-yr 'term life insurance'
export) — upgrades H2 composition mechanism to HIGH confidence.
P3 — KPI decomposition + final Excel workbook + one-page verdict.
Term Life (account_id=29). READ-ONLY against paid_search.db; writes ONLY
the workbook under analysis\\...\\output\\.

Inputs:
  - paid_search.db (Google-side mechanics, campaign x month / weekly)
  - Looker disposition series (Tables 1-3, hardcoded below, from the
    disposition thread, pulled ~Oct 6-7 2026)
  - Google Trends weekly CSV (demand index)

Outputs:
  output\\Term_Life_YTD_KPI_Deep_Dive.xlsx
    Verdict | Decomposition | Campaign_Split | CR12_Weekly | CR3_Weekly |
    CR45_Weekly | Serving_Mix | Trends_Correlation | Device_Mix |
    Inventory | Events_Levers
  plus a console summary of the decomposition and verdict.

Grain discipline: Policy KPI exists at account x month (Oct-24+) and
campaign-group x month (Aug-26+) ONLY. Google-side mechanics are weekly/
monthly. No keyword- or device-grain KPI claims anywhere.
"""

import os
import sqlite3
import numpy as np
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

DB_PATH = r"C:\Projects\google-ads-ops\db\paid_search.db"
ANALYSIS_DIR = r"C:\Projects\google-ads-ops\analysis\google-term-life-policy-kpi-ytd"
TRENDS_CSV = os.path.join(ANALYSIS_DIR, "input",
                          "time_series_US_20251008-1436_20261008-1436-weekly.csv")
OUT_XLSX = os.path.join(ANALYSIS_DIR, "output",
                        "Term_Life_YTD_KPI_Deep_Dive.xlsx")

ACCOUNT_ID = 29
CAMPS = {"CR12": 22899566624, "CR3": 23075071366, "CR45": 22849317525}
WINDOW_START = "2025-08-01"
PULL_NOTE = "Looker pulled ~2026-10-06/07; DB through 2026-10-06"

NAVY = "1F3864"
LIGHT = "D9E2F2"

# ----------------------------------------------------------------- Looker
T1 = pd.DataFrame([
    # month, payment, ops, cri_prem, cris
    ("2024-10", 6300, 100, 22963, 9), ("2024-11", 20435, 343, 104199, 38),
    ("2024-12", 69065, 1032, 353038, 129),
    ("2025-01", 242140, 3308, 1297999, 479),
    ("2025-02", 90195, 1348, 349645, 138),
    ("2025-03", 81885, 1239, 338765, 139),
    ("2025-04", 90480, 1259, 472335, 164),
    ("2025-05", 77860, 1103, 346361, 128),
    ("2025-06", 104865, 1474, 421555, 175),
    ("2025-07", 134295, 1962, 584645, 214),
    ("2025-08", 99060, 1421, 449763, 162),
    ("2025-09", 85850, 1222, 327438, 122),
    ("2025-10", 121250, 1602, 605476, 212),
    ("2025-11", 98834, 1321, 443774, 168),
    ("2025-12", 130315, 1968, 529481, 178),
    ("2026-01", 139152, 1873, 594899, 191),
    ("2026-02", 109480, 1476, 488277, 157),
    ("2026-03", 120534, 1541, 622221, 217),
    ("2026-04", 104358, 1393, 495492, 167),
    ("2026-05", 77238, 990, 439037, 147),
    ("2026-06", 129871, 1438, 624225, 233),
    ("2026-07", 178700, 2002, 767873, 277),
    ("2026-08", 171942, 1971, 688078, 234),
    ("2026-09", 194555, 2305, 658067, 218),
    ("2026-10", 26074, 304, 49307, 21),
], columns=["month", "payment", "ops", "cri_prem", "cris"])

T2 = pd.DataFrame([
    ("2026-08", "CR12", 63810, 711, 220319),
    ("2026-08", "CR3-45", 95400, 1096, 345462),
    ("2026-09", "CR12", 90014, 1033, 297813),
    ("2026-09", "CR3-45", 103540, 1241, 266369),
    ("2026-10", "CR12", 14386, 160, 33501),
    ("2026-10", "CR3-45", 11688, 140, 12906),
], columns=["month", "group", "payment", "ops", "cri_prem"])

T3 = pd.DataFrame([
    ("2026-08", 149.67, 86.55, 30.98, 672, 513, 772),
    ("2026-09", 144.02, 84.33, 31.51, 783, 615, 895),
    ("2026-10", 148.53, 86.75, 33.24, 99, 87, 115),
], columns=["month", "high_per_op", "med_per_op", "low_per_op",
            "ops_high", "ops_med", "ops_low"])

# Demand series, monthly 2026 (Google Trends, hardcoded from 10/08 exports:
# ethos = mean of 'ethos life'+'ethos insurance' (2-yr monthly pull);
# category = 'term life insurance' (5-yr pull). Each index is normalized to
# its own pull's peak=100 — compare DIRECTION month to month, not levels
# across the two columns.)
CAT_VS_BRAND = pd.DataFrame([
    ("2026-01", 68, 96.5), ("2026-02", 79, 73.5), ("2026-03", 96, 73.0),
    ("2026-04", 100, 72.0), ("2026-05", 92, 57.5), ("2026-06", 92, 54.0),
    ("2026-07", 88, 60.0), ("2026-08", 77, 65.0), ("2026-09", 63, 71.0),
    ("2026-10", 54, 91.0),
], columns=["month", "category_term_life_idx", "brand_ethos_idx"])

EVENTS_TL = pd.DataFrame([
    ("2026-06", "SQ payout step +16% ($78->$90/op blended) — H4, resolved"),
    ("2026-06-15", "CR3 query tail inflects (16%+) — broad valve opens wide"),
    ("2026-07-12", "Trends demand inflects upward (weekly)"),
    ("2026-08-14", "Google 'Limited by budget' flag on CR12"),
    ("2026-08-17", "Google bid/pacing update; CR12 budget 3700->1800; "
                   "Tier budgets cut; trio targets at cycle peaks"),
    ("2026-08-18/21", "Tier1 / Tier2SG stop spending (consolidation)"),
    ("2026-08-24/31", "CR3/CR45 budgets to 700/300; budget-lost 44-50%"),
    ("2026-09-03", "'luminary life insurance' BROAD first serves in CR12"),
    ("2026-09-07", "Post-Labor-Day: CR12 IS collapse begins (the team's "
                   "observed drop) — eligible auctions 15K->100-120K/wk; "
                   "denominator growth, not rank loss"),
    ("2026-09-10/22", "Budget raises: CR3 900->6600, CR12 2500->5900, "
                      "CR45 900->4100; 9/18 tROAS cuts CR3/CR45"),
    ("2026-09-11/14", "CR12 'ethos life' BROAD explodes ~100->1,168/day"),
    ("2026-09-13+", "Trends second surge (56->70->87->94)"),
    ("2026-09-29", "Schedule-cut incident (separate; contaminated ->)"),
    ("2026-09-30", "SQ Q3 close (sales-floor push may inflate late-Sep "
                   "binds)"),
], columns=["date", "event"])


# ------------------------------------------------------------------ helpers
def connect():
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


def week_start(s):
    d = pd.to_datetime(s)
    return (d - pd.to_timedelta(d.dt.dayofweek % 7, unit="D")).dt.date


def meta(ws, title, source, window, caveat):
    ws["A1"] = title
    ws["A1"].font = Font(bold=True, size=13, color="FFFFFF")
    ws["A1"].fill = PatternFill("solid", fgColor=NAVY)
    ws["A2"] = f"Source: {source}   |   Window: {window}"
    ws["A3"] = f"Caveats: {caveat}"
    for r in (2, 3):
        ws[f"A{r}"].font = Font(italic=True, size=9)
    ws.freeze_panes = "A6"


def write_block(ws, row, subtitle, df):
    ws.cell(row=row, column=1, value=subtitle).font = Font(bold=True,
                                                           size=11)
    ws.cell(row=row, column=1).fill = PatternFill("solid", fgColor=LIGHT)
    row += 1
    for j, c in enumerate(df.columns, start=1):
        cell = ws.cell(row=row, column=j, value=str(c))
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=NAVY)
    row += 1
    for _, r in df.iterrows():
        for j, v in enumerate(r, start=1):
            if isinstance(v, (np.floating, float)):
                v = None if pd.isna(v) else round(float(v), 2)
            elif isinstance(v, (np.integer,)):
                v = int(v)
            ws.cell(row=row, column=j, value=v)
        row += 1
    for j, c in enumerate(df.columns, start=1):
        width = max(len(str(c)), *(len(str(x)) for x in df[c].head(60)))
        ws.column_dimensions[get_column_letter(j)].width = min(width + 2, 46)
    return row + 2


def weekly_panel(con, campaign_id):
    d = pd.read_sql_query("""
        SELECT date, impressions, clicks, cost_micros/1e6 AS spend,
               conversions AS leads, conversion_value AS lead_value,
               search_impression_share AS is_share,
               search_budget_lost_is AS bl, search_rank_lost_is AS rl
        FROM campaign_performance_daily
        WHERE account_id=? AND campaign_id=? AND device='ALL' AND date>=?
        ORDER BY date
    """, con, params=(ACCOUNT_ID, campaign_id, WINDOW_START))
    d["week"] = week_start(d["date"])
    ok = d["is_share"].notna() & (d["is_share"] > 0)
    d["eligible"] = np.where(ok, d["impressions"] / d["is_share"], np.nan)
    d["impr_rated"] = np.where(ok, d["impressions"], np.nan)
    d["bl_e"] = d["bl"] * d["eligible"]
    d["rl_e"] = d["rl"] * d["eligible"]
    d["cens"] = ((d["is_share"] == 0.0999) | (d["bl"] == 0.9001)
                 | (d["rl"] == 0.9001)).astype(int)
    w = d.groupby("week").agg(
        days=("date", "nunique"), impressions=("impressions", "sum"),
        clicks=("clicks", "sum"), spend=("spend", "sum"),
        leads=("leads", "sum"), lead_value=("lead_value", "sum"),
        impr_rated=("impr_rated", "sum"), eligible=("eligible", "sum"),
        bl_e=("bl_e", "sum"), rl_e=("rl_e", "sum"),
        cens_days=("cens", "sum")).reset_index()
    w["IS_%"] = 100 * w["impr_rated"] / w["eligible"]
    w["budget_lost_%"] = 100 * w["bl_e"] / w["eligible"]
    w["rank_lost_%"] = 100 * w["rl_e"] / w["eligible"]
    w["cpc"] = w["spend"] / w["clicks"]
    w["val_per_lead"] = w["lead_value"] / w["leads"]
    return w[["week", "days", "impressions", "eligible", "IS_%",
              "budget_lost_%", "rank_lost_%", "clicks", "spend", "cpc",
              "leads", "val_per_lead", "cens_days"]]


def main():
    os.makedirs(os.path.dirname(OUT_XLSX), exist_ok=True)
    con = connect()

    # ------------------------------------------------- Looker derivations
    t1 = T1.copy()
    t1["per_op"] = t1["payment"] / t1["ops"]
    t1["prem_per_op"] = t1["cri_prem"] / t1["ops"]
    t1["policy_kpi"] = t1["cri_prem"] / t1["payment"]

    t2 = T2.copy()
    t2["per_op"] = t2["payment"] / t2["ops"]
    t2["prem_per_op"] = t2["cri_prem"] / t2["ops"]
    t2["policy_kpi"] = t2["cri_prem"] / t2["payment"]

    def decomp(m0, m1):
        a = t1.set_index("month")
        k0, k1 = a.loc[m0], a.loc[m1]
        dk = np.log(k1["policy_kpi"] / k0["policy_kpi"])
        dq = np.log(k1["prem_per_op"] / k0["prem_per_op"])
        dr = -np.log(k1["per_op"] / k0["per_op"])
        return {"window": f"{m0} -> {m1}",
                "KPI_from": round(k0["policy_kpi"], 2),
                "KPI_to": round(k1["policy_kpi"], 2),
                "dln_KPI": round(dk, 4),
                "quality_contrib(dln prem/op)": round(dq, 4),
                "rate_contrib(-dln $/op)": round(dr, 4),
                "quality_share_%": round(100 * dq / dk, 1),
                "rate_share_%": round(100 * dr / dk, 1)}

    windows = [("2026-05", "2026-06"), ("2026-06", "2026-07"),
               ("2026-07", "2026-08"), ("2026-08", "2026-09"),
               ("2026-06", "2026-08"), ("2026-06", "2026-09"),
               ("2026-05", "2026-09")]
    dec = pd.DataFrame([decomp(a, b) for a, b in windows])

    # ------------------------------------------------- Google-side monthly
    gm = pd.read_sql_query("""
        SELECT SUBSTR(p.date,1,7) AS month, c.campaign_name,
               SUM(p.cost_micros)/1e6 AS g_spend,
               SUM(p.impressions) AS impressions,
               SUM(p.clicks) AS clicks,
               SUM(p.conversions) AS leads,
               SUM(p.conversion_value) AS lead_value
        FROM campaign_performance_daily p
        JOIN campaigns c ON c.campaign_id=p.campaign_id
                         AND c.account_id=p.account_id
        WHERE p.account_id=? AND p.device='ALL' AND p.date>='2026-01-01'
        GROUP BY 1,2
    """, con, params=(ACCOUNT_ID,))
    short = {"LIFE INSURANCE-CR12-SEARCH-ALLDEVICE-USA-STE": "CR12",
             "LIFE INSURANCE-CR3-SEARCH-ALLDEVICE-USA-STE": "CR3-45",
             "LIFE INSURANCE-CR45-SEARCH-ALLDEVICE-USA-STE": "CR3-45"}
    gm["group"] = gm["campaign_name"].map(short).fillna("OTHER")
    gg = gm.groupby(["month", "group"], as_index=False).agg(
        g_spend=("g_spend", "sum"), clicks=("clicks", "sum"),
        leads=("leads", "sum"), lead_value=("lead_value", "sum"))
    gg["cpl"] = gg["g_spend"] / gg["leads"]
    camp_split = t2.merge(gg[gg["group"].isin(["CR12", "CR3-45"])],
                          on=["month", "group"], how="left")
    camp_split["sq_rev_per_ad$"] = (camp_split["payment"]
                                    / camp_split["g_spend"])
    camp_split["prem_per_ad$"] = (camp_split["cri_prem"]
                                  / camp_split["g_spend"])

    # luminary carve-out
    lum = con.execute(f"""
        SELECT SUM(p.cost_micros)/1e6, SUM(p.primary_conv_count)
        FROM keyword_performance_daily p
        JOIN keyword_instances ki ON ki.id=p.instance_id
        WHERE ki.account_id={ACCOUNT_ID} AND p.device='ALL'
          AND p.campaign_id={CAMPS['CR12']}
          AND LOWER(ki.keyword_text) LIKE '%luminary%'
          AND p.report_date BETWEEN '2026-09-01' AND '2026-09-30'
    """).fetchone()
    lum_spend, lum_leads = (lum[0] or 0), (lum[1] or 0)

    # ------------------------------------------------- device mix
    dv = pd.read_sql_query("""
        SELECT SUBSTR(date,1,7) AS month, device,
               SUM(impressions) AS impressions, SUM(clicks) AS clicks,
               SUM(cost_micros)/1e6 AS spend, SUM(conversions) AS leads
        FROM campaign_performance_daily
        WHERE account_id=? AND device!='ALL' AND date>='2026-01-01'
        GROUP BY 1,2
    """, con, params=(ACCOUNT_ID,))
    dv_src = "cpd device splits"
    if not len(dv):
        dv = pd.read_sql_query(f"""
            SELECT SUBSTR(p.report_date,1,7) AS month, p.device,
                   SUM(p.impressions) AS impressions,
                   SUM(p.clicks) AS clicks,
                   SUM(p.cost_micros)/1e6 AS spend,
                   SUM(p.primary_conv_count) AS leads
            FROM keyword_performance_daily p
            JOIN keyword_instances ki ON ki.id=p.instance_id
            WHERE ki.account_id={ACCOUNT_ID} AND p.device!='ALL'
              AND p.report_date>='2026-01-01'
            GROUP BY 1,2
        """, con)
        dv_src = "kpd device splits (cpd has none)"

    def dev_share(frame, metric):
        pv = frame.pivot_table(index="month", columns="device",
                               values=metric, aggfunc="sum").fillna(0)
        sh = 100 * pv.div(pv.sum(axis=1), axis=0)
        sh.columns = [f"{c}_{metric}_%" for c in sh.columns]
        return sh

    dev_tbl = pd.concat([dev_share(dv, "impressions"),
                         dev_share(dv, "clicks"),
                         dev_share(dv, "leads")], axis=1).reset_index()
    mob_cols = [c for c in dev_tbl.columns if c.startswith("MOBILE")
                and "clicks" in c]
    mob_delta = ""
    if mob_cols and {"2026-05", "2026-09"} <= set(dev_tbl["month"]):
        m5 = dev_tbl.set_index("month").loc["2026-05", mob_cols[0]]
        m9 = dev_tbl.set_index("month").loc["2026-09", mob_cols[0]]
        mob_delta = (f"Mobile click share May {m5:.1f}% -> Sep {m9:.1f}% "
                     f"({m9 - m5:+.1f}pt)")

    # ------------------------------------------------- serving mix monthly
    sm = pd.read_sql_query(f"""
        SELECT SUBSTR(p.report_date,1,7) AS month, c.campaign_name,
               p.match_type_served, SUM(p.impressions) AS impr
        FROM search_query_performance p
        JOIN campaigns c ON c.campaign_id=p.campaign_id
                         AND c.account_id={ACCOUNT_ID}
        JOIN keyword_instances ki ON ki.id=p.instance_id
                                  AND ki.account_id={ACCOUNT_ID}
        WHERE p.platform='google' AND p.device='ALL'
          AND p.report_date>='2026-01-01'
          AND LOWER(ki.keyword_text) LIKE '%ethos%'
          AND p.campaign_id IN ({CAMPS['CR12']},{CAMPS['CR3']},
                                {CAMPS['CR45']})
        GROUP BY 1,2,3
    """, con)
    sm["camp"] = sm["campaign_name"].str.extract(r"-(CR\d+)-")
    mix_blocks = {}
    for camp, g in sm.groupby("camp"):
        pv = g.pivot_table(index="month", columns="match_type_served",
                           values="impr", aggfunc="sum").fillna(0)
        mix_blocks[camp] = (100 * pv.div(pv.sum(axis=1), axis=0)) \
            .round(1).reset_index()

    # tail monthly per campaign
    tq = pd.read_sql_query(f"""
        SELECT SUBSTR(p.report_date,1,7) AS month, p.campaign_id,
               p.query_id, SUM(p.impressions) AS impr
        FROM search_query_performance p
        JOIN keyword_instances ki ON ki.id=p.instance_id
                                  AND ki.account_id={ACCOUNT_ID}
        WHERE p.platform='google' AND p.device='ALL'
          AND p.report_date>='2026-01-01'
          AND LOWER(ki.keyword_text) LIKE '%ethos%'
          AND p.campaign_id IN ({CAMPS['CR12']},{CAMPS['CR3']},
                                {CAMPS['CR45']})
        GROUP BY 1,2,3
    """, con)
    rev = {v: k for k, v in CAMPS.items()}
    rows = []
    for (m, cid), g in tq.groupby(["month", "campaign_id"]):
        q = g.groupby("query_id")["impr"].sum().sort_values(ascending=False)
        ti = q.sum()
        rows.append({"month": m, "camp": rev.get(cid, cid),
                     "distinct_q": len(q),
                     "top1_%": round(100 * q.iloc[0] / ti, 1),
                     "top10_%": round(100 * q.head(10).sum() / ti, 1),
                     "tail_%": round(100 * q.iloc[10:].sum() / ti, 1)
                     if len(q) > 10 else 0.0})
    tail_m = pd.DataFrame(rows).sort_values(["camp", "month"])

    # ------------------------------------------------- trends correlations
    w12 = weekly_panel(con, CAMPS["CR12"])
    trends_ok = os.path.exists(TRENDS_CSV)
    corr_blocks = []
    merged = pd.DataFrame()
    if trends_ok:
        tw = pd.read_csv(TRENDS_CSV)
        tw.columns = [c.strip().lower() for c in tw.columns]
        tcol = [c for c in tw.columns if c.startswith("time")][0]
        vals = [c for c in tw.columns if c != tcol]
        tw["demand"] = tw[vals].mean(axis=1)
        tw["week"] = (pd.to_datetime(tw[tcol]) + pd.Timedelta(days=1)) \
            .dt.date
        merged = w12.merge(tw[["week", "demand"]], on="week", how="inner")
        merged["contam"] = (pd.to_datetime(merged["week"])
                            >= pd.Timestamp("2026-09-28"))

        def corr(frame, label):
            out = []
            for c in ["impressions", "eligible", "cpc", "budget_lost_%",
                      "rank_lost_%", "IS_%", "clicks", "leads"]:
                out.append({"window": label, "metric": c,
                            "pearson": round(frame["demand"].corr(frame[c]),
                                             3),
                            "spearman": round(frame["demand"].corr(
                                frame[c], method="spearman"), 3),
                            "n_weeks": len(frame)})
            return pd.DataFrame(out)
        corr_blocks = [corr(merged, "full"),
                       corr(merged[~merged["contam"]], "excl_contam"),
                       corr(merged[(pd.to_datetime(merged["week"])
                                    >= "2026-01-01")
                                   & (~merged["contam"])],
                            "2026_pre_contam")]

    # ------------------------------------------------- inventory + events
    inv = pd.read_sql_query("""
        SELECT c.campaign_name, c.status, c.campaign_type,
               MIN(p.date) AS first_spend, MAX(p.date) AS last_spend,
               SUM(p.cost_micros)/1e6 AS spend_life,
               SUM(CASE WHEN p.date>='2026-01-01' THEN p.cost_micros
                   END)/1e6 AS spend_2026
        FROM campaigns c
        LEFT JOIN campaign_performance_daily p
               ON p.campaign_id=c.campaign_id AND p.account_id=c.account_id
              AND p.device='ALL'
        WHERE c.account_id=?
        GROUP BY 1,2,3 ORDER BY spend_life DESC
    """, con, params=(ACCOUNT_ID,))
    ce = pd.read_sql_query(
        "SELECT change_date, campaign_id, change_type, old_value, "
        "new_value FROM campaign_change_events WHERE account_id=? "
        "ORDER BY change_date", con, params=(ACCOUNT_ID,))
    ce["camp"] = ce["campaign_id"].map(rev).fillna(ce["campaign_id"]
                                                  .astype(str))

    # ------------------------------------------------- verdict text
    d_js = dec[dec["window"] == "2026-06 -> 2026-09"].iloc[0]
    d_mj = dec[dec["window"] == "2026-05 -> 2026-06"].iloc[0]
    verdict = [
        "TERM LIFE (acct 29) — YTD POLICY KPI DECLINE: ONE-PAGE VERDICT "
        "(as of 2026-10-08)",
        "",
        "HEADLINE: The fall from the May peak (5.68) splits into two "
        "different stories.",
        f"  1) May->Jun (5.68->4.81): ~{abs(d_mj['rate_share_%']):.0f}% "
        "is the SQ payout step (+16%, H4). Mechanical. RESOLVED.",
        f"  2) Jun->Sep (4.81->3.38): rate actually helped "
        f"({d_js['rate_share_%']:.0f}% share); the decline is a "
        f"premium-per-op QUALITY event: $434 -> $286 (-34%).",
        "",
        "ATTRIBUTION OF THE JUN->SEP QUALITY DECLINE (estimates, "
        "judgment on evidence below):",
        "  H2  Demand-surge composition dilution ........ ~50-65%  "
        "[confidence: HIGH on mechanism]",
        "      'ethos' demand rose from wk of Jul 12, surged again "
        "Sep 13+ (Trends). Diluted demand entered via",
        "      two doors: CR3's broad valve (30-50% broad-served all "
        "year; tail 16-20% from Jun 15) and CR12's",
        "      exact core (invisible same-query mix shift; eligible-"
        "auction test rho=0.80, IS vs demand -0.60).",
        "      Jan-26 control: demand doubled, serving stayed ~96% "
        "exact, quality ROSE -> composition, not volume.",
        "      Category-vs-brand divergence (5-yr Trends): Jul-Oct "
        "'term life insurance' FELL 88->54 while 'ethos'",
        "      ROSE 60->91. Brand demand growing against a falling "
        "category = manufactured (ad-driven) demand;",
        "      Jan's surge rode the category's in-market season "
        "(Jan-Apr peak 100) - that's why Jan quality held.",
        "  H5  Operator lever actions .................... ~25-40%  "
        "[confidence: MED-HIGH; Sep-weighted]",
        "      (a) Jan-Aug efficiency ratchet (targets up to 1.43/1.39/"
        "1.17, budgets down), ending in the Aug 17-31",
        "      starvation: CR12 3700->1800, CR3 ->700, CR45 ->300/day; "
        "budget-lost 44-50% on CR3/45.",
        "      (b) Sep 10-22 whiplash: budgets 3-14x + tROAS cuts; "
        "Smart Bidding routed the money into broad",
        "      ('ethos life' BROAD ~100->1,168 impr/day at raise, no "
        "keyword changes) + new 'luminary' BROAD",
        f"      (added Sep 3: ${lum_spend:,.0f}, {lum_leads:.0f} leads "
        "= ~8% of CR12 Sep spend). CR3-45 got the biggest",
        "      swing and the widest valve -> its Sep prem/op $215 vs "
        "CR12 $288.",
        "  H1  Google Aug-17 bid/pacing update ........... ~0-10%   "
        "[confidence: LOW-MED residual]",
        "      No independent inflection in QS, serving mix, CPC or IS "
        "at Aug 17 across three campaigns; the",
        "      'Limited by budget' flag and lost-IS spikes trace to the "
        "team's own cuts + demand heat (H3 absorbed).",
        "  Tier1/Tier2 overlap ........................... ~0% of "
        "quality; peak 10.4% of ethos impressions, closed.",
        "  H4 in this window ............................. 0% (rate "
        "drifted DOWN Jun->Sep).",
        "",
        "MEASUREMENT CONFOUNDS ON SEPTEMBER (push in opposite "
        "directions):",
        "  - Premium maturation: 2026-08/09 revise UP ~10-15% over 1-3 "
        "months -> measured Sep decline will shrink.",
        "  - SQ Q3-close floor push (Sep 30): inflates late-Sep binds "
        "-> underlying Sep quality slightly worse than printed.",
        "  - Sep 29+ contaminated by the schedule-cut incident "
        "(separate, attributed); Oct MTD near-worthless.",
        "",
        "TIMELINE CORRECTION TO THE TEAM NARRATIVE:",
        "  - The team's observation - CR12 IS dropping after Labor Day "
        "(Sep 7) - is REAL but misdiagnosed: eligible",
        "    auctions exploded 15K->100-120K/wk with the demand surge; "
        "budget-lost spiked only until the Sep 11",
        "    raise; rank-lost stayed flat. Denominator growth, not "
        "position loss. Context: share ROSE Mar->Jul",
        "    (15%->44% peak wk Jul 27); May's volume dip was the "
        "demand trough, not an IS event. CR3/CR45 IS is",
        "    censored (<10%) all year - no trend claims there.",
        "",
        "KEY GRAIN LIMIT: Policy KPI exists at account x month "
        "(Oct-24+) and campaign-group x month (Aug-26+) only.",
        "All keyword/query/device findings are Google-side mechanics.",
        "",
        "WHAT WOULD CHANGE THIS ANSWER:",
        "  - Extended campaign-split Looker export (pre-Aug-26): splits "
        "Jun-Jul quality decline between CR12's exact",
        "    core and CR3's broad valve -> re-weights H2's two doors "
        "and could shift H5 share. [REQUESTED]",
        "  - Segment-level $/op before Jun-26: confirms the June rate "
        "step is a rate-card change, not mix. [REQUESTED]",
        "  - Premium maturation re-pull (Nov/Dec): restates Sep; "
        "re-run decomposition then.",
        "  - Ethos marketing calendar (public): LARGELY RESOLVED by "
        "the category-vs-brand divergence; a known",
        "    campaign launch date would close it fully.",
        "  - Warehouse lead->campaign attribution (Maureen): enables "
        "keyword-grain quality bridge; currently impossible.",
        f"  - Device composition check (this workbook): {mob_delta or 'see Device_Mix sheet'}.",
    ]

    # ------------------------------------------------- build workbook
    wb = Workbook()

    ws = wb.active
    ws.title = "Verdict"
    meta(ws, "One-Page Verdict — YTD Policy KPI Decline",
         f"paid_search.db + Looker disposition series + Google Trends "
         f"({PULL_NOTE})",
         "2026 YTD focus; events 2025-08+",
         "Estimates are adjudications, not measurements; see "
         "Decomposition for arithmetic; Sep provisional")
    ws.column_dimensions["A"].width = 110
    for i, line in enumerate(verdict, start=5):
        c = ws.cell(row=i, column=1, value=line)
        c.alignment = Alignment(wrap_text=False)
        if line.startswith(("HEADLINE", "ATTRIBUTION", "MEASUREMENT",
                            "TIMELINE", "WHAT WOULD", "KEY GRAIN",
                            "TERM LIFE")):
            c.font = Font(bold=True)

    ws = wb.create_sheet("Decomposition")
    meta(ws, "Policy KPI Decomposition — account x month",
         "Looker Table 1 (Strategy=Search)", "Oct-24..Oct-26",
         "ln decomposition: dln KPI = dln prem/op - dln $/op; 2026-08/09 "
         "premium immature (+10-15% pending); 2026-10 MTD; Sep-29+ "
         "contaminated")
    r = write_block(ws, 5, "Window decomposition (quality vs rate shares)",
                    dec)
    r = write_block(ws, r, "Account x month series", t1.round(2))
    write_block(ws, r, "Segment-level $/op (Aug-Oct; mix calibration)", T3)

    ws = wb.create_sheet("Campaign_Split")
    meta(ws, "Campaign group x month — SQ side vs Google side",
         "Looker Table 2 + cpd (device=ALL)", "Aug-26..Oct-26 (extended "
         "export REQUESTED)",
         "payment = SQ revenue, g_spend = Google cost; Sep-29+ "
         "contaminated; Oct MTD")
    r = write_block(ws, 5, "Group x month", camp_split.round(2))
    write_block(ws, r, "Luminary carve-out (CR12, Sep)",
                pd.DataFrame([{"keyword": "luminary life insurance [BROAD]",
                               "first_serve": "2026-09-03",
                               "sep_spend": round(lum_spend, 0),
                               "sep_leads": lum_leads}]))

    for nm in ["CR12", "CR3", "CR45"]:
        ws = wb.create_sheet(f"{nm}_Weekly")
        meta(ws, f"{nm} weekly exposure & outcomes",
             "cpd (device=ALL); eligible=impr/IS daily; levers in "
             "Events_Levers", f"{WINDOW_START}+ (Mon-Sat weeks)",
             "cens_days = rows at Google bounds 0.0999/0.9001 (CR3 "
             "censored all year, CR45 nearly); value/lead = volume-mix "
             "proxy, never lead quality")
        write_block(ws, 5, f"{nm} weekly",
                    weekly_panel(con, CAMPS[nm]).round(2))

    ws = wb.create_sheet("Serving_Mix")
    meta(ws, "Ethos serving mix & query tail — monthly, by campaign",
         "sqp (platform=google, device=ALL)", "2026 YTD",
         "sqp hides low-volume terms (~15% of conv); shares directional; "
         "tail = impressions rank>10")
    r = 5
    for camp in ["CR12", "CR3", "CR45"]:
        if camp in mix_blocks:
            r = write_block(ws, r, f"{camp} — match_type_served share (%)",
                            mix_blocks[camp])
    write_block(ws, r, "Query tail by month", tail_m)

    ws = wb.create_sheet("Trends_Correlation")
    meta(ws, "Google Trends (weekly) vs CR12 mechanics",
         "Trends export ('ethos life'+'ethos insurance' avg) + cpd",
         "Oct-25..Oct-26 overlap",
         "Trends index self-normalized per pull; contam = weeks >= "
         "2026-09-28; eligible is the H2 denominator test")
    cvb_title = ("Category vs brand — monthly 2026 (DIRECTION only; each "
                 "index normalized to its own pull). Jul-Oct: category "
                 "-39% while ethos +52% = manufactured brand demand; "
                 "Jan-Apr: both high = in-market season.")
    if trends_ok:
        r = write_block(ws, 5, "Correlations vs demand",
                        pd.concat(corr_blocks, ignore_index=True))
        r = write_block(ws, r, cvb_title, CAT_VS_BRAND)
        write_block(ws, r, "Merged weekly series", merged.round(2))
    else:
        ws.cell(row=5, column=1,
                value=f"Trends CSV not found at {TRENDS_CSV}")
        write_block(ws, 7, cvb_title, CAT_VS_BRAND)

    ws = wb.create_sheet("Device_Mix")
    meta(ws, "Device mix — account monthly (composition proxy)",
         dv_src, "2026 YTD",
         "Composition proxy for H2 ONLY (ad-driven demand skews mobile); "
         "no KPI at device grain. " + (mob_delta or ""))
    write_block(ws, 5, "Device share by month (%)", dev_tbl.round(1))

    ws = wb.create_sheet("Inventory")
    meta(ws, "Campaign inventory (P0)", "campaigns + cpd", "account life",
         "status is CURRENT; Tier1/Tier2 = income-tier experiments "
         "duplicating the core term, dead Aug 18/21; see analysis thread")
    write_block(ws, 5, "All campaigns", inv.round(0))

    ws = wb.create_sheet("Events_Levers")
    meta(ws, "Event timeline + lever change history",
         "campaign_change_events (curated; grid-backstop to ~Sep-24)",
         "full history",
         "No STATUS events in curated table; budget/tROAS only; cpd "
         "*_that_day columns snapshot-suspect (escalated)")
    r = write_block(ws, 5, "Analysis event timeline", EVENTS_TL)
    write_block(ws, r, "All lever changes (acct 29)", ce)

    wb.save(OUT_XLSX)

    # ------------------------------------------------- console summary
    print("=" * 100)
    print("P3 DECOMPOSITION — console summary")
    print("=" * 100)
    print(dec.to_string(index=False))
    print(f"\nLuminary carve-out (CR12 Sep): ${lum_spend:,.0f}, "
          f"{lum_leads:.0f} leads")
    if mob_delta:
        print(f"Device composition check: {mob_delta}")
    print("\n" + "=" * 100)
    print("VERDICT")
    print("=" * 100)
    print("\n".join(verdict))
    print(f"\nWorkbook written: {OUT_XLSX}")
    con.close()


if __name__ == "__main__":
    main()
