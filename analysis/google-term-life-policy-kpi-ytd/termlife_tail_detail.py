#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
termlife_tail_detail.py  (v1)
=============================
Follow-up to the YTD analysis: the query tail, made concrete.
Term Life (account_id=29), ethos keywords, monthly Jun-Sep 2026.
READ-ONLY vs paid_search.db; writes output\\Tail_Detail.xlsx + console.

Answers:
  A  Tail expansion summary by month x campaign — tail % of ethos
     impressions, and the tail decomposed by HOW it was served
     (broad / close variant / phrase / exact).
  B  September head vs tail x match-type-served matrix (account).
  C  The tail, literally: top 20 September tail queries with text,
     serving match type, impressions, clicks, leads.

Definitions (consistent with the main workbook):
  tail = impressions on queries ranked 11+ by impressions within the
  month (per campaign for A; account-wide for B/C). Denominator =
  ethos-keyword sqp impressions only. Close variant = NEAR_EXACT +
  NEAR_PHRASE (Google matching variants of exact/phrase keywords —
  cannot be disabled, only negated). sqp hides low-volume queries
  (all tail by definition), so every tail figure is a LOWER BOUND.
"""

import os
import sqlite3
import numpy as np
import pandas as pd
from openpyxl import Workbook
from openpyxl.chart import LineChart, Reference
from openpyxl.chart.layout import Layout, ManualLayout
from openpyxl.drawing.text import RichTextProperties
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

DB_PATH = r"C:\Projects\google-ads-ops\db\paid_search.db"
OUT_XLSX = (r"C:\Projects\google-ads-ops\analysis"
            r"\google-term-life-policy-kpi-ytd\output\Tail_Detail.xlsx")

ACCOUNT_ID = 29
CAMPS = {"CR12": 22899566624, "CR3": 23075071366, "CR45": 22849317525}
MONTHS = ["2026-06", "2026-07", "2026-08", "2026-09"]
NAVY, LIGHT = "1F3864", "D9E2F2"

GROUP = {"BROAD": "broad", "NEAR_EXACT": "close_variant",
         "NEAR_PHRASE": "close_variant", "PHRASE": "phrase",
         "EXACT": "exact", "UNKNOWN": "other"}
SERIES_HEX = ["2A78D6", "EB6834", "1BAF7A"]
REF_HEX = "898781"


def week_start(s):
    d = pd.to_datetime(s)
    return (d - pd.to_timedelta(d.dt.dayofweek % 7, unit="D")).dt.date


def write_series_block(ws, start_col, df):
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
                    skip=4):
    ch = LineChart()
    ch.title = title
    data = Reference(ws_data, min_col=start_col + 1,
                     max_col=start_col + n_series,
                     min_row=1, max_row=n_rows + 1)
    cats = Reference(ws_data, min_col=start_col, min_row=2,
                     max_row=n_rows + 1)
    ch.add_data(data, titles_from_data=True)
    ch.set_categories(cats)
    if ch.title is not None:
        ch.title.overlay = False
    ch.legend.position = "r"
    ch.legend.overlay = False
    ch.y_axis.title = y_title
    try:
        ch.y_axis.title.tx.rich.bodyPr = RichTextProperties(
            rot=-5400000, vert="horz")
    except AttributeError:
        pass
    xn = ws_data.cell(row=1, column=start_col).value
    if xn:
        ch.x_axis.title = str(xn)
    ch.x_axis.delete = False
    ch.y_axis.delete = False
    ch.x_axis.tickLblSkip = skip
    ch.x_axis.tickMarkSkip = skip
    ch.layout = Layout(manualLayout=ManualLayout(
        layoutTarget="inner", xMode="edge", yMode="edge",
        x=0.10, y=0.14, w=0.72, h=0.66))
    ch.height = 9.5
    ch.width = 30
    for i, s in enumerate(ch.series):
        s.smooth = False
        s.graphicalProperties.line.width = 20000
        s.graphicalProperties.line.solidFill = SERIES_HEX[i % 3]
    return ch

pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 40)
pd.set_option("display.max_rows", 200)
pd.set_option("display.float_format", lambda x: f"{x:,.1f}")


def hr(t):
    print("\n" + "=" * 110)
    print(t)
    print("=" * 110)


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
                v = None if pd.isna(v) else round(float(v), 1)
            elif isinstance(v, (np.integer,)):
                v = int(v)
            ws.cell(row=row, column=j, value=v)
        row += 1
    for j, c in enumerate(df.columns, start=1):
        width = max([len(str(c))]
                    + [len(str(x)) for x in df[c].head(60)])
        ws.column_dimensions[get_column_letter(j)].width = min(width + 2, 50)
    return row + 2


def main():
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)

    has_sq = con.execute("SELECT 1 FROM sqlite_master "
                         "WHERE name='search_queries'").fetchone()
    q_sel = ("COALESCE(q.query_text, CAST(p.query_id AS TEXT))"
             if has_sq else "CAST(p.query_id AS TEXT)")
    q_join = ("LEFT JOIN search_queries q ON q.id=p.query_id"
              if has_sq else "")

    base = pd.read_sql_query(f"""
        SELECT SUBSTR(p.report_date,1,7) AS month, p.campaign_id,
               p.query_id, {q_sel} AS query, p.match_type_served,
               SUM(p.impressions) AS impr, SUM(p.clicks) AS clicks,
               SUM(p.cost_micros)/1e6 AS spend,
               SUM(p.primary_conv_count) AS leads
        FROM search_query_performance p
        JOIN keyword_instances ki ON ki.id=p.instance_id
                                  AND ki.account_id={ACCOUNT_ID}
        {q_join}
        WHERE p.platform='google' AND p.device='ALL'
          AND p.report_date>='2026-06-01' AND p.report_date<'2026-10-01'
          AND LOWER(ki.keyword_text) LIKE '%ethos%'
        GROUP BY 1,2,3,4,5
    """, con)
    base["cid_s"] = base["campaign_id"].astype(str)
    rev = {str(v): k for k, v in CAMPS.items()}
    base["camp"] = base["cid_s"].map(rev).fillna("OTHER")
    base["mgrp"] = base["match_type_served"].map(GROUP).fillna("other")

    def tail_flag(df, keys):
        """Mark tail rows: query rank 11+ by impressions within keys."""
        qt = df.groupby(keys + ["query_id"], as_index=False)["impr"].sum()
        qt["rank"] = qt.groupby(keys)["impr"].rank(method="first",
                                                   ascending=False)
        qt["is_tail"] = qt["rank"] > 10
        return df.merge(qt[keys + ["query_id", "is_tail"]],
                        on=keys + ["query_id"], how="left")

    # ------------------------------------------------------------- A
    hr("A  TAIL EXPANSION BY MONTH x CAMPAIGN — and HOW the tail was "
       "served")
    rows = []
    for scope, df_s in ([("ACCOUNT", base)]
                        + [(c, base[base["camp"] == c])
                           for c in ["CR12", "CR3", "CR45"]]):
        d = tail_flag(df_s.copy(), ["month"])
        for m, g in d.groupby("month"):
            tot = g["impr"].sum()
            tot_sp = g["spend"].sum()
            tot_ld = g["leads"].sum()
            t = g[g["is_tail"]]
            ti = t["impr"].sum()
            t_sp = t["spend"].sum()
            t_ld = t["leads"].sum()
            mg = t.groupby("mgrp")["impr"].sum()
            rows.append({
                "scope": scope, "month": m,
                "ethos_kw_impr": int(tot),
                "distinct_queries": g["query_id"].nunique(),
                "tail_impr": int(ti),
                "tail_%_of_ethos_kw_impr": 100 * ti / tot if tot else 0,
                "tail_%_of_kw_spend": 100 * t_sp / tot_sp
                if tot_sp else 0,
                "tail_%_of_kw_leads": 100 * t_ld / tot_ld
                if tot_ld else 0,
                "broad-served (% of tail)": 100 * mg.get("broad", 0) / ti
                if ti else 0,
                "close-variant (% of tail)": 100 * mg.get("close_variant", 0)
                / ti if ti else 0,
                "phrase-served (% of tail)": 100 * mg.get("phrase", 0) / ti
                if ti else 0,
                "exact-served (% of tail)": 100 * mg.get("exact", 0) / ti
                if ti else 0,
                "tail_spend": round(float(t_sp), 0),
                "tail_leads": round(float(t_ld), 1),
                "tail_CPL": round(t_sp / t_ld, 0) if t_ld else None,
            })
    A = pd.DataFrame(rows).sort_values(["scope", "month"])
    print(A.to_string(index=False))
    print("\n  Reading: 'close variant' = Google serving a variant of an "
          "exact/phrase keyword (NEAR_EXACT/NEAR_PHRASE) — cannot be "
          "turned off, only negated. Broad-served tail responds to match "
          "tightening AND negatives.")

    # ------------------------------------------------------------- B
    hr("B  SEPTEMBER: HEAD vs TAIL x MATCH-TYPE-SERVED (account, ethos)")
    sep = tail_flag(base[base["month"] == "2026-09"].copy(), ["month"])
    sep["segment"] = np.where(sep["is_tail"], "TAIL (rank 11+)",
                              "HEAD (top 10)")
    piv = sep.pivot_table(index="segment", columns="mgrp", values="impr",
                          aggfunc="sum").fillna(0)
    piv["total_impr"] = piv.sum(axis=1)
    pct = (100 * piv.div(piv["total_impr"], axis=0)).round(1)
    pct["total_impr"] = piv["total_impr"].astype(int)
    B = pct.reset_index()
    print(B.to_string(index=False))

    def share_table(frame, bycol, label):
        tbl = frame.groupby(bycol, as_index=False).agg(
            impr=("impr", "sum"), clicks=("clicks", "sum"),
            spend=("spend", "sum"), leads=("leads", "sum"))
        for c in ["impr", "spend", "leads"]:
            s = tbl[c].sum()
            tbl[f"{c}_%"] = (100 * tbl[c] / s).round(1) if s else 0
        tbl["CPL"] = (tbl["spend"] / tbl["leads"]).round(0)
        tbl["spend"] = tbl["spend"].round(0)
        tbl["leads"] = tbl["leads"].round(1)
        cols = [bycol, "impr", "impr_%", "spend", "spend_%", "leads",
                "leads_%", "CPL"]
        print(f"\n  {label}:")
        print(tbl[cols].to_string(index=False))
        return tbl[cols]

    B2 = share_table(sep, "segment",
                     "B2 — Sep head vs tail: shares of impressions / "
                     "spend / leads + CPL (account, ethos keywords)")
    B3 = share_table(sep, "mgrp",
                     "B3 — Sep by match-type-served (ALL ethos-kw "
                     "traffic, head+tail): shares + CPL")
    print("\n  NOTE: leads = Google-routed primary conversions at query "
          "grain (sqp hides ~15%; suppressed slice is all tail -> tail "
          "shares are lower bounds). Shares size volume and cost — "
          "premium QUALITY per slice remains unmeasurable below "
          "campaign x month.")

    # ------------------------------------------------------------- C
    hr("C  TAIL COMPOSITION BY QUERY TEXT — ethos long-tails vs OFF-BRAND "
       "queries the ethos keywords wandered onto (account, monthly)")
    d_acc = tail_flag(base.copy(), ["month"])
    dt = d_acc[d_acc["is_tail"]].copy()
    dt["qtype"] = np.where(dt["query"].str.lower()
                           .str.contains("ethos", na=False),
                           "ethos-text", "off-brand")
    rows = []
    for m, g in dt.groupby("month"):
        ti = g["impr"].sum()
        mtot = d_acc[d_acc["month"] == m]
        m_sp, m_ld = mtot["spend"].sum(), mtot["leads"].sum()
        for qt, gg in g.groupby("qtype"):
            br = gg.loc[gg["mgrp"] == "broad", "impr"].sum()
            cv = gg.loc[gg["mgrp"] == "close_variant", "impr"].sum()
            g_sp, g_ld = gg["spend"].sum(), gg["leads"].sum()
            rows.append({
                "month": m, "tail_slice": qt,
                "distinct_q": gg["query_id"].nunique(),
                "impr": int(gg["impr"].sum()),
                "%_of_tail": 100 * gg["impr"].sum() / ti if ti else 0,
                "broad-served_%": 100 * br / gg["impr"].sum()
                if gg["impr"].sum() else 0,
                "close-variant_%": 100 * cv / gg["impr"].sum()
                if gg["impr"].sum() else 0,
                "spend": round(float(g_sp), 0),
                "%_of_kw_spend": 100 * g_sp / m_sp if m_sp else 0,
                "leads": round(float(g_ld), 1),
                "%_of_kw_leads": 100 * g_ld / m_ld if m_ld else 0,
                "CPL": round(g_sp / g_ld, 0) if g_ld else None,
            })
    C1 = pd.DataFrame(rows).sort_values(["month", "tail_slice"])
    print(C1.to_string(index=False))
    print("\n  Reading: 'off-brand' = the query text never mentions "
          "ethos — broad/close-variant matching carried an ethos keyword "
          "onto someone else's search (other carrier brands, generic "
          "life-insurance terms). Fully negatable. 'ethos-text' = the "
          "genuine ethos long-tail (reviews/legit/cancel/cost "
          "variants).")

    # ------------------------------------------------- C2/C3: examples
    sep_t = dt[dt["month"] == "2026-09"]

    def top_list(frame, n=25):
        return (frame.groupby(["query", "match_type_served"],
                              as_index=False)
                .agg(impressions=("impr", "sum"),
                     clicks=("clicks", "sum"), spend=("spend", "sum"),
                     leads=("leads", "sum"))
                .sort_values("impressions", ascending=False).head(n))

    hr("C2  TOP 25 SEPTEMBER TAIL QUERIES — ETHOS LONG-TAILS "
       "(query text contains 'ethos')")
    C2 = top_list(sep_t[sep_t["qtype"] == "ethos-text"])
    print(C2.to_string(index=False) if len(C2) else "  (none)")

    hr("C3  TOP 25 SEPTEMBER TAIL QUERIES — OFF-BRAND "
       "(no 'ethos' in the query)")
    C3 = top_list(sep_t[sep_t["qtype"] == "off-brand"])
    print(C3.to_string(index=False) if len(C3) else "  (none)")

    n_tail_q = sep.loc[sep["is_tail"], "query_id"].nunique()
    print(f"\n  ...out of {n_tail_q:,} distinct September tail queries "
          f"(sqp-visible; suppressed slice hidden by Google is all tail "
          f"too).")

    # ------------------------------------------------------------- D
    hr("D  WEEKLY TAIL DECOMPOSITION — the T2c lines, split into ethos "
       "long-tail vs OFF-BRAND (2026 YTD, CR12 & CR3)")
    wk = pd.read_sql_query(f"""
        SELECT p.report_date AS date, p.campaign_id, p.query_id,
               {q_sel} AS query, SUM(p.impressions) AS impr
        FROM search_query_performance p
        JOIN keyword_instances ki ON ki.id=p.instance_id
                                  AND ki.account_id={ACCOUNT_ID}
        {q_join}
        WHERE p.platform='google' AND p.device='ALL'
          AND p.report_date>='2026-01-01'
          AND LOWER(ki.keyword_text) LIKE '%ethos%'
          AND p.campaign_id IN ({CAMPS['CR12']},{CAMPS['CR3']})
        GROUP BY 1,2,3,4
    """, con)
    wk["week"] = week_start(wk["date"]).astype(str)
    wk["camp"] = wk["campaign_id"].astype(str).map(rev)
    wk["qtype"] = np.where(wk["query"].str.lower()
                           .str.contains("ethos", na=False),
                           "ethos-text", "off-brand")

    d_blocks = {}
    for camp in ["CR12", "CR3"]:
        sub = wk[wk["camp"] == camp]
        qsum = sub.groupby(["week", "query_id"], as_index=False)[
            "impr"].sum()
        qsum["rank"] = qsum.groupby("week")["impr"].rank(
            method="first", ascending=False)
        qsum["is_tail"] = qsum["rank"] > 10
        s2 = sub.merge(qsum[["week", "query_id", "is_tail"]],
                       on=["week", "query_id"], how="left")
        tot = s2.groupby("week")["impr"].sum()
        tail = s2[s2["is_tail"]]
        pv = tail.pivot_table(index="week", columns="qtype",
                              values="impr", aggfunc="sum").fillna(0)
        out = pd.DataFrame({"week": tot.index})
        out[f"{camp} ethos long-tail %"] = (
            100 * pv.reindex(tot.index).get(
                "ethos-text", pd.Series(0, index=tot.index)).fillna(0)
            / tot).values
        out[f"{camp} off-brand tail %"] = (
            100 * pv.reindex(tot.index).get(
                "off-brand", pd.Series(0, index=tot.index)).fillna(0)
            / tot).values
        d_blocks[camp] = out.reset_index(drop=True)
        print(f"\n  {camp} — weekly tail % of ethos impressions, split "
              f"(the two columns sum to the T2c tail line):")
        print(out.round(1).to_string(index=False))

    # ------------------------------------------------------------- xlsx
    os.makedirs(os.path.dirname(OUT_XLSX), exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Tail_Detail"
    meta(ws, "Query tail, made concrete — Jun-Sep 2026, ethos keywords",
         "search_query_performance (platform=google, device=ALL) + "
         "search_queries",
         "2026-06..2026-09; tail = query rank 11+ by impressions within "
         "the month",
         "SCOPE: every row in this file — including the off-brand "
         "queries — is traffic served by keywords whose text contains "
         "'ethos'; the match-type columns describe HOW the ethos keyword "
         "matched each query; nothing from the rest of the keyword "
         "portfolio is included. sqp hides low-volume queries (all tail) "
         "— every tail figure is a LOWER bound; Sep 29+ contaminated "
         "(schedule-cut incident); close variant = NEAR_EXACT/"
         "NEAR_PHRASE, cannot be disabled, only negated",
         takeaway="Most of the Sep tail expansion was a RESULT OF OUR OWN "
         "Sep 10-22 changes, not a shift in searcher behavior: the "
         "budget raises + tROAS cuts let Smart Bidding expand broad/"
         "close-variant matching — off-brand queries went from ~0% to "
         "13-15% of CR12 impressions within weeks of the raise "
         "(blocks A, C; weekly charts). Broad-served tail -> match "
         "tightening + negatives; close-variant tail -> negatives "
         "only (cannot be disabled).")
    r = write_block(ws, 5, "A — Tail expansion by month x campaign. Two "
                    "denominators: tail_%_of_ethos_kw_impr = tail's "
                    "share of ALL ethos-keyword impressions (head+"
                    "tail); the four serving columns = shares OF THE "
                    "TAIL itself and sum to ~100%. All data = ethos-"
                    "keyword traffic only.", A)
    r = write_block(ws, r, "B — September: head vs tail x match type "
                    "served (% of row; both rows = ethos-keyword "
                    "impressions, account)", B)
    r = write_block(ws, r, "B2 — September head vs tail: shares of "
                    "impressions / spend / leads + CPL (account). Leads "
                    "size VOLUME, not premium quality.", B2)
    r = write_block(ws, r, "B3 — September by match-type-served, ALL "
                    "ethos-kw traffic (head+tail): shares + CPL", B3)
    r = write_block(ws, r, "C — Tail composition: ethos long-tails vs "
                    "off-brand queries, monthly (account)", C1)
    r = write_block(ws, r, "C2 — Top 25 Sep tail queries: ETHOS "
                    "long-tails", C2)
    write_block(ws, r, "C3 — Top 25 Sep tail queries: OFF-BRAND — query "
                "text has no 'ethos' but each was SERVED BY an ethos "
                "keyword (fully negatable)", C3)

    # Tail charts sheet: T2c's lines, decomposed
    ws_tc = wb.create_sheet("Tail_Charts")
    meta(ws_tc, "The two doors, decomposed — weekly tail split: ethos "
         "long-tail vs off-brand",
         "sqp weekly (same methodology as the main workbook's T2c chart)",
         "2026 YTD (Mon-Sat weeks)",
         "SCOPE: all series = traffic served by ethos keywords only "
         "(off-brand = query text without 'ethos', still served BY an "
         "ethos keyword). Both components are % of that campaign's "
         "ethos-keyword sqp impressions and sum to the T2c tail line; "
         "tail figures are lower bounds (suppression); Sep-29+ "
         "contaminated",
         takeaway="The same T2c tail lines, split by what the tail "
         "queries actually say: ethos long-tails (brand curiosity "
         "arriving with the surge) vs off-brand queries (broad/close-"
         "variant matching leaving the brand — fully negatable).")
    ws_cd = wb.create_sheet("Chart_Data_T")
    col, anchor = 1, 5
    for camp in ["CR12", "CR3"]:
        bdf = d_blocks[camp]
        n = write_series_block(ws_cd, col, bdf)
        hc = ws_tc.cell(row=anchor, column=1,
                        value=f"{camp} — weekly tail % split: ethos "
                              f"long-tail vs off-brand")
        hc.font = Font(bold=True, size=11)
        hc.fill = PatternFill("solid", fgColor=LIGHT)
        ws_tc.add_chart(
            make_line_chart(ws_cd, col, len(bdf.columns) - 1, n,
                            f"{camp}: tail composition, weekly",
                            "% of ethos impressions"),
            f"A{anchor + 1}")
        col += len(bdf.columns) + 1
        anchor += 22
    ws_cd.sheet_state = "hidden"

    wb.save(OUT_XLSX)
    print(f"\nWorkbook written: {OUT_XLSX}")
    print("Done. Paste console output back to the analysis thread.")
    con.close()


if __name__ == "__main__":
    main()
