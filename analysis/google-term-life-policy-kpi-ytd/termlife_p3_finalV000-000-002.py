#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
termlife_p3_final.py  (v3 — 10/08 evening)
==========================================
v2: team's IS observation re-anchored to post-Labor-Day Sep 7 (confirmed;
diagnosis = denominator growth, not rank loss); category-vs-brand Trends
divergence added — H2 composition mechanism upgraded to HIGH confidence.
v3: Takeaway_Charts sheet — one native Excel line chart per trend-shaped
takeaway (T1 KPI split, T2 demand/eligible co-movement + category-vs-brand,
T3 lever timeline + budget-lost, T5 IS anatomy, T6 Google blindness,
T7 broad valve), fed from a Chart_Data sheet. Max 3 colored series per
chart, indexed to a common base where scales differ (never dual-axis).
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
from openpyxl.chart import LineChart, Reference
from openpyxl.chart.layout import Layout, ManualLayout
from openpyxl.drawing.text import RichTextProperties
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
# Categorical series colors, fixed order (validated palette; <=3 per chart).
SERIES_HEX = ["2A78D6", "EB6834", "1BAF7A", "EDA100", "E87BA4", "008300"]
REF_HEX = "898781"  # muted gray for reference/guide lines

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


def meta(ws, title, source, window, caveat, takeaway=None):
    ws["A1"] = title
    ws["A1"].font = Font(bold=True, size=13, color="FFFFFF")
    ws["A1"].fill = PatternFill("solid", fgColor=NAVY)
    ws["A2"] = f"Source: {source}   |   Window: {window}"
    ws["A3"] = f"Caveats: {caveat}"
    for r in (2, 3):
        ws[f"A{r}"].font = Font(italic=True, size=9)
    if takeaway:
        ws["A4"] = f"TAKEAWAY: {takeaway}"
        ws["A4"].font = Font(bold=True, size=10)
        ws["A4"].fill = PatternFill("solid", fgColor=LIGHT)
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


def write_series_block(ws, start_col, df):
    """Write a chart-source block: headers row 1, data rows 2+."""
    for j, cname in enumerate(df.columns):
        ws.cell(row=1, column=start_col + j, value=str(cname))
        for i, v in enumerate(df[cname].tolist(), start=2):
            if isinstance(v, (np.floating, float)):
                v = None if pd.isna(v) else round(float(v), 2)
            elif isinstance(v, (np.integer,)):
                v = int(v)
            elif v is not None and not isinstance(v, (int, float)):
                v = str(v)
            ws.cell(row=i, column=start_col + j, value=v)
    return len(df)


def make_line_chart(ws_data, start_col, n_series, n_rows, title, y_title,
                    ref_styles=None, skip=4):
    """Line chart over a write_series_block block. ref_styles maps a series
    index to 'dash'/'sysDot' and renders it muted gray (guide lines)."""
    ch = LineChart()
    ch.title = title
    data = Reference(ws_data, min_col=start_col + 1,
                     max_col=start_col + n_series,
                     min_row=1, max_row=n_rows + 1)
    cats = Reference(ws_data, min_col=start_col, min_row=2,
                     max_row=n_rows + 1)
    ch.add_data(data, titles_from_data=True)
    ch.set_categories(cats)
    # Title above the plot, never over it
    if ch.title is not None:
        ch.title.overlay = False
    # Legend on the right, outside the plot area
    ch.legend.position = "r"
    ch.legend.overlay = False
    # Axis titles: y rotated vertical so it can't stack over tick labels
    ch.y_axis.title = y_title
    try:
        ch.y_axis.title.tx.rich.bodyPr = RichTextProperties(
            rot=-5400000, vert="horz")
    except AttributeError:
        pass  # older openpyxl layouts; title still renders
    x_name = ws_data.cell(row=1, column=start_col).value
    if x_name:
        ch.x_axis.title = str(x_name)
        if ch.x_axis.title is not None:
            try:
                ch.x_axis.title.overlay = False
            except AttributeError:
                pass
    ch.x_axis.delete = False
    ch.y_axis.delete = False
    ch.x_axis.tickLblSkip = skip
    ch.x_axis.tickMarkSkip = skip
    # Manual plot-area layout: reserve explicit margins so the rotated
    # y-title, tick labels, x-title and right-side legend never collide
    # (Excel auto-layout is unreliable for generated charts).
    ch.layout = Layout(manualLayout=ManualLayout(
        layoutTarget="inner", xMode="edge", yMode="edge",
        x=0.10, y=0.14, w=0.72, h=0.66))
    ch.height = 9.5
    ch.width = 30
    ref_styles = ref_styles or {}
    ci = 0
    for i, s in enumerate(ch.series):
        s.smooth = False
        s.graphicalProperties.line.width = 20000
        if i in ref_styles:
            s.graphicalProperties.line.solidFill = REF_HEX
            s.graphicalProperties.line.dashStyle = ref_styles[i]
        else:
            s.graphicalProperties.line.solidFill = \
                SERIES_HEX[ci % len(SERIES_HEX)]
            ci += 1
    return ch


def lever_series(con, campaign_id):
    """Daily budget / tROAS step series from campaign_change_events,
    rolled to end-of-week values (cpd *_that_day columns are
    snapshot-suspect on this account)."""
    mx = con.execute(
        "SELECT MAX(date) FROM campaign_performance_daily "
        "WHERE account_id=? AND campaign_id=?",
        (ACCOUNT_ID, campaign_id)).fetchone()[0]
    ev = pd.read_sql_query(
        "SELECT change_date, change_type, old_value, new_value "
        "FROM campaign_change_events "
        "WHERE account_id=? AND campaign_id=? ORDER BY change_date",
        con, params=(ACCOUNT_ID, campaign_id))

    def step(kind):
        e = ev[ev["change_type"] == kind]
        if not len(e) or mx is None:
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
    for kind, name in [("DAILY_BUDGET", "budget_hist"),
                       ("TARGET_ROAS", "troas_hist")]:
        s = step(kind)
        if s is not None:
            out[name] = weekly_last(s)
    return out


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
                "KPI_chg_%": round(100 * (np.exp(dk) - 1), 1),
                "quality(prem/op)_chg_%": round(100 * (np.exp(dq) - 1), 1),
                "rate($/op)_chg_%": round(100 * (np.exp(-dr) - 1), 1),
                "quality_share_%": round(100 * dq / dk, 1),
                "rate_share_%": round(100 * dr / dk, 1),
                "dln_KPI": round(dk, 4),
                "dln_quality": round(dq, 4),
                "dln_rate_contrib": round(dr, 4)}

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
    # NOTE: campaign_id returns as TEXT from sqp/change tables in this DB —
    # all id->name maps go through str() on both sides.
    rev = {str(v): k for k, v in CAMPS.items()}
    rows = []
    for (m, cid), g in tq.groupby(["month", "campaign_id"]):
        q = g.groupby("query_id")["impr"].sum().sort_values(ascending=False)
        ti = q.sum()
        rows.append({"month": m, "camp": rev.get(str(cid), str(cid)),
                     "distinct_q": len(q),
                     "top1_%": round(100 * q.iloc[0] / ti, 1),
                     "top10_%": round(100 * q.head(10).sum() / ti, 1),
                     "tail_%": round(100 * q.iloc[10:].sum() / ti, 1)
                     if len(q) > 10 else 0.0})
    tail_m = pd.DataFrame(rows).sort_values(["camp", "month"])

    # ------------------------------------------------- weekly panels (once)
    panels = {nm: weekly_panel(con, cid) for nm, cid in CAMPS.items()}

    # ------------------------------------------------- head queries (Sep)
    has_sq = con.execute("SELECT 1 FROM sqlite_master "
                         "WHERE name='search_queries'").fetchone()
    q_sel = ("COALESCE(q.query_text, CAST(p.query_id AS TEXT))"
             if has_sq else "CAST(p.query_id AS TEXT)")
    q_join = ("LEFT JOIN search_queries q ON q.id=p.query_id"
              if has_sq else "")
    head_tbl = pd.read_sql_query(f"""
        SELECT {q_sel} AS query,
               SUM(p.impressions) AS impressions,
               SUM(p.clicks) AS clicks,
               SUM(p.primary_conv_count) AS leads
        FROM search_query_performance p
        JOIN keyword_instances ki ON ki.id=p.instance_id
                                  AND ki.account_id={ACCOUNT_ID}
        {q_join}
        WHERE p.platform='google' AND p.device='ALL'
          AND p.report_date BETWEEN '2026-09-01' AND '2026-09-30'
          AND LOWER(ki.keyword_text) LIKE '%ethos%'
        GROUP BY 1 ORDER BY impressions DESC LIMIT 10""", con)
    eth_sep_impr = con.execute(f"""
        SELECT SUM(p.impressions)
        FROM search_query_performance p
        JOIN keyword_instances ki ON ki.id=p.instance_id
                                  AND ki.account_id={ACCOUNT_ID}
        WHERE p.platform='google' AND p.device='ALL'
          AND p.report_date BETWEEN '2026-09-01' AND '2026-09-30'
          AND LOWER(ki.keyword_text) LIKE '%ethos%'
        """).fetchone()[0] or 1
    head_tbl["impr_share_%"] = (100 * head_tbl["impressions"]
                                / eth_sep_impr)

    # ------------------------------------------------- trends correlations
    w12 = panels["CR12"]
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
    ce["camp"] = (ce["campaign_id"].astype(str).map(rev)
                  .fillna(ce["campaign_id"].astype(str)))

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
        "",
        "HOW TO READ THIS WORKBOOK (each sheet also carries its own "
        "TAKEAWAY line at the top):",
        "  Takeaway_Charts ... the whole argument as 11 charts, in order "
        "- start here after this page",
        "  Decomposition ..... the arithmetic: rate step vs quality "
        "event, in plain % and exact log form",
        "  Campaign_Split .... where Sep's damage landed (CR3-45 $215 "
        "vs CR12 $288) + luminary carve-out",
        "  CR12/CR3/CR45_Weekly ... per-campaign exposure panels "
        "(IS, budget-lost, spend, leads)",
        "  Serving_Mix ....... broad/exact serving + query tail + the "
        "top-10 head queries, named",
        "  Trends_Correlation  demand vs auction mechanics (rho=0.80) + "
        "category-vs-brand divergence",
        "  Device_Mix ........ supportive composition proxy only",
        "  Inventory ......... every campaign ever, incl. the "
        "Tier1/Tier2 answer",
        "  Events_Levers ..... the budget/tROAS change record behind "
        "the T3 charts",
        "  (Chart_Data is hidden plumbing - the charts reference it; "
        "do not delete it.)",
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
         "contaminated",
         takeaway="May->Jun was ~87% the SQ payout step (mechanical, "
         "resolved). Jun->Sep is a QUALITY event: prem/op $434->$286 "
         "(-34.2%) while the rate actually helped (-6.5%).")
    r = write_block(ws, 5, "Window decomposition — each KPI change split "
                    "into its quality part (prem/op) and rate part ($/op). "
                    "chg_% columns are plain percent changes; dln columns "
                    "are the exact additive log form (dln KPI = dln "
                    "prem/op - dln $/op); shares = each part / total. "
                    "A negative rate share means the rate OFFSET the "
                    "decline.", dec)
    r = write_block(ws, r, "Account x month series", t1.round(2))
    write_block(ws, r, "Segment-level $/op (Aug-Oct; mix calibration)", T3)

    ws = wb.create_sheet("Campaign_Split")
    meta(ws, "Campaign group x month — SQ side vs Google side",
         "Looker Table 2 + cpd (device=ALL)", "Aug-26..Oct-26 (extended "
         "export REQUESTED)",
         "payment = SQ revenue, g_spend = Google cost; Sep-29+ "
         "contaminated; Oct MTD",
         takeaway="September's quality damage concentrated in CR3-45 "
         "(prem/op $215 vs CR12's $288) — the group with the biggest "
         "lever swing and the widest broad valve. Luminary carved out "
         "below (~8% of CR12 Sep spend, quality unmeasurable).")
    r = write_block(ws, 5, "Group x month", camp_split.round(2))
    write_block(ws, r, "Luminary carve-out (CR12, Sep)",
                pd.DataFrame([{"keyword": "luminary life insurance [BROAD]",
                               "first_serve": "2026-09-03",
                               "sep_spend": round(lum_spend, 0),
                               "sep_leads": lum_leads}]))

    panel_takeaways = {
        "CR12": "CR12 never lost rank: share ROSE Mar->Jul (15%->44%); "
                "the Sep IS collapse is eligible auctions exploding "
                "15K->120K/wk (demand), with budget-lost spiking only "
                "until the Sep 11 raise.",
        "CR3": "CR3 sits below Google's 10% IS floor EVERY week "
               "(censored — no share-trend claims); the late-Aug budget "
               "cuts put lost-to-budget at 44-50% of eligible.",
        "CR45": "Same starvation pattern as CR3 (budget cut to $300/day "
                "late Aug); dark all of Feb 2026; IS censored nearly "
                "all year.",
    }
    for nm in ["CR12", "CR3", "CR45"]:
        ws = wb.create_sheet(f"{nm}_Weekly")
        meta(ws, f"{nm} weekly exposure & outcomes",
             "cpd (device=ALL); eligible=impr/IS daily; levers in "
             "Events_Levers", f"{WINDOW_START}+ (Mon-Sat weeks)",
             "cens_days = rows at Google bounds 0.0999/0.9001 (CR3 "
             "censored all year, CR45 nearly); value/lead = volume-mix "
             "proxy, never lead quality",
             takeaway=panel_takeaways[nm])
        write_block(ws, 5, f"{nm} weekly", panels[nm].round(2))

    ws = wb.create_sheet("Serving_Mix")
    meta(ws, "Ethos serving mix & query tail — monthly, by campaign",
         "sqp (platform=google, device=ALL)", "2026 YTD",
         "sqp hides low-volume terms (~15% of conv); shares directional; "
         "tail = impressions rank>10",
         takeaway="Dilution's visible face: CR3 served 30-50% broad all "
         "year (tail 16-20% from mid-June); CR12 stayed 4-5% tail "
         "through Jul-Aug — then jumped to ~20% in Sep when the raised "
         "budget flowed into broad. Head queries table at bottom.")
    r = 5
    for camp in ["CR12", "CR3", "CR45"]:
        if camp in mix_blocks:
            r = write_block(ws, r, f"{camp} — match_type_served share (%)",
                            mix_blocks[camp])
    r = write_block(ws, r, "Query tail by month", tail_m)
    write_block(ws, r, "The HEAD, concretely — top 10 ethos queries, Sep "
                "2026, account-wide (share of all ethos sqp impressions)",
                head_tbl.round(1))

    ws = wb.create_sheet("Trends_Correlation")
    meta(ws, "Google Trends (weekly) vs CR12 mechanics",
         "Trends export ('ethos life'+'ethos insurance' avg) + cpd",
         "Oct-25..Oct-26 overlap",
         "Trends index self-normalized per pull; contam = weeks >= "
         "2026-09-28; eligible is the H2 denominator test",
         takeaway="Demand and CR12's eligible auctions co-move (Spearman "
         "0.80, 2026 pre-contamination). Jul-Oct: brand demand ROSE ~50% "
         "while the term-life category FELL ~40% = manufactured demand, "
         "not seasonality (January, the control, rode the category peak "
         "and quality held).")
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
         "no KPI at device grain. " + (mob_delta or ""),
         takeaway="Supportive evidence only: mobile click share rose "
         "May->Sep, consistent with ad-prompted (upper-funnel) traffic "
         "entering the mix.")
    write_block(ws, 5, "Device share by month (%)", dev_tbl.round(1))

    ws = wb.create_sheet("Inventory")
    meta(ws, "Campaign inventory (P0)", "campaigns + cpd", "account life",
         "status is CURRENT; Tier1/Tier2 = income-tier experiments "
         "duplicating the core term, dead Aug 18/21; see analysis thread",
         takeaway="76 campaigns ever, 3 enabled today (CR12/CR3/CR45). "
         "The ~44 state campaigns are the pre-Jun-2025 era. Tier1/Tier2 "
         "(2026) were income-tier tests duplicating the core ethos term "
         "— minor overlap, closed.")
    write_block(ws, 5, "All campaigns", inv.round(0))

    ws = wb.create_sheet("Events_Levers")
    meta(ws, "Event timeline + lever change history",
         "campaign_change_events (curated; grid-backstop to ~Sep-24)",
         "full history",
         "No STATUS events in curated table; budget/tROAS only; cpd "
         "*_that_day columns snapshot-suspect (escalated)",
         takeaway="The lever record behind T3: targets ratcheted UP "
         "Jan-Aug while budgets drifted down, deep cuts Aug 17-31 "
         "(CR12 3700->1800, CR3 ->700, CR45 ->300), then the Sep 10-22 "
         "whiplash: budgets 3-14x + tROAS cuts.")
    r = write_block(ws, 5, "Analysis event timeline", EVENTS_TL)
    write_block(ws, r, "All lever changes (acct 29)", ce)

    # ------------------------------------------------- takeaway charts
    ws_cd = wb.create_sheet("Chart_Data")          # chart source blocks
    ws_tc = wb.create_sheet("Takeaway_Charts", 1)  # right after Verdict
    meta(ws_tc, "Takeaway Charts — one trend per takeaway",
         "Chart_Data sheet (from the same pulls as the evidence sheets)",
         "2026 YTD",
         "Indexed series share a stated base (never dual-axis); weekly "
         "series are Mon-Sat weeks; Sep-29+ contaminated; Oct partial; "
         "CR45 Feb gap = campaign dark (real)",
         takeaway="Read top to bottom — the charts follow the argument: "
         "T0 why ethos matters -> T1 the decline splits in two -> T2 the "
         "demand surge and its two doors -> T3 our levers -> T5 the IS "
         "diagnosis -> T6 why Google missed it -> T7 the fixable part.")

    t26 = t1[t1["month"] >= "2026-01"].copy()
    chart_specs = []  # (heading, block_df, y_title, ref_styles, skip)

    # T0 — why ethos is the story: its share of account spend & leads
    es = pd.read_sql_query(f"""
        SELECT SUBSTR(p.report_date,1,7) AS month,
               CASE WHEN LOWER(ki.keyword_text) LIKE '%ethos%'
                    THEN 'ethos' ELSE 'rest' END AS grp,
               SUM(p.cost_micros)/1e6 AS spend,
               SUM(p.primary_conv_count) AS leads
        FROM keyword_performance_daily p
        JOIN keyword_instances ki ON ki.id=p.instance_id
        WHERE ki.account_id={ACCOUNT_ID} AND p.device='ALL'
          AND p.report_date>='2026-01-01'
        GROUP BY 1,2""", con)
    piv_sp = es.pivot_table(index="month", columns="grp", values="spend",
                            aggfunc="sum").fillna(0)
    piv_ld = es.pivot_table(index="month", columns="grp", values="leads",
                            aggfunc="sum").fillna(0)
    b = pd.DataFrame({
        "month": piv_sp.index,
        "Ethos % of account spend":
            (100 * piv_sp.get("ethos", 0) / piv_sp.sum(axis=1)).values,
        "Ethos % of account routed leads":
            (100 * piv_ld.get("ethos", 0) / piv_ld.sum(axis=1)).values,
    })
    chart_specs.append((
        "T0 — Why ethos is the story: ethos keywords' share of account "
        "spend and routed leads climbs to ~78% by Sep — the account "
        "concentrated INTO ethos as quality declined, so account KPI "
        "increasingly IS ethos KPI", b, "% of account total", None, 1))

    # T1a — the KPI path against the goalposts
    b = t26[["month", "policy_kpi"]].copy()
    b.columns = ["month", "Policy KPI"]
    b["Target 4.0"] = 4.0
    b["Break-even 2.96"] = 2.96
    chart_specs.append((
        "T1 — Policy KPI, monthly 2026 (Oct = MTD, contaminated)",
        b, "Policy KPI (prem / payment)", {1: "dash", 2: "sysDot"}, 1))

    # T1b — the split: quality vs rate, indexed Jan-26 = 100
    bq, br = t26.iloc[0]["prem_per_op"], t26.iloc[0]["per_op"]
    b = pd.DataFrame({
        "month": t26["month"],
        "Quality: prem/op (Jan=100)": 100 * t26["prem_per_op"] / bq,
        "Rate: SQ $/op (Jan=100)": 100 * t26["per_op"] / br})
    chart_specs.append((
        "T1 — The split: June's rate step vs the Jun-Sep quality slide "
        "(indexed, Jan 2026 = 100)",
        b, "Index (Jan 2026 = 100)", None, 1))

    # T2a — demand vs eligible vs impressions (CR12, weekly, indexed)
    if trends_ok and len(merged):
        m26w = merged[pd.to_datetime(merged["week"])
                      >= "2026-01-01"].copy()
        b = pd.DataFrame({"week": m26w["week"].astype(str)})
        for col_, label in [("demand", "Demand (Trends)"),
                            ("eligible", "Eligible auctions"),
                            ("impressions", "Impressions served")]:
            base = merged.head(4)[col_].mean()
            b[label] = 100 * m26w[col_].values / base if base else np.nan
        chart_specs.append((
            "T2 — The denominator story: demand and eligible auctions "
            "explode in Sep while served impressions lag (CR12 weekly, "
            "indexed to Oct-2025 base)",
            b, "Index (first 4 overlap wks = 100)", None, 4))

    # T2b — category vs brand divergence
    b = CAT_VS_BRAND.copy()
    b.columns = ["month", "Category: 'term life insurance'",
                 "Brand: 'ethos'"]
    chart_specs.append((
        "T2 — Manufactured demand: Jul-Oct the category FALLS while the "
        "brand RISES (each Trends index normalized to its own pull; "
        "compare direction, not level)",
        b, "Trends index (per-pull peak = 100)", None, 1))

    # T2c — the two doors: weekly query-tail share, CR3 vs CR12.
    # CR3's tail opens mid-June (the visible broad valve); CR12 stays
    # clean through Jul-Aug while quality fell anyway (the invisible
    # same-query door, provable only by this contrast); CR12's Sep jump
    # is the bought expansion.
    tq2 = pd.read_sql_query(f"""
        SELECT p.report_date AS date, p.campaign_id, p.query_id,
               SUM(p.impressions) AS impr
        FROM search_query_performance p
        JOIN keyword_instances ki ON ki.id=p.instance_id
                                  AND ki.account_id={ACCOUNT_ID}
        WHERE p.platform='google' AND p.device='ALL'
          AND p.report_date>='2026-01-01'
          AND LOWER(ki.keyword_text) LIKE '%ethos%'
          AND p.campaign_id IN ({CAMPS['CR12']},{CAMPS['CR3']})
        GROUP BY 1,2,3""", con)
    tq2["week"] = week_start(tq2["date"]).astype(str)
    tq2["cid_s"] = tq2["campaign_id"].astype(str)
    b = pd.DataFrame({"week": sorted(tq2["week"].unique())})
    for nm in ["CR12", "CR3"]:
        sub = tq2[tq2["cid_s"] == str(CAMPS[nm])]
        rows_ = []
        for wk_, g in sub.groupby("week"):
            q = g.groupby("query_id")["impr"].sum() \
                 .sort_values(ascending=False)
            ti = q.sum()
            rows_.append({"week": wk_,
                          f"{nm} query-tail %":
                          (100 * q.iloc[10:].sum() / ti
                           if len(q) > 10 else 0.0)})
        b = b.merge(pd.DataFrame(rows_), on="week", how="left")
    chart_specs.append((
        "T2 — The two doors: ethos query-tail share (impressions beyond "
        "each week's top-10 queries). CR3's valve opens mid-June; CR12 "
        "stays clean Jul-Aug while quality fell (= same-query dilution), "
        "then jumps in Sep (bought expansion)",
        b, "% of ethos impressions in tail queries", None, 4))

    # T3a/T3b — the lever timeline and what it cost
    wk26 = [w_ for w_ in panels["CR12"]["week"]
            if str(w_) >= "2026-01-01"]
    levers = {nm: lever_series(con, cid) for nm, cid in CAMPS.items()}
    b = pd.DataFrame({"week": [str(w_) for w_ in wk26]})
    for nm in ["CR12", "CR3", "CR45"]:
        s = levers[nm].get("budget_hist")
        b[f"{nm} budget $/day"] = ([s.get(w_) if s is not None else np.nan
                                    for w_ in wk26])
    chart_specs.append((
        "T3 — The lever timeline: drift down, Aug 17-31 starvation, "
        "Sep 10-22 whiplash (end-of-week daily budget, from change "
        "history)", b, "Daily budget ($)", None, 4))

    bl = None
    for nm in ["CR12", "CR3", "CR45"]:
        p_ = panels[nm][["week", "budget_lost_%"]].rename(
            columns={"budget_lost_%": f"{nm} lost-to-budget %"})
        bl = p_ if bl is None else bl.merge(p_, on="week", how="outer")
    bl = bl[bl["week"].astype(str) >= "2026-01-01"].sort_values("week")
    bl["week"] = bl["week"].astype(str)
    chart_specs.append((
        "T3 — What starvation looked like: budget-lost IS spikes to "
        "44-50% on CR3/45 in late Aug, zero after the raises",
        bl, "Lost to budget (% of eligible)", None, 4))

    # T5 — IS anatomy (CR12)
    p12 = panels["CR12"]
    b = p12[p12["week"].astype(str) >= "2026-01-01"][
        ["week", "IS_%", "budget_lost_%", "rank_lost_%"]].copy()
    b.columns = ["week", "Impression share %", "Lost to budget %",
                 "Lost to rank %"]
    b["week"] = b["week"].astype(str)
    chart_specs.append((
        "T5 — The post-Labor-Day IS drop, diagnosed: share falls as the "
        "denominator explodes; rank-lost flat, budget-lost spikes then "
        "zeroes after the Sep 11 raise (CR12 weekly)",
        b, "% of eligible auctions", None, 4))

    # T6 — Google is blind to the quality event
    vl = pd.read_sql_query("""
        SELECT SUBSTR(date,1,7) AS month,
               SUM(conversion_value)/SUM(conversions) AS vpl
        FROM campaign_performance_daily
        WHERE account_id=? AND device='ALL' AND date>='2026-01-01'
        GROUP BY 1""", con, params=(ACCOUNT_ID,))
    b = t26[["month", "prem_per_op"]].merge(vl, on="month", how="left")
    jn = b[b["month"] == "2026-06"].iloc[0]
    b = pd.DataFrame({
        "month": b["month"],
        "SQ premium/op (Jun=100)": 100 * b["prem_per_op"]
        / jn["prem_per_op"],
        "Google value/lead (Jun=100)": 100 * b["vpl"] / jn["vpl"]})
    chart_specs.append((
        "T6 — Why Google metrics missed it: Google value/lead holds "
        "flat while SQ premium/op falls 34% (indexed, Jun 2026 = 100)",
        b, "Index (Jun 2026 = 100)", None, 1))

    # T7 — the broad valve
    bs = pd.read_sql_query(f"""
        SELECT p.report_date AS date, p.campaign_id,
               SUM(CASE WHEN p.match_type_served='BROAD'
                   THEN p.impressions ELSE 0 END) AS broad_impr,
               SUM(p.impressions) AS impr
        FROM search_query_performance p
        JOIN keyword_instances ki ON ki.id=p.instance_id
                                  AND ki.account_id={ACCOUNT_ID}
        WHERE p.platform='google' AND p.device='ALL'
          AND p.report_date>='2026-01-01'
          AND LOWER(ki.keyword_text) LIKE '%ethos%'
          AND p.campaign_id IN ({CAMPS['CR12']},{CAMPS['CR3']})
        GROUP BY 1,2""", con)
    bs["week"] = week_start(bs["date"]).astype(str)
    bs["cid_s"] = bs["campaign_id"].astype(str)
    b = pd.DataFrame({"week": sorted(bs["week"].unique())})
    for nm in ["CR12", "CR3"]:
        sub = bs[bs["cid_s"] == str(CAMPS[nm])].groupby(
            "week", as_index=False).agg(bi=("broad_impr", "sum"),
                                        ti=("impr", "sum"))
        sub[f"{nm} broad-served %"] = 100 * sub["bi"] / sub["ti"]
        b = b.merge(sub[["week", f"{nm} broad-served %"]],
                    on="week", how="left")
    if len(b.columns) < 3:
        print("  !! T7 broad-share block came up short — inspect "
              "sqp campaign_id values.")
    chart_specs.append((
        "T7 — The recoverable part: broad-served share of ethos "
        "impressions — CR3's standing valve vs CR12's Sep explosion "
        "(the match-tightening target)",
        b, "% of ethos impressions broad-served", None, 4))

    # write blocks + charts
    col, anchor_row = 1, 5
    for heading, bdf, y_title, refs, skip in chart_specs:
        n = write_series_block(ws_cd, col, bdf)
        hc = ws_tc.cell(row=anchor_row, column=1, value=heading)
        hc.font = Font(bold=True, size=11)
        hc.fill = PatternFill("solid", fgColor=LIGHT)
        ws_tc.add_chart(
            make_line_chart(ws_cd, col, len(bdf.columns) - 1, n,
                            heading.split(" — ")[0] + ": "
                            + heading.split(" — ")[1][:80],
                            y_title, ref_styles=refs, skip=skip),
            f"A{anchor_row + 1}")
        col += len(bdf.columns) + 1
        anchor_row += 22
    ws_cd.cell(row=1, column=col,
               value="Chart source blocks for Takeaway_Charts; "
                     "same pulls as the evidence sheets.")
    # Plumbing, not evidence: hide it (never delete — every chart
    # references it; deleting would break all 11 with #REF errors).
    ws_cd.sheet_state = "hidden"

    wb.save(OUT_XLSX)

    # ------------------------------------------------- console summary
    print("=" * 100)
    print("P3 DECOMPOSITION — console summary")
    print("=" * 100)
    print(dec.to_string(index=False))
    print(f"\nLuminary carve-out (CR12 Sep): ${lum_spend:,.0f}, "
          f"{lum_leads:.0f} leads")
    print("\nHEAD QUERIES — top 10 ethos queries, Sep 2026 (account):")
    print(head_tbl.round(1).to_string(index=False))
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
