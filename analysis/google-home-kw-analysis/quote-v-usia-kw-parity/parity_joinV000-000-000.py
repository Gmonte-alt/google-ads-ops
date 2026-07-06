#!/usr/bin/env python
"""
parity_join.py -- Quote.com <-> USIA three-set join with three-tier covering.

READ-ONLY on paid_search.db (mode=ro). Writes only to --out-dir. Never writes the DB.

Covering (per operator's AND-gate ruling): a query is ACTIVELY COVERED only if a keyword with its
normalized text is ENABLED at all three levels -- keyword AND ad_group AND campaign. Not-covered
splits into RE-ENABLE (keyword exists but paused somewhere) vs ADD (no keyword at all). Joins carry
`AND account_id=1` (shared multi-platform DB -- never join on id alone). covering_key() (collapse-
both) is applied to BOTH sides; match_type is irrelevant to covering.

Sets: 1 USIA targets (generic + carrier); 2 account-1 keyword_instances (serving + existence);
3 account-1 realized sqp demand (device='ALL', primary_conv_* truth, trailing --days), inheriting
persisted target_theme/target_group.

Deliverables: Theme_UnderBuilt_Map; Adds_NonBrand / Adds_Brand (Action = add_new|reenable_existing,
navigate suppressed, brand bid_status shown); USIA_Target_Gap (USIA targets not actively covered);
08_Disagreement; README.
"""
import argparse
import csv
import os
import sqlite3
from datetime import date

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from parity_common import (categorize, make_brand_tagger, brand_of, covering_key, is_navigate,
                           load_usia_all, is_suppressed_add, query_group, DEFAULT_OUT_DIR)

FONT = "Arial"
HDR_FILL = PatternFill("solid", fgColor="1F3864")
HDR_FONT = Font(name=FONT, bold=True, color="FFFFFF", size=10)
TITLE_FONT = Font(name=FONT, bold=True, size=13)
CELL_FONT = Font(name=FONT, size=10)
NOTE_FONT = Font(name=FONT, italic=True, size=9, color="7F7F7F")
THIN = Side(style="thin", color="D9D9D9")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def ro_connect(db):
    return sqlite3.connect("file:%s?mode=ro" % os.path.abspath(db), uri=True)


# --------------------------------------------------------------------------- covering (three tiers)
def paused_desc(loc):
    parts = []
    if (loc["c_status"] or "") != "ENABLED":
        parts.append("campaign PAUSED")
    if (loc["ag_status"] or "") != "ENABLED":
        parts.append("ad group PAUSED")
    if (loc["kw_status"] or "") != "ENABLED":
        parts.append("keyword PAUSED")
    where = "campaign='%s' / ad group='%s'" % (loc.get("campaign") or "?", loc.get("ad_group") or "?")
    return where + ("  [" + ", ".join(parts) + "]" if parts else "")


def load_covering(conn, tagger):
    """serving_keys (ENABLED all 3 levels), existence_map (key -> best location, any status),
    and serving theme/group counts over DISTINCT serving keywords."""
    q = """
        SELECT ki.keyword_text, ki.status AS kw_status,
               ag.status AS ag_status, c.status AS c_status,
               ki.campaign_name, ki.ad_group_name
        FROM keyword_instances ki
        LEFT JOIN ad_groups ag ON ag.ad_group_id = ki.ad_group_id AND ag.account_id = ki.account_id
        LEFT JOIN campaigns  c ON c.campaign_id  = ki.campaign_id  AND c.account_id  = ki.account_id
        WHERE ki.account_id = 1
    """
    serving_keys = set()
    serving_kw = {}          # key -> representative keyword_text (serving)
    existence_map = {}       # key -> best location dict
    n = 0
    for kw, kws, ags, cs, cname, agname in conn.execute(q):
        n += 1
        key = covering_key(kw)
        serving = (kws == "ENABLED" and ags == "ENABLED" and cs == "ENABLED")
        levels_ok = sum(1 for s in (kws, ags, cs) if (s or "") == "ENABLED")
        if serving:
            serving_keys.add(key)
            serving_kw.setdefault(key, kw)
        rec = dict(keyword=kw, campaign=cname, ad_group=agname,
                   kw_status=kws, ag_status=ags, c_status=cs, levels_ok=levels_ok, serving=serving)
        prev = existence_map.get(key)
        if prev is None or rec["levels_ok"] > prev["levels_ok"]:
            existence_map[key] = rec

    # theme/group counts over distinct SERVING keywords (actively targeted set)
    counts = {}
    for key, kw in serving_kw.items():
        theme = categorize(kw)
        group = "BRAND" if brand_of(tagger, kw)[0] else "NONBRAND"
        counts[(group, theme)] = counts.get((group, theme), 0) + 1
    return serving_keys, existence_map, counts, n


def load_set3(conn, days):
    q = """
        SELECT sq.id, sq.query_text, sq.target_theme, sq.target_group,
               SUM(p.impressions) AS impressions, SUM(p.clicks) AS clicks,
               SUM(p.cost_micros)/1e6 AS cost,
               SUM(p.primary_conv_count) AS conv, SUM(p.primary_conv_value) AS conv_value
        FROM search_query_performance p
        JOIN keyword_instances ki ON ki.id = p.instance_id
        JOIN search_queries    sq ON sq.id = p.query_id
        WHERE ki.account_id = 1 AND p.device = 'ALL' AND p.report_date >= date('now', ?)
        GROUP BY sq.id
    """
    rows = []
    for r in conn.execute(q, ("-%d day" % days,)):
        rows.append(dict(query_id=r[0], query_text=r[1] or "",
                         target_theme=r[2] or "CORE", target_group=r[3] or "NONBRAND",
                         impressions=r[4] or 0, clicks=r[5] or 0, cost=r[6] or 0.0,
                         conv=r[7] or 0.0, conv_value=r[8] or 0.0))
    return rows


def load_disagreements(conn, tagger, limit=500):
    theme_d = brand_d = total = 0
    sample = []
    for qt, theme08, ttheme, mbrand08, tgroup in conn.execute(
            "SELECT query_text, theme, target_theme, modifier_brand, target_group "
            "FROM search_queries WHERE target_theme IS NOT NULL"):
        total += 1
        reg_brand = brand_of(tagger, qt or "")[0]
        t_dis = (theme08 or "") != (ttheme or "") and (theme08 or "") != ""
        b_dis = bool((mbrand08 or "").strip()) != bool(reg_brand)
        theme_d += t_dis; brand_d += b_dis
        if (t_dis or b_dis) and len(sample) < limit:
            sample.append(dict(query_text=qt or "", theme_08=theme08 or "", target_theme=ttheme or "",
                               modifier_brand_08=mbrand08 or "", registry_brand=reg_brand or "",
                               theme_disagree=t_dis, brand_disagree=b_dis))
    return dict(total=total, theme_disagree=theme_d, brand_disagree=brand_d, sample=sample)


# --------------------------------------------------------------------------- aggregation
def build_theme_map(usia_by_theme, serving_counts, set3):
    themes = set(usia_by_theme) | {t for (_, t) in serving_counts} | {r["target_theme"] for r in set3}
    agg = {}
    for r in set3:
        a = agg.setdefault(r["target_theme"], dict(clicks=0, cost=0.0, conv=0.0, value=0.0))
        a["clicks"] += r["clicks"]; a["cost"] += r["cost"]
        a["conv"] += r["conv"]; a["value"] += r["conv_value"]
    out = []
    for th in themes:
        a = agg.get(th, dict(clicks=0, cost=0.0, conv=0.0, value=0.0))
        out.append(dict(theme=th, usia=len(usia_by_theme.get(th, [])),
                        qc_nonbrand=serving_counts.get(("NONBRAND", th), 0),
                        qc_brand=serving_counts.get(("BRAND", th), 0),
                        clicks=a["clicks"], cost=a["cost"], conv=a["conv"], value=a["value"]))
    out.sort(key=lambda d: (-d["value"], -d["conv"]))
    return out


def build_adds(set3, serving_keys, existence_map, tagger, min_clicks, min_conv):
    brand, nonbrand, dropped = [], [], {"suppressed": 0, "navigate": 0, "actively_covered": 0}
    for r in set3:
        if is_suppressed_add(r["target_theme"]):
            dropped["suppressed"] += 1; continue
        if is_navigate(r["query_text"]):
            dropped["navigate"] += 1; continue
        key = covering_key(r["query_text"])
        if key in serving_keys:
            dropped["actively_covered"] += 1; continue
        loc = existence_map.get(key)
        action = "reenable_existing" if loc else "add_new"
        where = paused_desc(loc) if loc else ""
        meets = r["conv"] > 0 and (r["clicks"] >= min_clicks or r["conv"] >= min_conv)
        row = dict(r, action=action, where=where, meets_support=meets, bid_status="")
        if r["target_group"] == "BRAND":
            row["bid_status"] = brand_of(tagger, r["query_text"])[1] or "unknown"
            brand.append(row)
        else:
            nonbrand.append(row)

    def rank(rows):
        r = [x for x in rows if x["meets_support"]]
        r.sort(key=lambda d: (d["cost"] / d["conv"]) if d["conv"] else float("inf"))
        return r
    return rank(brand), rank(nonbrand), dropped


def build_group_opportunity(set3, serving_keys, tagger):
    """Aggregate realized demand by V3-style query group, splitting covered vs not. USIA-independent:
    this is 'where is untapped demand, by group' regardless of USIA coverage. 'Volume' proxy =
    realized impressions/clicks (we have no absolute KWP volume for arbitrary queries)."""
    agg = {}
    for r in set3:
        brand = r["target_group"] == "BRAND"
        qg = query_group(r["query_text"], brand=brand)
        served = covering_key(r["query_text"]) in serving_keys
        a = agg.setdefault(qg, dict(queries=0, impressions=0, clicks=0, cost=0.0, conv=0.0, value=0.0,
                                    cov_queries=0, cov_clicks=0, cov_conv=0.0))
        a["queries"] += 1
        a["impressions"] += r["impressions"]; a["clicks"] += r["clicks"]; a["cost"] += r["cost"]
        a["conv"] += r["conv"]; a["value"] += r["conv_value"]
        if served:
            a["cov_queries"] += 1; a["cov_clicks"] += r["clicks"]; a["cov_conv"] += r["conv"]
    rows = []
    for qg, a in agg.items():
        rows.append(dict(group=qg, **a,
                         uncov_clicks=a["clicks"] - a["cov_clicks"],
                         uncov_conv=a["conv"] - a["cov_conv"]))
    # rank by untapped demand: uncovered clicks desc (biggest demand we're not actively targeting)
    rows.sort(key=lambda d: -d["uncov_clicks"])
    return rows


def build_gap(usia_flat, serving_keys, existence_map, set3):
    s3 = {}
    for r in set3:
        k = covering_key(r["query_text"])
        a = s3.setdefault(k, dict(clicks=0, cost=0.0, conv=0.0, value=0.0))
        a["clicks"] += r["clicks"]; a["cost"] += r["cost"]; a["conv"] += r["conv"]; a["value"] += r["conv_value"]
    rows = []
    for u in usia_flat:
        key = covering_key(u["keyword"])
        if key in serving_keys:
            continue  # USIA target already actively covered -> not a gap
        loc = existence_map.get(key)
        coverage = "paused" if loc else "absent"
        perf = s3.get(key, dict(clicks=0, cost=0.0, conv=0.0, value=0.0))
        rows.append(dict(keyword=u["keyword"], theme=u["theme"], source=u["source"],
                         brand=u.get("brand", ""), usia_volume=u["volume"], coverage=coverage,
                         where=paused_desc(loc) if loc else "",
                         q_clicks=perf["clicks"], q_cost=perf["cost"], q_conv=perf["conv"], q_value=perf["value"]))
    # paused (fast re-enable wins) first, then by USIA volume, then Quote value
    rows.sort(key=lambda d: (0 if d["coverage"] == "paused" else 1, -d["usia_volume"], -d["q_value"]))
    return rows


# --------------------------------------------------------------------------- workbook helpers
def _hdr(ws, headers, row=1):
    for c, h in enumerate(headers, 1):
        cell = ws.cell(row=row, column=c, value=h)
        cell.font = HDR_FONT; cell.fill = HDR_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = BORDER
    ws.row_dimensions[row].height = 28


def _style(ws, first, last, ncols):
    for r in range(first, last + 1):
        for c in range(1, ncols + 1):
            ws.cell(row=r, column=c).font = CELL_FONT
            ws.cell(row=r, column=c).border = BORDER


def _widths(ws, widths):
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w


def sheet_readme(wb, params, caveats):
    ws = wb.active; ws.title = "README"
    ws["A1"] = "Quote.com <-> USIA Keyword Parity"; ws["A1"].font = TITLE_FONT
    r = 3
    ws.cell(row=r, column=1, value="Run parameters").font = Font(name=FONT, bold=True, size=11); r += 1
    for k, v in params.items():
        ws.cell(row=r, column=1, value=k).font = CELL_FONT
        ws.cell(row=r, column=2, value=str(v)).font = CELL_FONT
        r += 1
    r += 1
    ws.cell(row=r, column=1, value="Caveats").font = Font(name=FONT, bold=True, size=11); r += 1
    for c in caveats:
        cell = ws.cell(row=r, column=1, value="\u2022 " + c); cell.font = NOTE_FONT
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=6)
        ws.row_dimensions[r].height = 30
        r += 1
    _widths(ws, [26, 60, 12, 12, 12, 12])


def sheet_theme_map(wb, tmap):
    ws = wb.create_sheet("Theme_UnderBuilt_Map")
    ws["A1"] = ("USIA targets (generic + carrier) vs Quote.com ACTIVELY-SERVING keyword counts, "
                "against realized account-1 demand. QC counts are ENABLED-at-all-3-levels only.")
    ws["A1"].font = NOTE_FONT; ws.merge_cells("A1:M1"); ws.row_dimensions[1].height = 26
    headers = ["Theme", "USIA targets", "QC serving nonbrand", "QC serving brand", "Under-built (USIA/QCnb)",
               "Realized clicks", "Realized cost", "Realized leads", "Realized value", "CPL", "CvR", "RPL", "Value share", "ROAS"]
    _hdr(ws, headers, row=2)
    first = 3
    for i, d in enumerate(tmap):
        r = first + i
        ws.cell(row=r, column=1, value=d["theme"])
        ws.cell(row=r, column=2, value=d["usia"])
        ws.cell(row=r, column=3, value=d["qc_nonbrand"])
        ws.cell(row=r, column=4, value=d["qc_brand"])
        ws.cell(row=r, column=5, value="=IFERROR(B%d/C%d,\"\")" % (r, r))
        ws.cell(row=r, column=6, value=round(d["clicks"]))
        ws.cell(row=r, column=7, value=round(d["cost"], 2))
        ws.cell(row=r, column=8, value=round(d["conv"], 2))
        ws.cell(row=r, column=9, value=round(d["value"], 2))
        ws.cell(row=r, column=10, value="=IFERROR(G%d/H%d,\"\")" % (r, r))
        ws.cell(row=r, column=11, value="=IFERROR(H%d/F%d,\"\")" % (r, r))
        ws.cell(row=r, column=12, value="=IFERROR(I%d/H%d,\"\")" % (r, r))
        ws.cell(row=r, column=14, value="=IFERROR(I%d/G%d,\"\")" % (r, r))
    last = first + len(tmap) - 1
    for i in range(len(tmap)):
        r = first + i
        ws.cell(row=r, column=13, value="=IFERROR(I%d/SUM($I$%d:$I$%d),\"\")" % (r, first, last))
    _style(ws, first, last, len(headers))
    for r in range(first, last + 1):
        ws.cell(row=r, column=7).number_format = "$#,##0"
        ws.cell(row=r, column=9).number_format = "$#,##0"
        ws.cell(row=r, column=10).number_format = "$#,##0.00"
        ws.cell(row=r, column=11).number_format = "0.0%"
        ws.cell(row=r, column=12).number_format = "$#,##0.00"
        ws.cell(row=r, column=13).number_format = "0.0%"
        ws.cell(row=r, column=5).number_format = "0.0x"
        ws.cell(row=r, column=14).number_format = '0.00"x"'
    ws.freeze_panes = "A3"
    _widths(ws, [20, 12, 16, 14, 16, 13, 12, 12, 13, 10, 8, 10, 10, 8])


ADD_HEADERS = ["Rank", "Query", "Theme", "Group", "Action", "Bid status", "Impr", "Clicks", "Cost",
               "Leads", "Value", "CPL", "CvR", "RPL", "Where (if re-enable)"]


def sheet_adds(wb, title, rows, caveat, min_clicks, min_conv):
    ws = wb.create_sheet(title)
    cap = ("Converting account-1 demand that is NOT actively covered (no ENABLED-at-all-3-levels "
           "keyword). Action: add_new = no keyword exists; reenable_existing = keyword exists but "
           "paused (see Where). Ranked by CPL. Support: conv>0 AND (clicks>=%d OR conv>=%d). "
           "'Not covered' still may serve on broad/phrase; match simulation deferred.") % (min_clicks, min_conv)
    if caveat:
        cap += " " + caveat
    ws["A1"] = cap; ws["A1"].font = NOTE_FONT; ws.merge_cells("A1:O1")
    ws.row_dimensions[1].height = 46; ws["A1"].alignment = Alignment(wrap_text=True, vertical="top")
    _hdr(ws, ADD_HEADERS, row=2)
    first = 3
    for i, d in enumerate(rows):
        r = first + i
        ws.cell(row=r, column=1, value=i + 1)
        ws.cell(row=r, column=2, value=d["query_text"])
        ws.cell(row=r, column=3, value=d["target_theme"])
        ws.cell(row=r, column=4, value=d["target_group"])
        ws.cell(row=r, column=5, value=d["action"])
        ws.cell(row=r, column=6, value=d.get("bid_status", ""))
        ws.cell(row=r, column=7, value=round(d["impressions"]))
        ws.cell(row=r, column=8, value=round(d["clicks"]))
        ws.cell(row=r, column=9, value=round(d["cost"], 2))
        ws.cell(row=r, column=10, value=round(d["conv"], 2))
        ws.cell(row=r, column=11, value=round(d["conv_value"], 2))
        ws.cell(row=r, column=12, value="=IFERROR(I%d/J%d,\"\")" % (r, r))
        ws.cell(row=r, column=13, value="=IFERROR(J%d/H%d,\"\")" % (r, r))
        ws.cell(row=r, column=14, value="=IFERROR(K%d/J%d,\"\")" % (r, r))
        ws.cell(row=r, column=15, value=d.get("where", ""))
    last = max(first, first + len(rows) - 1)
    if rows:
        _style(ws, first, last, len(ADD_HEADERS))
        for r in range(first, last + 1):
            ws.cell(row=r, column=9).number_format = "$#,##0.00"
            ws.cell(row=r, column=11).number_format = "$#,##0.00"
            ws.cell(row=r, column=12).number_format = "$#,##0.00"
            ws.cell(row=r, column=13).number_format = "0.0%"
            ws.cell(row=r, column=14).number_format = "$#,##0.00"
    ws.freeze_panes = "C3"
    _widths(ws, [5, 34, 15, 9, 16, 12, 8, 7, 10, 7, 10, 10, 7, 10, 40])


def sheet_group_opportunity(wb, rows):
    ws = wb.create_sheet("Group_Opportunity")
    ws["A1"] = ("Realized demand by query group (V3 taxonomy), ranked by UNCOVERED clicks = demand "
                "you're exposed to but NOT actively targeting (no ENABLED-all-3-levels keyword). "
                "USIA-independent. 'Volume' = your realized impressions/clicks, not absolute KWP volume.")
    ws["A1"].font = NOTE_FONT; ws.merge_cells("A1:M1"); ws.row_dimensions[1].height = 40
    ws["A1"].alignment = Alignment(wrap_text=True, vertical="top")
    headers = ["Query Group", "Queries", "Impressions", "Clicks", "Cost", "Leads", "Value",
               "Covered clicks %", "Uncovered clicks", "CPL", "CvR", "RPL", "ROAS"]
    _hdr(ws, headers, row=2)
    first = 3
    for i, d in enumerate(rows):
        r = first + i
        ws.cell(row=r, column=1, value=d["group"])
        ws.cell(row=r, column=2, value=d["queries"])
        ws.cell(row=r, column=3, value=round(d["impressions"]))
        ws.cell(row=r, column=4, value=round(d["clicks"]))
        ws.cell(row=r, column=5, value=round(d["cost"], 2))
        ws.cell(row=r, column=6, value=round(d["conv"], 2))
        ws.cell(row=r, column=7, value=round(d["value"], 2))
        ws.cell(row=r, column=8, value=(d["cov_clicks"] / d["clicks"]) if d["clicks"] else 0)
        ws.cell(row=r, column=9, value=round(d["uncov_clicks"]))
        ws.cell(row=r, column=10, value="=IFERROR(E%d/F%d,\"\")" % (r, r))
        ws.cell(row=r, column=11, value="=IFERROR(F%d/D%d,\"\")" % (r, r))
        ws.cell(row=r, column=12, value="=IFERROR(G%d/F%d,\"\")" % (r, r))
        ws.cell(row=r, column=13, value="=IFERROR(G%d/E%d,\"\")" % (r, r))
    last = max(first, first + len(rows) - 1)
    if rows:
        _style(ws, first, last, len(headers))
        for r in range(first, last + 1):
            ws.cell(row=r, column=5).number_format = "$#,##0"
            ws.cell(row=r, column=7).number_format = "$#,##0"
            ws.cell(row=r, column=8).number_format = "0.0%"
            ws.cell(row=r, column=10).number_format = "$#,##0.00"
            ws.cell(row=r, column=11).number_format = "0.0%"
            ws.cell(row=r, column=12).number_format = "$#,##0.00"
            ws.cell(row=r, column=13).number_format = '0.00"x"'
    ws.freeze_panes = "A3"
    _widths(ws, [34, 9, 12, 9, 11, 8, 12, 15, 15, 10, 8, 10, 8])


def sheet_gap(wb, rows):
    ws = wb.create_sheet("USIA_Target_Gap")
    ws["A1"] = ("USIA targets (generic + carrier) NOT actively covered in Quote.com. coverage=paused "
                "-> keyword exists but switched off (re-enable, fast win); coverage=absent -> no keyword. "
                "Quote perf shown where the term also appears in account-1 realized demand.")
    ws["A1"].font = NOTE_FONT; ws.merge_cells("A1:L1"); ws.row_dimensions[1].height = 40
    ws["A1"].alignment = Alignment(wrap_text=True, vertical="top")
    headers = ["USIA keyword", "Theme", "Source", "Brand", "USIA vol", "Coverage", "Where (if paused)",
               "Q clicks", "Q cost", "Q leads", "Q value", "Q CPL"]
    _hdr(ws, headers, row=2)
    first = 3
    for i, d in enumerate(rows):
        r = first + i
        ws.cell(row=r, column=1, value=d["keyword"])
        ws.cell(row=r, column=2, value=d["theme"])
        ws.cell(row=r, column=3, value=d["source"])
        ws.cell(row=r, column=4, value=d["brand"])
        ws.cell(row=r, column=5, value=d["usia_volume"])
        ws.cell(row=r, column=6, value=d["coverage"])
        ws.cell(row=r, column=7, value=d["where"])
        ws.cell(row=r, column=8, value=round(d["q_clicks"]))
        ws.cell(row=r, column=9, value=round(d["q_cost"], 2))
        ws.cell(row=r, column=10, value=round(d["q_conv"], 2))
        ws.cell(row=r, column=11, value=round(d["q_value"], 2))
        ws.cell(row=r, column=12, value="=IFERROR(I%d/J%d,\"\")" % (r, r))
    last = max(first, first + len(rows) - 1)
    if rows:
        _style(ws, first, last, len(headers))
        for r in range(first, last + 1):
            ws.cell(row=r, column=5).number_format = "#,##0"
            ws.cell(row=r, column=9).number_format = "$#,##0.00"
            ws.cell(row=r, column=11).number_format = "$#,##0.00"
            ws.cell(row=r, column=12).number_format = "$#,##0.00"
    ws.freeze_panes = "A3"
    _widths(ws, [34, 15, 9, 16, 10, 10, 38, 9, 10, 8, 10, 9])


def sheet_disagreement(wb, dis):
    ws = wb.create_sheet("08_Disagreement")
    ws["A1"] = "08 tagging audit -- where trusted target_theme / registry-brand diverge from 08's stored tags."
    ws["A1"].font = NOTE_FONT; ws.merge_cells("A1:G1"); ws.row_dimensions[1].height = 22
    ws["A2"] = "rows tagged"; ws["B2"] = dis["total"]
    ws["A3"] = "theme disagreements"; ws["B3"] = dis["theme_disagree"]; ws["C3"] = "=IFERROR(B3/B2,\"\")"; ws["C3"].number_format = "0.0%"
    ws["A4"] = "brand disagreements"; ws["B4"] = dis["brand_disagree"]; ws["C4"] = "=IFERROR(B4/B2,\"\")"; ws["C4"].number_format = "0.0%"
    for cr in ("A2", "A3", "A4"):
        ws[cr].font = Font(name=FONT, bold=True, size=10)
    headers = ["Query", "08 theme", "target_theme", "08 modifier_brand", "registry brand", "theme\u2260", "brand\u2260"]
    _hdr(ws, headers, row=6)
    first = 7
    for i, d in enumerate(dis["sample"]):
        r = first + i
        ws.cell(row=r, column=1, value=d["query_text"]); ws.cell(row=r, column=2, value=d["theme_08"])
        ws.cell(row=r, column=3, value=d["target_theme"]); ws.cell(row=r, column=4, value=d["modifier_brand_08"])
        ws.cell(row=r, column=5, value=d["registry_brand"])
        ws.cell(row=r, column=6, value="Y" if d["theme_disagree"] else "")
        ws.cell(row=r, column=7, value="Y" if d["brand_disagree"] else "")
    last = max(first, first + len(dis["sample"]) - 1)
    if dis["sample"]:
        _style(ws, first, last, len(headers))
    ws.freeze_panes = "A7"
    _widths(ws, [40, 16, 16, 18, 16, 8, 8])


def dump_csv(path, headers, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(headers); w.writerows(rows)


# --------------------------------------------------------------------------- main
def run(db, usia_csv, brand_csv, out_dir, days, min_clicks, min_conv, brand_db, run_date):
    os.makedirs(out_dir, exist_ok=True)
    print("opening DB (read-only): %s" % os.path.abspath(db))
    if brand_csv and not os.path.exists(brand_csv):
        print("  [WARN] --brand-csv '%s' NOT FOUND -- carrier Set 1 not loaded; gap sheet will be "
              "generic-only. Fix the path/filename and re-run." % brand_csv)
    conn = ro_connect(db)
    tagger = make_brand_tagger(brand_db or db)

    usia_by_theme, usia_flat = load_usia_all(usia_csv, brand_csv)
    n_carrier = sum(1 for r in usia_flat if r.get("source") == "carrier")
    print("Set 1 sources: %d generic + %d carrier" % (len(usia_flat) - n_carrier, n_carrier))
    serving_keys, existence_map, serving_counts, n_ki = load_covering(conn, tagger)
    set3 = load_set3(conn, days)
    print("Set 1 USIA (gen+carrier): %d | Set 2 instances: %d (serving keys %d, existence keys %d) | "
          "Set 3 realized: %d" % (len(usia_flat), n_ki, len(serving_keys), len(existence_map), len(set3)))

    tmap = build_theme_map(usia_by_theme, serving_counts, set3)
    group_opp = build_group_opportunity(set3, serving_keys, tagger)
    brand_adds, nonbrand_adds, dropped = build_adds(set3, serving_keys, existence_map, tagger, min_clicks, min_conv)
    gap = build_gap(usia_flat, serving_keys, existence_map, set3)
    dis = load_disagreements(conn, tagger)
    conn.close()

    print("top untapped groups (by uncovered clicks): " + ", ".join(
        "%s=%d" % (g["group"].split(".")[0].split(":")[0].strip() or g["group"][:10], round(g["uncov_clicks"]))
        for g in group_opp[:5]))

    def _n(rows, act):
        return sum(1 for r in rows if r["action"] == act)
    core_share = (sum(1 for r in set3 if r["target_theme"] == "CORE") / len(set3)) if set3 else 0
    print("adds: nonbrand %d (add %d / reenable %d) | brand %d (add %d / reenable %d) | "
          "gap rows %d | Set-3 CORE %.1f%% | dropped %s"
          % (len(nonbrand_adds), _n(nonbrand_adds, "add_new"), _n(nonbrand_adds, "reenable_existing"),
             len(brand_adds), _n(brand_adds, "add_new"), _n(brand_adds, "reenable_existing"),
             len(gap), 100 * core_share, dropped))

    wb = Workbook()
    params = {
        "run_date": run_date, "source_db": os.path.abspath(db),
        "usia_generic_csv": os.path.abspath(usia_csv),
        "usia_carrier_csv": os.path.abspath(brand_csv) if brand_csv else "(none)",
        "trailing_window_days": days, "min_support_clicks": min_clicks, "min_support_conv": min_conv,
        "device_filter": "ALL", "truth_metric": "primary_conv_count / primary_conv_value",
        "covering_rule": "ENABLED at keyword AND ad_group AND campaign (join on id AND account_id=1)",
        "Set1_USIA_targets": len(usia_flat), "Set2_instances": n_ki, "Set2_serving_keys": len(serving_keys),
        "Set3_realized_queries": len(set3), "Set3_CORE_share": "%.1f%%" % (100 * core_share),
    }
    caveats = [
        "Covering is three-tier: actively covered = ENABLED at keyword+ad_group+campaign (dropped); "
        "reenable_existing = keyword exists but paused somewhere (Where column); add_new = no keyword. "
        "'Not covered' still may serve on an existing broad/phrase keyword -- match simulation deferred.",
        "QC serving counts in the theme map are ENABLED-at-all-3-levels distinct keywords, not raw "
        "instance rows -- so they reflect what is actually live, not historical/paused duplicates.",
        "GENERIC_HOME splits generic home-insurance head terms out of CORE so the theme map can "
        "differentiate; it applies only where assign_theme returned CORE and the text is recognizably home.",
        "Brand add/gap rows carry bid_status -- restricted brands are generally NOT biddable; treat "
        "those as informational, not actionable. Navigate/login queries are suppressed from adds.",
        "Nonbrand still has residual long-tail carrier noise (carriers not enumerated in the registry); "
        "the deferred fuzzy carrier layer will scrub it.",
        "Set 1 uses product_code as an OUT-scope exclusion (empties retained); export lacks "
        "product_ambiguous, so ~26 rows may be over-excluded vs the live selector. Read-only analysis.",
    ]
    sheet_readme(wb, params, caveats)
    sheet_theme_map(wb, tmap)
    sheet_group_opportunity(wb, group_opp)
    sheet_gap(wb, gap)
    sheet_adds(wb, "Adds_NonBrand", nonbrand_adds, "Residual carrier noise remains (see README).", min_clicks, min_conv)
    sheet_adds(wb, "Adds_Brand", brand_adds, "Check Bid status before acting -- restricted brands aren't biddable.", min_clicks, min_conv)
    sheet_disagreement(wb, dis)

    xlsx_path = os.path.join(out_dir, "quote_v_usia_parity.xlsx")
    wb.save(xlsx_path)

    dump_csv(os.path.join(out_dir, "theme_underbuilt_map.csv"),
             ["theme", "usia", "qc_serving_nonbrand", "qc_serving_brand", "clicks", "cost", "conv", "value", "roas"],
             [[d["theme"], d["usia"], d["qc_nonbrand"], d["qc_brand"], round(d["clicks"]),
               round(d["cost"], 2), round(d["conv"], 2), round(d["value"], 2),
               (round(d["value"]/d["cost"], 2) if d["cost"] else "")] for d in tmap])
    for name, rows in (("adds_nonbrand", nonbrand_adds), ("adds_brand", brand_adds)):
        dump_csv(os.path.join(out_dir, name + ".csv"),
                 ["rank", "query", "theme", "group", "action", "bid_status", "impressions", "clicks",
                  "cost", "conv", "value", "where"],
                 [[i + 1, d["query_text"], d["target_theme"], d["target_group"], d["action"], d.get("bid_status", ""),
                   round(d["impressions"]), round(d["clicks"]), round(d["cost"], 2), round(d["conv"], 2),
                   round(d["conv_value"], 2), d.get("where", "")] for i, d in enumerate(rows)])
    dump_csv(os.path.join(out_dir, "group_opportunity.csv"),
             ["query_group", "queries", "impressions", "clicks", "cost", "leads", "value",
              "covered_clicks_pct", "uncovered_clicks"],
             [[d["group"], d["queries"], round(d["impressions"]), round(d["clicks"]), round(d["cost"], 2),
               round(d["conv"], 2), round(d["value"], 2),
               round(d["cov_clicks"] / d["clicks"], 4) if d["clicks"] else 0, round(d["uncov_clicks"])]
              for d in group_opp])
    dump_csv(os.path.join(out_dir, "usia_target_gap.csv"),
             ["keyword", "theme", "source", "brand", "usia_volume", "coverage", "where",
              "q_clicks", "q_cost", "q_conv", "q_value"],
             [[d["keyword"], d["theme"], d["source"], d["brand"], d["usia_volume"], d["coverage"], d["where"],
               round(d["q_clicks"]), round(d["q_cost"], 2), round(d["q_conv"], 2), round(d["q_value"], 2)] for d in gap])

    print("wrote: %s (+ CSV dumps) -> %s" % (os.path.basename(xlsx_path), out_dir))
    return xlsx_path


def main():
    p = argparse.ArgumentParser(description="Quote.com <-> USIA three-set parity join (read-only).")
    p.add_argument("--db", required=True, help="paid_search.db (opened read-only)")
    p.add_argument("--usia-csv", required=True, help="kwp_ideas_export_v2.csv (generic Set-1 source)")
    p.add_argument("--brand-csv", default=None, help="usia_home_brand_generic.csv (carrier Set-1 source)")
    p.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    p.add_argument("--brand-db", default=None, help="registry DB for BrandTagger (default: --db)")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--min-clicks", type=int, default=20)
    p.add_argument("--min-conv", type=int, default=2)
    p.add_argument("--run-date", default=str(date.today()))
    a = p.parse_args()
    run(a.db, a.usia_csv, a.brand_csv, a.out_dir, a.days, a.min_clicks, a.min_conv, a.brand_db, a.run_date)


if __name__ == "__main__":
    main()
