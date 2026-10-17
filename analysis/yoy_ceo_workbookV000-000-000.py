#!/usr/bin/env python3
r"""
yoy_ceo_workbook.py -- Google Home YoY "what worked" CEO workbook

Exec page per window (YTD, Q2, July -- matched spans both years):
  headline cards (GP LY / GP TY / gap), Panel A GP bridge waterfall,
  Panel B GP by brand LY vs TY, Panel C GP by match type + query group.
Appendix tabs: Brands detail (with bid_status + bucket), Query groups,
Match types, Leak audit, Notes (reconciliation + methodology).

Bridge buckets (each dollar lands in exactly one; sums to the GP gap):
  1. now-restricted brands (bid_status='restricted')      -- full dGP; policy, unrecoverable
  2. State Farm (conditional, recoverable per Gilbert)    -- full dGP; the ask
  3. approved/conditional brands effectively exited       -- full dGP; TY spend < EXIT_THRESHOLD x LY spend
  4. continuing book (everything else, incl. generic/agg) -- dGP decomposed exactly:
       payout (RPL), price (CPC), conversion (CvR), volume+interaction

Sources: search_query_performance x query_classification (demand-true; Panels A/B,
query groups) and keyword_performance_daily x keyword_classification (match type,
leak audit). brand_registry.bid_status joined by brand_name -- brands resolved only
via the persisted brand_matcher-canonical classifications; never re-matched here.
Registry brand_name rows with conflicting bid_status are reported and routed back
to the DB architect (per guidance) -- the script does not guess.

USAGE
  python yoy_ceo_workbook.py --db "C:\...\paid_search.db"
      [--as-of 2026-07-20] [--funnel-xlsx google_home_funnel_analysis.xlsx]
      [--output google_home_yoy_ceo_<asof>.xlsx]

--funnel-xlsx adds a Looker-topline reconciliation row on the Notes tab.
Footnotes carried on every exec page: bid_status is CURRENT state ("brands now
restricted" -- some restrictions landed mid-history); platform data runs slightly
below Looker due to attribution; exit threshold documented.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import sys
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference, Series
from openpyxl.chart.axis import ChartLines
from openpyxl.chart.series import DataPoint
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

EXIT_THRESHOLD = 0.10   # TY spend < 10% of LY spend => "effectively exited"
RECOVERABLE = {"State Farm"}          # per Gilbert 2026-07-24: only State Farm

F_TITLE = Font(name="Arial", size=15, bold=True, color="1F3864")
F_H2    = Font(name="Arial", size=12, bold=True, color="1F3864")
F_SUB   = Font(name="Arial", size=10, italic=True, color="595959")
F_HDR   = Font(name="Arial", size=10, bold=True, color="FFFFFF")
F_BODY  = Font(name="Arial", size=10)
F_BOLD  = Font(name="Arial", size=10, bold=True)
F_NOTE  = Font(name="Arial", size=9, italic=True, color="7F7F7F")
F_CARD_L = Font(name="Arial", size=10, color="595959")
F_CARD_V = Font(name="Arial", size=18, bold=True, color="1F3864")
FILL_HDR = PatternFill("solid", fgColor="1F3864")
FILL_CARD = PatternFill("solid", fgColor="DEEAF6")
FILL_GAP = PatternFill("solid", fgColor="FBE4E4")
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

C_END   = "808080"   # bridge endpoints
C_POLICY = "F09595"  # restricted / exits (lighter red)
C_RED   = "C00000"   # operational negatives
C_GREEN = "70AD47"
C_LY    = "8496B0"
C_TY    = "2E75B6"


# ---------------------------------------------------------------- helpers
def write_table(ws, row, col, headers, data, fmts=None, widths=None):
    for j, h in enumerate(headers):
        c = ws.cell(row=row, column=col + j, value=h)
        c.font, c.fill, c.border = F_HDR, FILL_HDR, BORDER
        c.alignment = Alignment(horizontal="center", wrap_text=True)
    first = row + 1
    for i, r in enumerate(data):
        for j, v in enumerate(r):
            if isinstance(v, float) and (v != v or abs(v) == float("inf")):
                v = None
            c = ws.cell(row=first + i, column=col + j, value=v)
            c.font, c.border = F_BODY, BORDER
            if fmts and fmts[j]:
                c.number_format = fmts[j]
    if widths:
        for j, w in enumerate(widths):
            L = get_column_letter(col + j)
            ws.column_dimensions[L].width = max(ws.column_dimensions[L].width or 0, w)
    return first, first + len(data) - 1


def style_chart(ch, title, y_title=None, height=8.5, width=15):
    ch.title = title
    ch.height, ch.width = height, width
    ch.style = 2
    if y_title:
        ch.y_axis.title = y_title
    ch.y_axis.majorGridlines = ChartLines()
    return ch


def windows(asof: dt.date):
    y, py = asof.year, asof.year - 1
    return [
        ("YTD",  dt.date(py, 1, 1), asof.replace(year=py), dt.date(y, 1, 1), asof),
        ("Q2",   dt.date(py, 4, 1), dt.date(py, 6, 30), dt.date(y, 4, 1), dt.date(y, 6, 30)),
        ("July", dt.date(py, 7, 1), asof.replace(year=py), dt.date(y, 7, 1), asof),
    ]


# ---------------------------------------------------------------- data pulls
def query_pull(conn, lo, hi) -> pd.DataFrame:
    """Query-side segment rollup for one span (brand grain, off-vertical excluded)."""
    df = pd.read_sql_query("""
        SELECT qc.category, qc.brand_name AS brand, qc.query_group, qc.product_code AS product,
               SUM(p.impressions) AS imps, SUM(p.clicks) AS clicks,
               SUM(p.primary_conv_count) AS leads, SUM(p.primary_conv_value) AS revenue,
               SUM(p.cost_micros)/1e6 AS cost
        FROM search_query_performance p
        JOIN query_classification qc ON qc.query_id = p.query_id
        WHERE p.platform='google' AND p.device='ALL'
          AND p.report_date BETWEEN :lo AND :hi
          AND qc.category != 'excluded_offvertical'
        GROUP BY qc.category, qc.brand_name, qc.query_group, qc.product_code""",
        conn, params={"lo": lo.isoformat(), "hi": hi.isoformat()})
    # navigational override (Gilbert 2026-07-21) -- analysis layer only
    mask = (df["query_group"] == "1. Brand + product name") & df["product"].isna()
    df.loc[mask, "query_group"] = "2. Pure brand name"
    df["gp"] = df["revenue"] - df["cost"]
    df["seg_brand"] = df["brand"].fillna("(" + df["category"] + ")")
    return df


def keyword_pull(conn, lo, hi) -> pd.DataFrame:
    """Keyword-side rollup (match type grain), fragmentation-correct."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info('keyword_performance_daily')")]
    zc = "secondary_conv_count" if "secondary_conv_count" in cols else \
         next((c for c in cols if "zip" in c.lower() or "secondary" in c.lower()), None)
    extra = f", SUM(p.{zc}) AS zips" if zc else ""
    df = pd.read_sql_query(f"""
        SELECT kc.match_type, kc.brand_name AS brand, kc.category,
               SUM(p.clicks) AS clicks, SUM(p.primary_conv_count) AS leads,
               SUM(p.primary_conv_value) AS revenue, SUM(p.cost_micros)/1e6 AS cost{extra}
        FROM keyword_performance_daily p
        JOIN keyword_instances ki ON ki.id = p.instance_id
        JOIN keyword_classification kc
             ON kc.account_id = ki.account_id
            AND kc.ad_group_criterion_id = ki.ad_group_criterion_id
        WHERE ki.account_id = 1 AND p.device='ALL'
          AND p.report_date BETWEEN :lo AND :hi
        GROUP BY kc.match_type, kc.brand_name, kc.category""",
        conn, params={"lo": lo.isoformat(), "hi": hi.isoformat()})
    df["gp"] = df["revenue"] - df["cost"]
    return df


def load_registry(conn, notes):
    reg = pd.read_sql_query("""
        SELECT brand_name, bid_status, COUNT(DISTINCT bid_status) AS n_status
        FROM brand_registry
        WHERE instr(verticals,'home')
        GROUP BY brand_name""", conn)
    amb = reg[reg["n_status"] > 1]
    if len(amb):
        notes.append("AMBIGUOUS registry variants (conflicting bid_status) -- "
                     "sent back to DB architect, excluded from bucket logic: "
                     + ", ".join(amb.brand_name))
    return dict(zip(reg[reg.n_status == 1].brand_name, reg[reg.n_status == 1].bid_status))


# ---------------------------------------------------------------- bridge
def decompose(ly, ty):
    """Exact dGP decomposition for the continuing book. Returns component dict."""
    c0, l0, r0, co0 = ly["clicks"], ly["leads"], ly["revenue"], ly["cost"]
    c1, l1, r1, co1 = ty["clicks"], ty["leads"], ty["revenue"], ty["cost"]
    gp0, gp1 = r0 - co0, r1 - co1
    rpl0, rpl1 = r0 / l0, r1 / l1
    cpl0, cpl1 = co0 / l0, co1 / l1
    cpc0, cpc1 = co0 / c0, co1 / c1
    cvr0, cvr1 = l0 / c0, l1 / c1
    value = l0 * (rpl1 - rpl0)
    eff_cpc = -l0 * (cpc1 - cpc0) / cvr0
    eff_cvr = -l0 * cpc1 * (1.0 / cvr1 - 1.0 / cvr0)
    volume = (l1 - l0) * (rpl0 - cpl0)
    inter = (gp1 - gp0) - (value + eff_cpc + eff_cvr + volume)
    return {"payout": value, "price": eff_cpc, "conversion": eff_cvr,
            "volume_inter": volume + inter, "dgp": gp1 - gp0}


def build_bridge(qly: pd.DataFrame, qty: pd.DataFrame, status: dict):
    """Bucket every segment; return (steps list, brand table df, recon dict)."""
    ly_b = qly.groupby("seg_brand").agg(clicks=("clicks", "sum"), leads=("leads", "sum"),
                                        revenue=("revenue", "sum"), cost=("cost", "sum"),
                                        gp=("gp", "sum"))
    ty_b = qty.groupby("seg_brand").agg(clicks=("clicks", "sum"), leads=("leads", "sum"),
                                        revenue=("revenue", "sum"), cost=("cost", "sum"),
                                        gp=("gp", "sum"))
    all_b = ly_b.join(ty_b, how="outer", lsuffix="_ly", rsuffix="_ty").fillna(0.0)

    def bucket_of(name, row):
        st = status.get(name)
        if st == "restricted":
            return "restricted"
        if name in RECOVERABLE:
            return "recoverable"
        if row["cost_ly"] > 0 and row["cost_ty"] < EXIT_THRESHOLD * row["cost_ly"]:
            return "exited"
        return "continuing"

    all_b["bucket"] = [bucket_of(n, r) for n, r in all_b.iterrows()]
    all_b["dgp"] = all_b["gp_ty"] - all_b["gp_ly"]

    gp_ly, gp_ty = all_b["gp_ly"].sum(), all_b["gp_ty"].sum()
    d_restricted = all_b.loc[all_b.bucket == "restricted", "dgp"].sum()
    d_recover = all_b.loc[all_b.bucket == "recoverable", "dgp"].sum()
    d_exited = all_b.loc[all_b.bucket == "exited", "dgp"].sum()
    cont = all_b[all_b.bucket == "continuing"]
    cont_ly = {k: cont[f"{k}_ly"].sum() for k in ("clicks", "leads", "revenue", "cost")}
    cont_ty = {k: cont[f"{k}_ty"].sum() for k in ("clicks", "leads", "revenue", "cost")}
    comp = decompose(cont_ly, cont_ty)

    steps = [
        ("GP last year", gp_ly, C_END, "end"),
        ("Brands now restricted (policy)", d_restricted, C_POLICY, "delta"),
        ("State Farm (recoverable)", d_recover, C_POLICY, "delta"),
        ("Brands effectively exited", d_exited, C_POLICY, "delta"),
        ("Price paid (CPC)", comp["price"], C_RED, "delta"),
        ("Conversion (CvR)", comp["conversion"], C_RED, "delta"),
        ("Volume withdrawn + interaction", comp["volume_inter"], C_RED, "delta"),
        ("Richer payouts (RPL)", comp["payout"], C_GREEN, "delta"),
        ("GP this year", gp_ty, C_END, "end"),
    ]
    resid = gp_ty - (gp_ly + d_restricted + d_recover + d_exited + comp["dgp"])
    return steps, all_b.reset_index(), {"gp_ly": gp_ly, "gp_ty": gp_ty, "resid": resid}


# ---------------------------------------------------------------- exec sheet
def waterfall(ws, anchor_cell_row, col, steps, title):
    labels, bases, heights, colors = [], [], [], []
    running = 0.0
    for lab, v, color, kind in steps:
        labels.append(lab)
        if kind == "end":
            bases.append(min(0.0, v)); heights.append(abs(v)); running = v
        else:
            new = running + v
            bases.append(min(running, new)); heights.append(abs(v)); running = new
        colors.append(color)
    first, last = write_table(ws, anchor_cell_row, col,
                              ["Step", "base", "delta", "signed $"],
                              [[l, b, h, s[1]] for l, b, h, s in
                               zip(labels, bases, heights, steps)],
                              [None, "0", "0", "$#,##0;($#,##0)"],
                              widths=[30, 9, 9, 12])
    ch = BarChart()
    ch.type = "col"; ch.grouping = "stacked"; ch.overlap = 100; ch.gapWidth = 40
    ch.add_data(Reference(ws, min_col=col + 1, min_row=anchor_cell_row, max_row=last),
                titles_from_data=True)
    ch.add_data(Reference(ws, min_col=col + 2, min_row=anchor_cell_row, max_row=last),
                titles_from_data=True)
    ch.set_categories(Reference(ws, min_col=col, min_row=first, max_row=last))
    ch.series[0].graphicalProperties.noFill = True
    for i, hexc in enumerate(colors):
        dp = DataPoint(idx=i)
        dp.graphicalProperties.solidFill = hexc
        ch.series[1].data_points.append(dp)
    ch.legend = None
    style_chart(ch, title, y_title="GP ($)", height=10, width=19)
    return ch, last


def clustered(ws, hdr_row, first, last, col_ly, col_ty, cats_col, title, y_title,
              horizontal=True):
    ch = BarChart()
    ch.type = "bar" if horizontal else "col"
    ch.grouping = "clustered"; ch.gapWidth = 50
    for cc, color in ((col_ly, C_LY), (col_ty, C_TY)):
        ref = Reference(ws, min_col=cc, min_row=hdr_row, max_row=last)
        ch.add_data(ref, titles_from_data=True)
    for s, color in zip(ch.series, (C_LY, C_TY)):
        s.graphicalProperties.solidFill = color
    ch.set_categories(Reference(ws, min_col=cats_col, min_row=first, max_row=last))
    style_chart(ch, title, y_title=y_title, height=9, width=12)
    return ch


def net_steps(steps):
    """Collapse price + payouts into one net-auction bar for the CEO chart."""
    d = {s[0]: s for s in steps}
    price = d["Price paid (CPC)"][1]
    payout = d["Richer payouts (RPL)"][1]
    net = price + payout
    out = []
    for lab, v, color, kind in steps:
        if lab == "Price paid (CPC)":
            out.append((f"Net auction economics (RPL vs CPC)", net,
                        C_GREEN if net >= 0 else C_RED, "delta"))
        elif lab == "Richer payouts (RPL)":
            continue
        else:
            out.append((lab, v, color, kind))
    return out, price, payout


def exec_sheet(wb, name, plo, phi, clo, chi, qly, qty, kly, kty, status, notes,
               bridge_style="detail", coverage=None):
    ws = wb.create_sheet(f"Exec {name}")
    ws.cell(row=1, column=1, value=f"Google Home -- what drove profit, "
            f"{plo.year} vs {clo.year} ({name})").font = F_TITLE
    ws.cell(row=2, column=1, value=f"Matched spans: {plo}..{phi} vs {clo}..{chi} · "
            "query-classified platform data · Looker remains the official topline").font = F_SUB

    steps, brand_tbl, recon = build_bridge(qly, qty, status)
    if abs(recon["resid"]) > 1.0:
        notes.append(f"[{name}] bridge residual ${recon['resid']:,.2f} -- investigate")

    # headline cards
    gap = recon["gp_ty"] - recon["gp_ly"]
    cards = [("GP last year", recon["gp_ly"], FILL_CARD),
             ("GP this year", recon["gp_ty"], FILL_CARD),
             ("Gap the bridge allocates", gap, FILL_GAP)]
    for i, (lab, val, fill) in enumerate(cards):
        c0 = 1 + i * 3
        ws.cell(row=4, column=c0, value=lab).font = F_CARD_L
        v = ws.cell(row=5, column=c0, value=val)
        v.font, v.number_format = F_CARD_V, "$#,##0;($#,##0)"
        for rr in (4, 5):
            for cc in range(c0, c0 + 2):
                ws.cell(row=rr, column=cc).fill = fill

    # Panel A
    ws.cell(row=7, column=1, value="Panel A -- where the profit went").font = F_H2
    chart_steps = steps
    if bridge_style == "net":
        chart_steps, price, payout = net_steps(steps)
    ch, last = waterfall(ws, 8, 1, chart_steps,
                         f"{name}: GP bridge {plo.year} -> {clo.year}")
    ws.add_chart(ch, "F8")
    r = last + 2
    if bridge_style == "net":
        ws.cell(row=r, column=1, value=f"Net auction economics = richer payouts "
                f"(RPL) ${payout:+,.0f} against price paid (CPC) ${price:+,.0f} "
                "-- two large offsetting forces, netted for readability; full "
                "detail on the Notes tab.").font = F_NOTE
        r += 1
    ws.cell(row=r, column=1, value="Owners: restricted = policy (not recoverable) · "
            "State Farm = partner ask · exited = investigate · price/conversion/volume "
            "= operations · payouts = partner tailwind").font = F_NOTE
    r += 2

    # Panel B: GP by brand
    ws.cell(row=r, column=1, value="Panel B -- the profit roster, then vs now").font = F_H2
    r += 1
    bt = brand_tbl.copy()
    # select on the best of LY/TY GP so this-year winners (e.g. Costco) appear,
    # then display in LY order for the then-vs-now read
    bt["rank_val"] = bt[["gp_ly", "gp_ty"]].max(axis=1)
    top = bt.sort_values("rank_val", ascending=False).head(14) \
            .sort_values("gp_ly", ascending=False)
    data = [[rr.seg_brand, status.get(rr.seg_brand, ""), rr.bucket,
             rr.gp_ly, rr.gp_ty, rr.dgp] for rr in top.itertuples()]
    first, last = write_table(ws, r, 1,
                              ["Brand / segment", "bid_status", "Bucket",
                               f"GP {plo.year}", f"GP {clo.year}", "dGP"],
                              data, [None, None, None] + ["$#,##0;($#,##0)"] * 3,
                              widths=[22, 11, 11, 11, 11, 11])
    ws.add_chart(clustered(ws, r, first, last, 4, 5, 1,
                           f"{name}: GP by brand -- what worked then vs now", "GP ($)"),
                 f"H{r}")
    r = last + 3

    # Panel C: match type + query group
    ws.cell(row=r, column=1, value="Panel C -- how we bought it").font = F_H2
    r += 1
    mly = kly.groupby("match_type")["gp"].sum()
    mty = kty.groupby("match_type")["gp"].sum()
    mts = sorted(set(mly.index) | set(mty.index))
    data = [[m, float(mly.get(m, 0)), float(mty.get(m, 0))] for m in mts]
    f1, l1 = write_table(ws, r, 1, ["Match type", f"GP {plo.year}", f"GP {clo.year}"],
                         data, [None, "$#,##0;($#,##0)", "$#,##0;($#,##0)"],
                         widths=[14, 11, 11])
    ws.add_chart(clustered(ws, r, f1, l1, 2, 3, 1,
                           f"{name}: GP by match type", "GP ($)", horizontal=False),
                 f"E{r}")
    gly = qly.groupby("query_group")["gp"].sum()
    gty = qty.groupby("query_group")["gp"].sum()
    groups = list(pd.concat([gly, gty], axis=1).fillna(0)
                  .abs().max(axis=1).sort_values(ascending=False).head(8).index)
    gdata = [[g, float(gly.get(g, 0)), float(gty.get(g, 0))] for g in groups]
    r2 = l1 + 2
    f2, l2 = write_table(ws, r2, 1, ["Query group", f"GP {plo.year}", f"GP {clo.year}"],
                         gdata, [None, "$#,##0;($#,##0)", "$#,##0;($#,##0)"],
                         widths=[30, 11, 11])
    ws.add_chart(clustered(ws, r2, f2, l2, 2, 3, 1,
                           f"{name}: GP by query group", "GP ($)"), f"E{r2}")
    r = l2 + 20
    if coverage:
        ws.cell(row=r, column=1, value=coverage).font = F_NOTE
        r += 1
    ws.cell(row=r, column=1, value="Footnotes: bid_status reflects CURRENT policy "
            "('brands now restricted' -- some restrictions landed mid-history). "
            f"'Effectively exited' = TY spend < {EXIT_THRESHOLD:.0%} of LY spend. "
            "Platform data runs slightly below the Looker topline (attribution).").font = F_NOTE
    return brand_tbl


# ---------------------------------------------------------------- appendices
def leak_audit(wb, conn, status, clo, chi, notes):
    ws = wb.create_sheet("Leak audit")
    ws.cell(row=1, column=1, value="Restricted-brand exposure audit").font = F_TITLE
    ws.cell(row=2, column=1, value="restricted + live spend = compliance leak to fix; "
            "restricted + paused/no spend = deliberate exit (label as such, not 'loser')").font = F_SUB
    restricted = sorted([b for b, s in status.items() if s == "restricted"])

    kw = keyword_pull(conn, clo, chi)
    kw_leak = kw[kw["brand"].isin(restricted) & ((kw["cost"] > 0) | (kw["clicks"] > 0))]
    r = 4
    ws.cell(row=r, column=1, value="A. Restricted brands with keyword-side spend "
            f"this year ({clo}..{chi})").font = F_BOLD
    if len(kw_leak):
        g = kw_leak.groupby("brand").agg(clicks=("clicks", "sum"), cost=("cost", "sum"),
                                         leads=("leads", "sum"), gp=("gp", "sum"))
        data = [[b, rr.clicks, rr.cost, rr.leads, rr.gp] for b, rr in
                zip(g.index, g.itertuples())]
        _, last = write_table(ws, r + 1, 1, ["Brand", "Clicks", "Cost", "Leads", "GP"],
                              data, [None, "#,##0", "$#,##0.00", "#,##0", "$#,##0;($#,##0)"],
                              widths=[20, 9, 11, 8, 11])
        notes.append("LEAKS: restricted brands with keyword spend this year: "
                     + ", ".join(g.index))
        r = last + 2
    else:
        ws.cell(row=r + 1, column=1, value="None found.").font = F_BODY
        r += 3

    q = pd.read_sql_query("""
        SELECT qc.brand_name AS brand, SUM(p.impressions) AS imps, SUM(p.clicks) AS clicks,
               SUM(p.cost_micros)/1e6 AS cost, SUM(p.primary_conv_value) AS revenue
        FROM search_query_performance p
        JOIN query_classification qc ON qc.query_id = p.query_id
        WHERE p.platform='google' AND p.device='ALL'
          AND p.report_date BETWEEN :lo AND :hi AND qc.brand_name IS NOT NULL
        GROUP BY qc.brand_name""", conn,
        params={"lo": clo.isoformat(), "hi": chi.isoformat()})
    q_leak = q[q["brand"].isin(restricted) & ((q["cost"] > 0) | (q["clicks"] > 0))]
    ws.cell(row=r, column=1, value="B. Restricted-brand QUERIES still receiving traffic "
            "this year (broad harvesting -- negative-keyword gaps)").font = F_BOLD
    if len(q_leak):
        data = [[rr.brand, rr.imps, rr.clicks, rr.cost, rr.revenue - rr.cost]
                for rr in q_leak.itertuples()]
        write_table(ws, r + 1, 1, ["Brand", "Imps", "Clicks", "Cost", "GP"],
                    data, [None, "#,##0", "#,##0", "$#,##0.00", "$#,##0;($#,##0)"],
                    widths=[20, 9, 9, 11, 11])
    else:
        ws.cell(row=r + 1, column=1, value="None found.").font = F_BODY


def detail_tab(wb, title, frames_by_window, keycols, metric_cols):
    ws = wb.create_sheet(title)
    ws.cell(row=1, column=1, value=title).font = F_TITLE
    r = 3
    for name, (ly, ty, plabel, clabel) in frames_by_window.items():
        ws.cell(row=r, column=1, value=name).font = F_H2
        merged = ly.merge(ty, on=keycols, how="outer", suffixes=("_ly", "_ty")).fillna(0.0)
        hdr = keycols + [f"{m} {plabel}" for m in metric_cols] + \
              [f"{m} {clabel}" for m in metric_cols]
        data = [[*(getattr(rr, k) for k in keycols),
                 *(getattr(rr, f"{m}_ly") for m in metric_cols),
                 *(getattr(rr, f"{m}_ty") for m in metric_cols)]
                for rr in merged.itertuples()]
        fmts = [None] * len(keycols) + ["#,##0.00"] * (2 * len(metric_cols))
        _, last = write_table(ws, r + 1, 1, hdr, data, fmts,
                              widths=[18] * len(keycols) + [10] * (2 * len(metric_cols)))
        r = last + 2


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Google Home YoY CEO workbook")
    ap.add_argument("--db", required=True)
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--funnel-xlsx", default=None,
                    help="Looker export for topline reconciliation on Notes tab")
    ap.add_argument("--output", default=None)
    ap.add_argument("--bridge-style", choices=["detail", "net"], default="detail",
                    help="'net' collapses price+payouts into one net-auction bar "
                         "(recommended for the CEO chart when the two are large "
                         "and offsetting)")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    notes = []
    asof = dt.date.fromisoformat(args.as_of) if args.as_of else dt.date.fromisoformat(
        str(conn.execute("SELECT MAX(report_date) FROM search_query_performance "
                         "WHERE platform='google'").fetchone()[0])[:10])
    out = args.output or f"google_home_yoy_ceo_{asof.isoformat()}.xlsx"
    notes.append(f"Anchor date {asof}; matched spans both years")
    lo = conn.execute("SELECT MIN(report_date) FROM search_query_performance "
                      "WHERE platform='google'").fetchone()[0]
    notes.append(f"Query perf coverage from {lo}")
    status = load_registry(conn, notes)

    wb = Workbook(); wb.remove(wb.active)
    qframes, kframes = {}, {}
    for name, plo, phi, clo, chi in windows(asof):
        qly, qty = query_pull(conn, plo, phi), query_pull(conn, clo, chi)
        kly, kty = keyword_pull(conn, plo, phi), keyword_pull(conn, clo, chi)
        if qly.empty:
            notes.append(f"[{name}] LY window empty -- skipped")
            continue
        # query-report coverage vs keyword-side account totals (search terms
        # below Google's privacy threshold never reach query grain)
        kgp_ly, kgp_ty = kly["gp"].sum(), kty["gp"].sum()
        qgp_ly, qgp_ty = qly["gp"].sum(), qty["gp"].sum()
        cov_ly = qgp_ly / kgp_ly if kgp_ly else None
        cov_ty = qgp_ty / kgp_ty if kgp_ty else None
        cov_txt = (f"Query-report coverage: bridge universe captures "
                   f"${qgp_ly:,.0f} of ${kgp_ly:,.0f} keyword-side GP LY"
                   + (f" ({cov_ly:.0%})" if cov_ly is not None else "")
                   + f" and ${qgp_ty:,.0f} of ${kgp_ty:,.0f} TY"
                   + (f" ({cov_ty:.0%})" if cov_ty is not None else "")
                   + " -- unreported ('other') search terms cannot be brand-"
                     "classified; similar capture both years keeps the bridge "
                     "shape unbiased.")
        notes.append(f"[{name}] {cov_txt}")
        if cov_ly is not None and cov_ty is not None and abs(cov_ly - cov_ty) > 0.05:
            notes.append(f"[{name}] WARNING: capture rates differ by "
                         f"{abs(cov_ly - cov_ty):.0%} between years -- bridge "
                         "bars may be skewed; review before presenting.")
        exec_sheet(wb, name, plo, phi, clo, chi, qly, qty, kly, kty, status, notes,
                   bridge_style=args.bridge_style, coverage=cov_txt)
        qframes[name] = (
            qly.groupby(["seg_brand"], as_index=False)[["clicks", "leads", "revenue", "cost", "gp"]].sum(),
            qty.groupby(["seg_brand"], as_index=False)[["clicks", "leads", "revenue", "cost", "gp"]].sum(),
            str(plo.year), str(clo.year))
        kframes[name] = (
            kly.groupby(["match_type"], as_index=False)[["clicks", "leads", "revenue", "cost", "gp"]].sum(),
            kty.groupby(["match_type"], as_index=False)[["clicks", "leads", "revenue", "cost", "gp"]].sum(),
            str(plo.year), str(clo.year))

    detail_tab(wb, "Brands detail", qframes, ["seg_brand"],
               ["clicks", "leads", "revenue", "cost", "gp"])
    detail_tab(wb, "Match types", kframes, ["match_type"],
               ["clicks", "leads", "revenue", "cost", "gp"])
    _, _, clo, chi = windows(asof)[0][1], None, windows(asof)[0][3], windows(asof)[0][4]
    leak_audit(wb, conn, status, clo, chi, notes)

    # Notes / reconciliation
    if args.funnel_xlsx and Path(args.funnel_xlsx).exists():
        f = pd.read_excel(args.funnel_xlsx, sheet_name="Raw Data")
        f["Date"] = pd.to_datetime(f["Date"], errors="coerce")
        f = f.dropna(subset=["Date"]).set_index("Date").sort_index()
        for c in ("Rev", "Cost", "GP"):
            f[c] = pd.to_numeric(f[c], errors="coerce").fillna(0)
        s = f.loc[str(clo):str(chi)]
        cov_hi = s.index.max().date() if len(s) else None
        notes.append(f"Looker topline {clo}..{cov_hi} (export coverage; window requested "
                     f"..{chi}): GP ${float(s['GP'].sum()):,.0f} -- re-export Looker through "
                     f"{chi} for a full tie-out; platform query-side GP runs below (attribution)")
    ws = wb.create_sheet("Notes", 0)
    ws.cell(row=1, column=1, value="Methodology and reconciliation").font = F_TITLE
    method = [
        "Bridge buckets: (1) brands now restricted (bid_status='restricted', policy, not recoverable);",
        "(2) State Farm (conditional; the recoverable ask, per Gilbert 2026-07-24);",
        f"(3) brands effectively exited (TY spend < {EXIT_THRESHOLD:.0%} of LY spend);",
        "(4) continuing book -- dGP decomposed exactly into payout (RPL), price (CPC),",
        "conversion (CvR), and volume+interaction. Buckets sum exactly to the GP gap.",
        "Brands resolved ONLY via persisted brand_matcher-canonical classifications;",
        "registry joined on brand_name for bid_status; ambiguities routed to DB architect.",
        "Query side = demand truth (broad-era correct); keyword side = match type + leak audit.",
        "Bridge universe = query-reported traffic only; coverage vs keyword-side GP is",
        "quantified per window above. Unreported search terms cannot be brand-classified;",
        "no gross-up is applied (allocating them to brands would be fabrication).",
    ]
    r = 3
    for m in method:
        ws.cell(row=r, column=1, value=m).font = F_BODY; r += 1
    r += 1
    for n in notes:
        ws.cell(row=r, column=1, value="- " + n).font = F_NOTE; r += 1
    ws.column_dimensions["A"].width = 110

    wb.save(out)
    print(f"wrote {out}")
    for n in notes:
        print("  -", n)


if __name__ == "__main__":
    main()
