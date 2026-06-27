#!/usr/bin/env python
"""
Build the USIA demand-research analysis workbook from demand_research.db.

Reads the loaded dry-run data and produces a multi-sheet .xlsx:
  Summary        key findings + caveats
  State Demand   per-state biddable home demand (geo-loop), ranked
  Seed Categories intent-modifier taxonomy by volume (home, national)
  Brand & DNB    carrier exposure + do-not-bid volume (home, national)
  Contact Wedge  biddable home-contact demand, brand-nav stripped (high_intent)
  Home vs Renters head-term demand + granularity split (state vs city)

Values are query results (source = demand_research.db, frozen at pull date);
within-sheet shares / cumulatives / totals are Excel formulas so they stay live.

USAGE
  python build_demand_workbook.py
  python build_demand_workbook.py --db C:\\Projects\\google-ads-ops\\db\\demand_research.db --out demand_review.xlsx
"""

import argparse
import csv
import json
import os
import re
import sqlite3

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

# ---- classification (mirrors the puller; used only for in-workbook breakdowns) ----
MODIFIER_RE = {
    "quote":      re.compile(r"\b(quote|quotes|quotation)\b"),
    "comparison": re.compile(r"\b(best|compare|comparison|companies|company|top|reviews?|ratings?)\b"),
    "coverage":   re.compile(r"\b(dwelling|liability|hazard|flood|fire|coverage|policy|deductible|replacement)\b"),
    "price":      re.compile(r"\b(cheap|cheapest|affordable|inexpensive|low cost|low-cost|discount|budget)\b"),
    "cost":       re.compile(r"\b(cost|costs|average|how much|rate|rates|premium|pricing)\b"),
    "contact":    re.compile(r"\b(near me|agent|agents|agency|agencies|broker|brokers|phone|call|contact|local|number)\b"),
    "bundle":     re.compile(r"\b(bundle|bundling|home and auto|auto and home|car and home|home and car)\b"),
    "home_type":  re.compile(r"\b(condo|condos|mobile home|manufactured|townhouse|townhome)\b"),
}
CONTACT_RE = MODIFIER_RE["contact"]
US_CITIES = ["new york", "los angeles", "chicago", "houston", "phoenix", "philadelphia",
             "san antonio", "san diego", "dallas", "austin", "fort worth", "jacksonville",
             "san jose", "columbus", "charlotte", "indianapolis", "seattle", "denver",
             "boston", "el paso", "detroit", "nashville", "portland", "memphis",
             "atlanta", "miami", "tampa", "orlando", "sacramento", "san francisco"]
CITY_RE = re.compile(r"\b(" + "|".join(US_CITIES) + r")\b")
STATE_RE = re.compile(r"\b(alabama|alaska|arizona|arkansas|california|colorado|connecticut|"
                      r"delaware|florida|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|"
                      r"kentucky|louisiana|maine|maryland|massachusetts|michigan|minnesota|"
                      r"mississippi|missouri|montana|nebraska|nevada|ohio|oklahoma|oregon|"
                      r"pennsylvania|tennessee|texas|utah|vermont|virginia|washington|"
                      r"wisconsin|wyoming|tx|ca|fl|ny)\b")
ZIP_RE = re.compile(r"\b\d{5}\b")

# US Census 2023 population estimates (thousands) -- for per-capita demand context.
STATE_POP = {
    "Alabama": 5108, "Alaska": 733, "Arizona": 7431, "Arkansas": 3067, "California": 38965,
    "Colorado": 5878, "Connecticut": 3617, "Delaware": 1031, "District of Columbia": 679,
    "Florida": 22610, "Georgia": 11029, "Hawaii": 1435, "Idaho": 1964, "Illinois": 12549,
    "Indiana": 6862, "Iowa": 3207, "Kansas": 2940, "Kentucky": 4526, "Louisiana": 4574,
    "Maine": 1395, "Maryland": 6180, "Massachusetts": 7001, "Michigan": 10037, "Minnesota": 5738,
    "Mississippi": 2940, "Missouri": 6196, "Montana": 1132, "Nebraska": 1978, "Nevada": 3194,
    "New Hampshire": 1402, "New Jersey": 9290, "New Mexico": 2114, "New York": 19571,
    "North Carolina": 10835, "North Dakota": 783, "Ohio": 11785, "Oklahoma": 4053, "Oregon": 4233,
    "Pennsylvania": 12961, "Rhode Island": 1095, "South Carolina": 5373, "South Dakota": 919,
    "Tennessee": 7126, "Texas": 30503, "Utah": 3417, "Vermont": 647, "Virginia": 8716,
    "Washington": 7812, "West Virginia": 1770, "Wisconsin": 5910, "Wyoming": 584,
}


def categorize(t):
    return [k for k, rx in MODIFIER_RE.items() if rx.search(t)]


def granularity(t):
    if ZIP_RE.search(t):
        return "zip"
    if CITY_RE.search(t):
        return "city"
    if STATE_RE.search(t):
        return "state"
    return "none"


# ---- styles ----
HEAD = PatternFill("solid", fgColor="1F3864")
SUBHEAD = PatternFill("solid", fgColor="D6DCE4")
WHITE = Font(name="Arial", bold=True, color="FFFFFF", size=10)
BOLD = Font(name="Arial", bold=True, size=10)
REG = Font(name="Arial", size=10)
NOTE = Font(name="Arial", size=9, italic=True, color="595959")
TITLE = Font(name="Arial", bold=True, size=14, color="1F3864")
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def hdr(ws, row, headers, start=1):
    for j, h in enumerate(headers, start):
        c = ws.cell(row=row, column=j, value=h)
        c.fill = HEAD
        c.font = WHITE
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = BORDER


def setw(ws, widths):
    for col, w in widths.items():
        ws.column_dimensions[col].width = w


# ---- data access ----
def q(conn, sql, params=()):
    cur = conn.cursor()
    cur.execute(sql, params)
    return cur.fetchall()


def distinct_rows(conn, where, params=()):
    """Distinct keyword (max avg) under a WHERE on kwp_ideas; returns [(keyword, avg, brand, dnb)]."""
    sql = """
      SELECT keyword_text, MAX(avg_monthly_searches) av,
             MAX(brand) brand, MAX(dnb_flag) dnb
      FROM kwp_ideas WHERE %s GROUP BY keyword_text
    """ % where
    return q(conn, sql, params)


GEO_LOOP_DATE = None  # detected: the home pull spanning the most geos


def detect_geoloop_date(conn, vertical="home"):
    """Return (pull_date, geo_count) of the pull spanning the most geos for a vertical."""
    rows = q(conn, """SELECT pull_date, COUNT(DISTINCT geo_constant_id) g
                      FROM kwp_ideas WHERE vertical=? GROUP BY pull_date ORDER BY g DESC""", (vertical,))
    return (rows[0][0], rows[0][1]) if rows else (None, 0)


# ---- sheet builders ----
def sheet_state_demand(wb, conn, gl_date, vertical="home"):
    label = vertical.title()
    ws = wb.create_sheet("%s State Demand" % label)
    ws["A1"] = "Per-State Biddable %s Demand" % label
    ws["A1"].font = TITLE
    extra = ("" if vertical == "home" else
             " No %s RPL source (no %s account), so this is a demand/per-capita map only -- no winnability overlay." % (vertical, vertical))
    ws["A2"] = ("on_vertical, not-DNB, distinct keywords (max volume). Source: demand_research.db "
                "geo-loop pull %s. RELATIVE INDEX, not unique searches: KWP buckets close variants into "
                "shared volume pools, so summing keywords overcounts -- but the overcount is constant "
                "across states, so the RANKING and SHARES hold.%s" % (gl_date, extra))
    ws["A2"].font = NOTE
    ws.merge_cells("A2:E2")

    rows = q(conn, """
      SELECT geo_name, SUM(mx) FROM (
        SELECT geo_name, keyword_text, MAX(avg_monthly_searches) mx
        FROM kwp_ideas
        WHERE vertical=? AND vertical_match='on_vertical' AND dnb_flag=0
          AND geo_name!='United States' AND pull_date=?
        GROUP BY geo_name, keyword_text)
      GROUP BY geo_name ORDER BY 2 DESC""", (vertical, gl_date))

    hr = 4
    hdr(ws, hr, ["Rank", "State", "Biddable Demand (mo. searches)", "% of States", "Cumulative %"])
    n = len(rows)
    total_row = hr + 1 + n
    for i, (state, dem) in enumerate(rows):
        r = hr + 1 + i
        ws.cell(r, 1, i + 1).font = REG
        ws.cell(r, 2, state).font = REG
        ws.cell(r, 3, int(dem)).font = REG
        ws.cell(r, 3).number_format = "#,##0"
        ws.cell(r, 4, "=C%d/$C$%d" % (r, total_row)).number_format = "0.0%"
        ws.cell(r, 4).font = REG
        if i == 0:
            ws.cell(r, 5, "=D%d" % r)
        else:
            ws.cell(r, 5, "=E%d+D%d" % (r - 1, r))
        ws.cell(r, 5).number_format = "0.0%"
        ws.cell(r, 5).font = REG
    ws.cell(total_row, 2, "All States Total").font = BOLD
    ws.cell(total_row, 3, "=SUM(C%d:C%d)" % (hr + 1, total_row - 1)).font = BOLD
    ws.cell(total_row, 3).number_format = "#,##0"
    ws.cell(total_row, 4, "=SUM(D%d:D%d)" % (hr + 1, total_row - 1)).font = BOLD
    ws.cell(total_row, 4).number_format = "0.0%"
    setw(ws, {"A": 6, "B": 22, "C": 28, "D": 12, "E": 13})
    ws.freeze_panes = "A%d" % (hr + 1)


def sheet_categories(wb, conn, gl_date, vertical="home"):
    label = vertical.title()
    ws = wb.create_sheet("%s Categories" % label)
    ws["A1"] = "%s Seed-Category (Intent) Taxonomy" % label
    ws["A1"].font = TITLE
    ws["A2"] = ("%s, national, on-vertical only (cross-vertical bleed excluded). "
                "Distinct keywords, multi-label. Largest categories = strongest dedicated-seed candidates." % label)
    ws["A2"].font = NOTE
    ws.merge_cells("A2:E2")

    rows = distinct_rows(conn, "vertical=? AND geo_name='United States' "
                               "AND vertical_match='on_vertical' AND pull_date=?", (vertical, gl_date))
    catc, catv, examp = {}, {}, {}
    for kw, av, *_ in rows:
        for c in categorize(kw):
            catc[c] = catc.get(c, 0) + 1
            catv[c] = catv.get(c, 0) + (av or 0)
            examp.setdefault(c, []).append((kw, av or 0))
    order = sorted(catv, key=lambda c: -catv[c])

    hr = 4
    hdr(ws, hr, ["Category", "Distinct Keywords", "Total Volume", "% of Categorized Vol", "Top example keywords"])
    if not order:
        ws.cell(hr + 1, 1, "(no on-vertical national keywords for this vertical in DB)").font = NOTE
        setw(ws, {"A": 16, "B": 18, "C": 16, "D": 18, "E": 60})
        return
    total_row = hr + 1 + len(order)
    for i, c in enumerate(order):
        r = hr + 1 + i
        ws.cell(r, 1, c).font = REG
        ws.cell(r, 2, catc[c]).font = REG
        ws.cell(r, 3, int(catv[c])).font = REG
        ws.cell(r, 3).number_format = "#,##0"
        ws.cell(r, 4, "=C%d/$C$%d" % (r, total_row)).number_format = "0.0%"
        ws.cell(r, 4).font = REG
        ex = ", ".join(k for k, _ in sorted(examp[c], key=lambda x: -x[1])[:3])
        ws.cell(r, 5, ex).font = NOTE
    ws.cell(total_row, 1, "Total (overlapping)").font = BOLD
    ws.cell(total_row, 3, "=SUM(C%d:C%d)" % (hr + 1, total_row - 1)).font = BOLD
    ws.cell(total_row, 3).number_format = "#,##0"
    setw(ws, {"A": 16, "B": 18, "C": 16, "D": 18, "E": 60})
    ws.freeze_panes = "A%d" % (hr + 1)


def sheet_brand_dnb(wb, conn, gl_date):
    ws = wb.create_sheet("Brand & DNB")
    ws["A1"] = "Carrier Brand & Do-Not-Bid Exposure"
    ws["A1"].font = TITLE
    ws["A2"] = ("Home, national, on-vertical. Every carrier KWP surfaced in this pull (not a top-N cap). "
                "DNB = restricted (landscape only). Note: a carrier absent here is missing from KWP's "
                "returned set for this pull, not necessarily from the brand registry.")
    ws["A2"].font = NOTE
    ws.merge_cells("A2:D2")

    rows = distinct_rows(conn, "vertical='home' AND geo_name='United States' AND brand!='' "
                               "AND vertical_match='on_vertical' AND pull_date=?", (gl_date,))
    bvol, bcnt, bstatus = {}, {}, {}
    dnbvol = 0
    for kw, av, brand, dnb in rows:
        bvol[brand] = bvol.get(brand, 0) + (av or 0)
        bcnt[brand] = bcnt.get(brand, 0) + 1
        if dnb:
            dnbvol += (av or 0)
    for b in bvol:
        st = q(conn, "SELECT brand_bid_status FROM kwp_ideas WHERE brand=? AND brand_bid_status!='' LIMIT 1", (b,))
        bstatus[b] = st[0][0] if st else ""
    top = sorted(bvol, key=lambda b: -bvol[b])

    ws["A4"] = "Total branded keyword volume (home, national):"
    ws["A4"].font = BOLD
    ws["C4"] = int(sum(bvol.values()))
    ws["C4"].number_format = "#,##0"
    ws["C4"].font = BOLD
    ws["A5"] = "...of which DNB (restricted):"
    ws["A5"].font = BOLD
    ws["C5"] = int(dnbvol)
    ws["C5"].number_format = "#,##0"
    ws["C5"].font = Font(name="Arial", bold=True, color="C00000")
    ws["D5"] = "=C5/C4"
    ws["D5"].number_format = "0.0%"
    ws["D5"].font = BOLD
    ws["A6"] = "Distinct carriers surfaced:"
    ws["A6"].font = BOLD
    ws["C6"] = len(top)
    ws["C6"].font = BOLD

    hr = 8
    hdr(ws, hr, ["Carrier", "Bid Status", "Distinct KW", "Total Volume"])
    for i, b in enumerate(top):
        r = hr + 1 + i
        ws.cell(r, 1, b).font = REG
        c2 = ws.cell(r, 2, bstatus.get(b, ""))
        c2.font = Font(name="Arial", size=10, color="C00000" if bstatus.get(b) == "restricted" else "000000")
        ws.cell(r, 3, bcnt[b]).font = REG
        ws.cell(r, 4, int(bvol[b])).font = REG
        ws.cell(r, 4).number_format = "#,##0"
    setw(ws, {"A": 24, "B": 14, "C": 12, "D": 16})
    ws.freeze_panes = "A%d" % (hr + 1)


def sheet_contact_wedge(wb, conn):
    ws = wb.create_sheet("Contact Wedge")
    ws["A1"] = "Home-Contact Wedge (USIA)"
    ws["A1"].font = TITLE
    ws["A2"] = ("High-intent theme, home, national, on_vertical, contact-intent. Clean (non-brand) = biddable "
                "generic wedge. Brand-contact = carrier phone/contact lookups (mostly navigational; DNB flagged).")
    ws["A2"].font = NOTE
    ws.merge_cells("A2:D2")

    rows = q(conn, """
      SELECT i.keyword_text, MAX(i.avg_monthly_searches) av, MAX(i.brand) brand, MAX(i.dnb_flag) dnb
      FROM kwp_ideas i JOIN kwp_idea_provenance p ON i.idea_id=p.idea_id
      WHERE p.theme='high_intent' AND i.vertical='home' AND i.geo_name='United States'
        AND i.vertical_match='on_vertical'
      GROUP BY i.keyword_text""")
    contact = [(kw, av, brand, dnb) for kw, av, brand, dnb in rows if CONTACT_RE.search(kw)]
    clean = [(kw, av) for kw, av, brand, dnb in contact if not brand]
    brandc = [(kw, av, brand, dnb) for kw, av, brand, dnb in contact if brand]

    ws["A4"] = "All contact-intent terms:"
    ws["A4"].font = BOLD
    ws["C4"] = int(sum(a for _, a, _, _ in contact)); ws["C4"].number_format = "#,##0"; ws["C4"].font = BOLD
    ws["A5"] = "  CLEAN generic home-contact wedge (biddable):"
    ws["A5"].font = Font(name="Arial", bold=True, color="1F7A1F")
    ws["C5"] = int(sum(a for _, a in clean)); ws["C5"].number_format = "#,##0"
    ws["C5"].font = Font(name="Arial", bold=True, color="1F7A1F")
    ws["D5"] = "%d terms" % len(clean); ws["D5"].font = NOTE
    ws["A6"] = "  brand-contact (carrier lookups):"
    ws["A6"].font = REG
    ws["C6"] = int(sum(a for _, a, _, _ in brandc)); ws["C6"].number_format = "#,##0"; ws["C6"].font = REG
    ws["D6"] = "%d terms" % len(brandc); ws["D6"].font = NOTE

    hr = 8
    ws.cell(hr - 1, 1, "Clean (non-brand) home-contact terms -- the biddable wedge").font = BOLD
    hdr(ws, hr, ["Keyword", "Monthly Searches"])
    cl = sorted(clean, key=lambda x: -x[1])[:40]
    for i, (kw, av) in enumerate(cl):
        r = hr + 1 + i
        ws.cell(r, 1, kw).font = REG
        ws.cell(r, 2, int(av)).font = REG
        ws.cell(r, 2).number_format = "#,##0"

    # Brand Contact Wedge
    br0 = hr + 1 + len(cl) + 2
    ws.cell(br0 - 1, 1, "Brand Contact Wedge -- carrier phone/contact lookups (landscape; DNB = not biddable)").font = BOLD
    hdr(ws, br0, ["Keyword", "Monthly Searches", "Carrier", "DNB"])
    for i, (kw, av, brand, dnb) in enumerate(sorted(brandc, key=lambda x: -x[1])[:30]):
        r = br0 + 1 + i
        ws.cell(r, 1, kw).font = REG
        ws.cell(r, 2, int(av)).font = REG; ws.cell(r, 2).number_format = "#,##0"
        ws.cell(r, 3, brand).font = REG
        c4 = ws.cell(r, 4, "DNB" if dnb else "")
        c4.font = Font(name="Arial", size=10, color="C00000")
    setw(ws, {"A": 42, "B": 18, "C": 18, "D": 6})
    ws.freeze_panes = "A%d" % (hr + 1)


def sheet_home_vs_renters(wb, conn):
    ws = wb.create_sheet("Home vs Renters")
    ws["A1"] = "Home vs Renters: Demand & Granularity"
    ws["A1"].font = TITLE
    ws["A2"] = ("National head terms + geo-qualified granularity split. Source: demand_research.db. "
                "Granularity from generic_geo pulls (volume share by geo grain).")
    ws["A2"].font = NOTE
    ws.merge_cells("A2:E2")

    def head_vol(vert, kw):
        r = q(conn, "SELECT MAX(avg_monthly_searches) FROM kwp_ideas WHERE vertical=? AND keyword_text=? AND geo_name='United States'",
              (vert, kw))
        return int(r[0][0]) if r and r[0][0] else 0

    ws["A4"] = "Head-term demand (national):"
    ws["A4"].font = BOLD
    hdr(ws, 5, ["Vertical", "Head Term", "Monthly Searches"])
    heads = [("home", "home insurance"), ("home", "homeowners insurance"),
             ("renters", "renters insurance"), ("renters", "renters insurance quote")]
    for i, (v, kw) in enumerate(heads):
        r = 6 + i
        ws.cell(r, 1, v).font = REG
        ws.cell(r, 2, kw).font = REG
        ws.cell(r, 3, head_vol(v, kw)).font = REG
        ws.cell(r, 3).number_format = "#,##0"

    # granularity per vertical from generic_geo theme rows
    start = 12
    ws.cell(start - 1, 1, "Geo-qualified granularity (volume share by grain):").font = BOLD
    hdr(ws, start, ["Vertical", "Grain", "Distinct KW", "Volume", "% of Vol"])
    rr = start + 1
    for vert in ("home", "renters"):
        rows = q(conn, """
          SELECT i.keyword_text, MAX(i.avg_monthly_searches) av
          FROM kwp_ideas i JOIN kwp_idea_provenance p ON i.idea_id=p.idea_id
          WHERE p.theme='generic_geo' AND i.vertical=? AND i.vertical_match='on_vertical'
          GROUP BY i.keyword_text""", (vert,))
        gc, gv = {}, {}
        for kw, av in rows:
            g = granularity(kw)
            gc[g] = gc.get(g, 0) + 1
            gv[g] = gv.get(g, 0) + (av or 0)
        tot = sum(gv.values())
        if tot == 0:
            ws.cell(rr, 1, vert).font = REG
            ws.cell(rr, 2, "(no generic_geo pull in DB)").font = NOTE
            rr += 1
            continue
        for g in ("none", "state", "city", "zip"):
            if gc.get(g):
                ws.cell(rr, 1, vert).font = REG
                ws.cell(rr, 2, g).font = REG
                ws.cell(rr, 3, gc[g]).font = REG
                ws.cell(rr, 4, int(gv[g])).font = REG
                ws.cell(rr, 4).number_format = "#,##0"
                ws.cell(rr, 5, round(100.0 * gv[g] / tot, 1) / 100).number_format = "0.0%"
                ws.cell(rr, 5).font = REG
                rr += 1
    setw(ws, {"A": 12, "B": 26, "C": 12, "D": 14, "E": 10})


def sheet_summary(wb, conn, gl_date, has_overlay=False):
    ws = wb.create_sheet("Summary")
    ws["A1"] = "USIA Demand Research -- Step 1 Findings"
    ws["A1"].font = TITLE
    counts = {t: q(conn, "SELECT COUNT(*) FROM %s" % t)[0][0]
              for t in ("kwp_ideas", "kwp_idea_provenance", "idea_monthly_volumes")}
    lines = [
        ("Database", "demand_research.db  (separate from paid_search.db)"),
        ("Ideas / provenance / monthly", "%s / %s / %s" % (f"{counts['kwp_ideas']:,}",
         f"{counts['kwp_idea_provenance']:,}", f"{counts['idea_monthly_volumes']:,}")),
        ("Geo-loop snapshot", "%s (national + 50 states + DC)" % gl_date),
        ("", ""),
        ("KEY FINDINGS", ""),
        ("Demand tracks population", "Top 10 states ~50% of all demand; TX/CA/FL/NY lead."),
    ]
    if has_overlay:
        lines += [
            ("Value reorders the map", "TX leads on demand AND RPL; FL/CA/NY are demand giants but value "
             "laggards (low RPL); IL/NJ/PA/OH/CO/MA climb on high per-lead value."),
        ]
    lines += [
        ("Quote intent dominates", "Largest seed-category by volume -- top dedicated-seed candidate."),
        ("Contact wedge is thin", "Biddable generic home-contact ~100-135k/mo; ~2-3% of home demand."),
        ("Home state vs renters city", "Home demand skews state-level; renters skews city-level."),
        ("DNB exposure", "Restricted carriers (Lemonade, etc.) carved out as landscape-only."),
        ("", ""),
        ("CAVEATS", ""),
        ("Volume != winnability", "Raw volume ~ population; the Winnability Overlay adds RPL to rank launch states."),
        ("Trend column is seasonal", "12-month KWP array can't separate trend from season; YoY needs the historical-metrics endpoint."),
        ("RPL is a directional proxy", "Geo RPL is native-conversions (PF2 gap >5%); rank on RPL shape, not absolute $."),
        ("RPL is realized-where-we-bid", "Reflects current Home tROAS/structure, not latent USIA economics; thin states flagged."),
        ("Granularity is a lower bound", "Rounding floor + per-request cap truncate the city/zip tail."),
        ("Volume is bucketed, not unique", "~44 volume levels; ~92% in buckets shared by close variants. Demand is a relative index, not unique-search counts."),
        ("Renters coverage is sparse", "National + Texas only; needs a renters geo-loop for parity with home."),
    ]
    r = 3
    for k, v in lines:
        is_section = k.isupper() and k != ""
        ws.cell(r, 1, k).font = BOLD if (is_section or k in (
            "Database", "Ideas / provenance / monthly", "Geo-loop snapshot")) else REG
        if is_section:
            ws.cell(r, 1).fill = SUBHEAD
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=4)
        else:
            ws.cell(r, 2, v).font = REG
        r += 1
    ws.merge_cells("A1:D1")
    setw(ws, {"A": 30, "B": 70})


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def state_demand(conn, gl_date):
    rows = q(conn, """
      SELECT geo_name, SUM(mx) FROM (
        SELECT geo_name, keyword_text, MAX(avg_monthly_searches) mx
        FROM kwp_ideas
        WHERE vertical='home' AND vertical_match='on_vertical' AND dnb_flag=0
          AND geo_name!='United States' AND pull_date=?
        GROUP BY geo_name, keyword_text)
      GROUP BY geo_name""", (gl_date,))
    return {s: int(v) for s, v in rows}


def sheet_overlay(wb, conn, gl_date, rpl_csv):
    ws = wb.create_sheet("Winnability Overlay")
    rpl, basis, win = {}, "", ("", "")
    with open(rpl_csv, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            basis = r.get("rpl_basis", "")
            win = (r.get("window_start", ""), r.get("window_end", ""))
            rpl[r["geo_state"]] = {"rpl": _f(r["RPL"]), "cpc": _f(r["CPC"]), "cvr": _f(r["CvR"]),
                                   "conv": _f(r["conversions"]), "low": int(r.get("low_volume") or 0),
                                   "rev": _f(r["revenue"]), "cost": _f(r["cost"]), "clk": _f(r["clicks"])}
    dem = state_demand(conn, gl_date)

    ranked = sorted([s for s in dem if s in rpl and rpl[s]["rpl"] and not rpl[s]["low"]],
                    key=lambda s: -(dem[s] * rpl[s]["rpl"]))
    thin = [s for s in dem if s in rpl and rpl[s]["low"]]
    nosig = sorted([s for s in dem if s not in rpl], key=lambda s: -dem[s])
    demrank = {s: i + 1 for i, (s, _) in enumerate(sorted(dem.items(), key=lambda x: -x[1]))}

    ws["A1"] = "Launch Winnability Overlay -- Home"
    ws["A1"].font = TITLE
    proxy = basis != "reconciled_native≈primary"
    ws["A2"] = ("Demand (demand_research.db) x realized Home RPL (geo_state_rpl_overlay.csv, "
                "%s..%s). RPL basis: %s%s" % (win[0], win[1], basis,
                "  -- DIRECTIONAL PROXY (native conversions, level understated; rank on shape, not $)."
                if proxy else "  -- reconciled to pixel-primary."))
    ws["A2"].font = Font(name="Arial", size=9, italic=True, color="C00000" if proxy else "595959")
    ws.merge_cells("A2:Q2")
    ws["A3"] = ("RPL is realized WHERE HOME ALREADY BIDS (shaped by current tROAS/structure), not latent "
                "USIA economics. Value Index = Demand x RPL, indexed to 100. Thin/no-signal states listed below, not ranked.")
    ws["A3"].font = NOTE
    ws.merge_cells("A3:Q3")
    ws["A4"] = ("Profitability lens: ROAS = revenue/cost (native basis ~15% light; x1.15 for pixel-primary; "
                "RELATIVE order holds). Auction prices value in (RPL~CPC corr +0.87), so high RPL != high margin -- "
                "read ROAS/Margin-per-Click for unit economics. ROAS reflects Home's current bidding.")
    ws["A4"].font = Font(name="Arial", size=9, italic=True, color="595959")
    ws.merge_cells("A4:Q4")

    cols = ["Value Rank", "State", "Biddable Demand", "Demand Rank", "Rank Shift",
            "RPL ($/lead, proxy)", "CPC ($)", "CvR", "Leads (window)", "Pop (000s)",
            "Demand /100k", "Value Opp (Dem x RPL)", "Value Index",
            "Rev/Click ($)", "Margin/Click ($)", "ROAS", "Flag"]
    hr = 6
    hdr(ws, hr, cols)
    r0 = hr + 1
    for i, s in enumerate(ranked):
        r = r0 + i
        d = rpl[s]
        ws.cell(r, 1, i + 1).font = REG
        ws.cell(r, 2, s).font = REG
        ws.cell(r, 3, dem[s]).font = REG; ws.cell(r, 3).number_format = "#,##0"
        ws.cell(r, 4, demrank[s]).font = REG
        ws.cell(r, 5, "=D%d-A%d" % (r, r)).font = REG  # + => value-promoted
        ws.cell(r, 6, round(d["rpl"], 2)).font = REG; ws.cell(r, 6).number_format = "$#,##0.00"
        ws.cell(r, 7, round(d["cpc"], 2) if d["cpc"] else None).font = REG; ws.cell(r, 7).number_format = "$#,##0.00"
        ws.cell(r, 8, round(d["cvr"], 4) if d["cvr"] else None).font = REG; ws.cell(r, 8).number_format = "0.0%"
        ws.cell(r, 9, int(d["conv"]) if d["conv"] else None).font = REG; ws.cell(r, 9).number_format = "#,##0"
        pop = STATE_POP.get(s)
        ws.cell(r, 10, pop).font = REG; ws.cell(r, 10).number_format = "#,##0"
        ws.cell(r, 11, "=C%d*100/J%d" % (r, r) if pop else None).number_format = "#,##0.0"
        ws.cell(r, 11).font = REG
        ws.cell(r, 12, "=C%d*F%d" % (r, r)).font = REG; ws.cell(r, 12).number_format = "#,##0"
        last = r0 + len(ranked) - 1
        ws.cell(r, 13, "=L%d/MAX($L$%d:$L$%d)*100" % (r, r0, last)).font = REG
        ws.cell(r, 13).number_format = "0.0"
        # profitability lens
        clk, rev, cost = d.get("clk"), d.get("rev"), d.get("cost")
        ws.cell(r, 14, round(rev / clk, 2) if clk else None).font = REG
        ws.cell(r, 14).number_format = "$#,##0.00"
        ws.cell(r, 15, "=N%d-G%d" % (r, r) if clk else None).font = REG  # rev/click - CPC
        ws.cell(r, 15).number_format = "$#,##0.00"
        roas = (rev / cost) if cost else None
        c16 = ws.cell(r, 16, round(roas, 2) if roas else None)
        c16.number_format = "0.00"
        c16.font = Font(name="Arial", size=10, color="C00000" if (roas and roas < 1.0) else "000000")
        sh = demrank[s] - (i + 1)
        ws.cell(r, 17, "value+" if sh > 0 else ("demand+" if sh < 0 else "")).font = REG

    # thin + no-signal blocks
    r = r0 + len(ranked) + 1
    ws.cell(r, 2, "Thin RPL (low conversions -- not ranked)").font = BOLD
    ws.cell(r, 2).fill = SUBHEAD
    r += 1
    for s in sorted(thin, key=lambda s: -dem[s]):
        ws.cell(r, 2, s).font = REG
        ws.cell(r, 3, dem[s]).font = REG; ws.cell(r, 3).number_format = "#,##0"
        ws.cell(r, 6, round(rpl[s]["rpl"], 2) if rpl[s]["rpl"] else None).number_format = "$#,##0.00"
        ws.cell(r, 6).font = REG
        ws.cell(r, 9, int(rpl[s]["conv"]) if rpl[s]["conv"] else None).number_format = "#,##0"
        ws.cell(r, 9).font = REG
        ws.cell(r, 17, "thin").font = Font(name="Arial", size=10, color="C00000")
        r += 1
    r += 1
    ws.cell(r, 2, "No realized RPL signal (Home under-invested / absent)").font = BOLD
    ws.cell(r, 2).fill = SUBHEAD
    r += 1
    for s in nosig:
        ws.cell(r, 2, s).font = REG
        ws.cell(r, 3, dem[s]).font = REG; ws.cell(r, 3).number_format = "#,##0"
        ws.cell(r, 17, "latent").font = NOTE
        r += 1

    setw(ws, {"A": 10, "B": 20, "C": 16, "D": 12, "E": 10, "F": 16, "G": 9, "H": 8,
              "I": 13, "J": 11, "K": 12, "L": 18, "M": 11, "N": 13, "O": 15, "P": 8, "Q": 9})
    ws.freeze_panes = "A%d" % r0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=r"C:\Projects\google-ads-ops\db\demand_research.db")
    p.add_argument("--out", default="usia_demand_review.xlsx")
    p.add_argument("--rpl-csv", default=None,
                   help="geo_state_rpl_overlay.csv from analyze_geo_state_device.py --overlay; "
                        "adds the Winnability Overlay sheet.")
    args = p.parse_args()

    conn = sqlite3.connect(args.db)
    gl_date, _ = detect_geoloop_date(conn, "home")
    rent_date, rent_geos = detect_geoloop_date(conn, "renters")

    wb = Workbook()
    wb.remove(wb.active)
    sheet_summary(wb, conn, gl_date, bool(args.rpl_csv))
    if args.rpl_csv:
        sheet_overlay(wb, conn, gl_date, args.rpl_csv)
    sheet_state_demand(wb, conn, gl_date, "home")
    sheet_categories(wb, conn, gl_date, "home")
    if rent_geos > 1:  # renters geo-loop loaded -> add per-state renters sheets
        sheet_state_demand(wb, conn, rent_date, "renters")
        sheet_categories(wb, conn, rent_date, "renters")
    else:
        print("note: no renters geo-loop in DB (geos=%d) -- skipping renters per-state sheets. "
              "Load usia_dryrun_renters_generic_*_states_natl.csv first." % rent_geos)
    sheet_brand_dnb(wb, conn, gl_date)
    sheet_contact_wedge(wb, conn)
    sheet_home_vs_renters(wb, conn)
    conn.close()
    wb.save(args.out)
    print("wrote %s" % args.out)


if __name__ == "__main__":
    main()
