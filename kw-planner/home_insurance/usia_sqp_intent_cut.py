#!/usr/bin/env python
"""
usia_sqp_intent_cut.py -- cut REAL Google Home search-query data by the non-standard
intent buckets borrowed from home_insurance_keyword_analysisV3.py, to test whether the
"hard-to-place" segments (the agent-assisted wedge) actually carry volume + RPL.

Reads search_query_performance (sqp) in paid_search.db -- realized served/converted
queries on the Home account (account_id=1) -- NOT the KWP estimates. Classifies each
distinct query into one of five borrowed buckets (policy distress, high-risk, fire/FAIR,
flood/wind/catastrophe, does-carrier) via regex, falling through to "Standard"; reports
impressions / clicks / cost / leads / revenue / RPL / CvR / CPL per bucket.

READ-ONLY. Self-verifies schema (PRAGMA) before querying -- the architect warned this DB
has had column surprises, so the join column and tag columns are resolved, not assumed.

Conventions enforced (per the architect): scope via keyword_instances.account_id=1 (sqp
has no account_id); device='ALL'; cost_micros/1e6; primary_conv_* are the truth metrics;
rows are per-match-type so we GROUP to collapse them.

    python usia_sqp_intent_cut.py --start 2026-01-01 --end 2026-06-07 --out usia_sqp_intent_ytd.xlsx
"""
import argparse
import re
import sqlite3
import sys

# --- five borrowed buckets (adapted, not copied) + a VS/versus informational flag ---
BUCKETS = [
    ("Policy distress / cancellation", re.compile(
        r"\b(cancell?ed|cancellation|dropped|drops? (me|you|by)|getting dropped|being dropped|"
        r"non.?renew(al|ed)?|laps(e|ed|es)|non.?standard)\b", re.I)),
    ("High-risk / hard-to-insure", re.compile(
        r"\b(high.?risk|hard to insure|knob and tube|aluminum wiring|dog bite|old roof|"
        r"denied|refused|can.?t get|houses? over 100|100\+? ?years|prior claims?)\b", re.I)),
    ("Fire / FAIR plan", re.compile(
        r"\b(fair act|fair plan|cal ?fair|california fair|ca fire|fire hazard|fire risk|"
        r"fire zone|hazard zone|zone zero|wild ?fire insurance|brush fire insurance|"
        r"fire insurance|fire coverage|fire damage insurance)\b", re.I)),
    ("Flood / wind / catastrophe", re.compile(
        r"\b(nfip|flood|windstorm|wind storm|wind and hail|hail insurance|hurricane insurance|"
        r"tornado insurance|wind insurance|earthquake|seismic|named storm|named peril|"
        r"hazard insurance|catastroph(e|ic)|sinkhole|landslide|mudslide|disaster insurance|"
        r"tsunami|twia)\b", re.I)),
    ("Does-carrier-have (answer)", re.compile(
        r"\bdoes\b.{0,40}\b(insurance|home|homeowners|house|insure|cover)\b", re.I)),
    ("VS / versus (informational)", re.compile(r"\b(vs\.?|versus)\b", re.I)),
]
STANDARD = "Standard (existing taxonomy)"


def classify(text):
    for label, rx in BUCKETS:
        if rx.search(text or ""):
            return label
    return STANDARD


def cols_of(conn, table):
    return [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]


def resolve_schema(conn):
    for t in ("search_query_performance", "keyword_instances", "search_queries"):
        if not conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone():
            sys.exit("ERROR: table '%s' not found in this DB." % t)
    sqc = cols_of(conn, "search_queries")
    txt = "normalized_text" if "normalized_text" in sqc else ("query_text" if "query_text" in sqc else None)
    if not txt:
        sys.exit("ERROR: no query-text column (normalized_text/query_text) in search_queries.")
    join = "query_id" if "query_id" in sqc else "id"     # how sqp.query_id maps to search_queries
    ia = "intent_action" if "intent_action" in sqc else None
    tagm = "tag_method" if "tag_method" in sqc else None
    return txt, join, ia, tagm


def build(db, start, end, out):
    conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)   # READ-ONLY
    txt, join, ia, tagm = resolve_schema(conn)
    print("schema: query-text=sq.%s  join=sq.%s=p.query_id  intent_action=%s  tag_method=%s"
          % (txt, join, ia or "(absent)", tagm or "(absent)"))

    sel_extra = []
    if ia:
        sel_extra.append("sq.%s AS intent_action" % ia)
    if tagm:
        sel_extra.append("sq.%s AS tag_method" % tagm)
    extra = (", " + ", ".join(sel_extra)) if sel_extra else ""
    grp_extra = (", " + ", ".join("sq.%s" % c for c in ([ia] if ia else []) + ([tagm] if tagm else []))) if sel_extra else ""

    sql = """
        SELECT sq.{txt} AS qtext{extra},
               SUM(p.impressions)        AS impr,
               SUM(p.clicks)             AS clicks,
               SUM(p.cost_micros)/1e6    AS cost,
               SUM(p.primary_conv_count) AS leads,
               SUM(p.primary_conv_value) AS revenue
        FROM search_query_performance p
        JOIN keyword_instances ki ON ki.id = p.instance_id
        JOIN search_queries    sq ON sq.{join} = p.query_id
        WHERE ki.account_id = 1
          AND p.device = 'ALL'
          AND p.report_date BETWEEN ? AND ?
        GROUP BY sq.{txt}{grp_extra}
    """.format(txt=txt, extra=extra, join=join, grp_extra=grp_extra)

    rows = conn.execute(sql, (start, end)).fetchall()
    conn.close()
    if not rows:
        sys.exit("No rows for account 1, device='ALL', %s..%s. Check the window / data coverage." % (start, end))

    has_ia = ia is not None
    has_tag = tagm is not None
    # column index map
    idx = {"qtext": 0}
    k = 1
    if has_ia:
        idx["intent_action"] = k; k += 1
    if has_tag:
        idx["tag_method"] = k; k += 1
    idx.update({"impr": k, "clicks": k + 1, "cost": k + 2, "leads": k + 3, "revenue": k + 4})

    agg = {}            # bucket -> metric sums
    ia_by_bucket = {}   # bucket -> {intent_action: leads}
    tagged_impr = 0
    total_impr = 0
    for r in rows:
        b = classify(r[idx["qtext"]])
        a = agg.setdefault(b, {"q": 0, "impr": 0, "clicks": 0, "cost": 0.0, "leads": 0, "revenue": 0.0})
        a["q"] += 1
        a["impr"] += r[idx["impr"]] or 0
        a["clicks"] += r[idx["clicks"]] or 0
        a["cost"] += r[idx["cost"]] or 0
        a["leads"] += r[idx["leads"]] or 0
        a["revenue"] += r[idx["revenue"]] or 0
        total_impr += r[idx["impr"]] or 0
        if has_tag and r[idx["tag_method"]] is not None:
            tagged_impr += r[idx["impr"]] or 0
        if has_ia:
            d = ia_by_bucket.setdefault(b, {})
            ia_val = r[idx["intent_action"]] or "(untagged)"
            d[ia_val] = d.get(ia_val, 0) + (r[idx["leads"]] or 0)

    write_outputs(out, agg, ia_by_bucket, start, end, total_impr, tagged_impr, has_tag, has_ia)
    return agg


def write_outputs(out, agg, ia_by_bucket, start, end, total_impr, tagged_impr, has_tag, has_ia):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    HEAD = PatternFill("solid", fgColor="1F3864")
    HILITE = PatternFill("solid", fgColor="E2EFDA")
    WHITE = Font(name="Arial", bold=True, color="FFFFFF", size=10)
    BOLD = Font(name="Arial", bold=True, size=10)
    REG = Font(name="Arial", size=10)
    NOTE = Font(name="Arial", size=9, italic=True, color="595959")
    TITLE = Font(name="Arial", bold=True, size=14, color="1F3864")
    THIN = Side(style="thin", color="BFBFBF")
    BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

    def hdr(ws, row, headers):
        for j, h in enumerate(headers, 1):
            c = ws.cell(row, j, h); c.fill = HEAD; c.font = WHITE
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True); c.border = BORDER

    order = [b for b, _ in BUCKETS] + [STANDARD]
    wb = Workbook(); ws = wb.active; ws.title = "Intent Buckets YTD"
    ws["A1"] = "USIA -- Real Home Search-Query Intent Cut (sqp)"; ws["A1"].font = TITLE
    ws.merge_cells("A1:J1")
    cov = (" Tag coverage: %.0f%% of impressions carry an 08-11 tag." % (100 * tagged_impr / total_impr)) if (has_tag and total_impr) else ""
    ws["A2"] = ("Realized served/converted queries on Google Home (account_id=1, device=ALL), %s..%s. "
                "Non-standard buckets via borrowed regex; first-match priority. RPL=revenue/leads. "
                "Realized demand where Home already bids -- not total addressable.%s" % (start, end, cov))
    ws["A2"].font = NOTE; ws.merge_cells("A2:J2")

    hdr(ws, 4, ["Intent Bucket", "Queries", "Impr", "Clicks", "Cost", "Leads", "Revenue",
                "RPL", "CvR %", "CPL"])
    r = 5
    tot = {"q": 0, "impr": 0, "clicks": 0, "cost": 0.0, "leads": 0, "revenue": 0.0}
    for b in order:
        a = agg.get(b)
        if not a:
            continue
        for kk in tot:
            tot[kk] += a[kk]
        rpl = a["revenue"] / a["leads"] if a["leads"] else 0
        cvr = 100 * a["leads"] / a["clicks"] if a["clicks"] else 0
        cpl = a["cost"] / a["leads"] if a["leads"] else 0
        nonstd = b != STANDARD
        ws.cell(r, 1, b).font = BOLD if nonstd else REG
        if nonstd:
            ws.cell(r, 1).fill = HILITE
        ws.cell(r, 2, a["q"]).font = REG
        ws.cell(r, 3, a["impr"]).font = REG; ws.cell(r, 3).number_format = "#,##0"
        ws.cell(r, 4, a["clicks"]).font = REG; ws.cell(r, 4).number_format = "#,##0"
        ws.cell(r, 5, round(a["cost"], 2)).font = REG; ws.cell(r, 5).number_format = "$#,##0"
        ws.cell(r, 6, a["leads"]).font = REG; ws.cell(r, 6).number_format = "#,##0"
        ws.cell(r, 7, round(a["revenue"], 2)).font = REG; ws.cell(r, 7).number_format = "$#,##0"
        ws.cell(r, 8, round(rpl, 2)).font = BOLD; ws.cell(r, 8).number_format = "$#,##0.00"
        ws.cell(r, 9, round(cvr, 2)).font = REG; ws.cell(r, 9).number_format = "0.0"
        ws.cell(r, 10, round(cpl, 2)).font = REG; ws.cell(r, 10).number_format = "$#,##0.00"
        r += 1
    # total row
    ws.cell(r, 1, "TOTAL").font = BOLD
    ws.cell(r, 2, tot["q"]).font = BOLD
    ws.cell(r, 3, tot["impr"]).font = BOLD; ws.cell(r, 3).number_format = "#,##0"
    ws.cell(r, 4, tot["clicks"]).font = BOLD; ws.cell(r, 4).number_format = "#,##0"
    ws.cell(r, 5, round(tot["cost"], 2)).font = BOLD; ws.cell(r, 5).number_format = "$#,##0"
    ws.cell(r, 6, tot["leads"]).font = BOLD; ws.cell(r, 6).number_format = "#,##0"
    ws.cell(r, 7, round(tot["revenue"], 2)).font = BOLD; ws.cell(r, 7).number_format = "$#,##0"
    ws.cell(r, 8, round(tot["revenue"] / tot["leads"], 2) if tot["leads"] else 0).font = BOLD
    ws.cell(r, 8).number_format = "$#,##0.00"
    for col, w in {"A": 32, "B": 9, "C": 11, "D": 9, "E": 11, "F": 9, "G": 12, "H": 10, "I": 8, "J": 10}.items():
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A5"

    # optional bucket x intent_action sheet (only if intent_action present)
    if has_ia and ia_by_bucket:
        ws2 = wb.create_sheet("Bucket x intent_action")
        ws2["A1"] = "Leads by bucket x intent_action"; ws2["A1"].font = TITLE
        ws2.merge_cells("A1:C1")
        hdr(ws2, 3, ["Intent Bucket", "intent_action", "Leads"])
        rr = 4
        for b in order:
            for iaval, leads in sorted(ia_by_bucket.get(b, {}).items(), key=lambda x: -x[1]):
                ws2.cell(rr, 1, b).font = REG
                ws2.cell(rr, 2, iaval).font = REG
                ws2.cell(rr, 3, leads).font = REG; ws2.cell(rr, 3).number_format = "#,##0"
                rr += 1
        for col, w in {"A": 32, "B": 22, "C": 10}.items():
            ws2.column_dimensions[col].width = w
        ws2.freeze_panes = "A4"

    wb.save(out)
    print("wrote %s" % out)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=r"C:\Projects\google-ads-ops\db\paid_search.db")
    p.add_argument("--start", default="2026-01-01")
    p.add_argument("--end", default="2026-06-07")
    p.add_argument("--out", default="usia_sqp_intent_ytd.xlsx")
    a = p.parse_args()
    agg = build(a.db, a.start, a.end, a.out)
    print("\nintent buckets (%s..%s), Home account, device=ALL:" % (a.start, a.end))
    order = [b for b, _ in BUCKETS] + [STANDARD]
    print("  %-34s %8s %7s %10s %8s" % ("bucket", "leads", "queries", "revenue", "RPL"))
    for b in order:
        x = agg.get(b)
        if not x:
            continue
        rpl = x["revenue"] / x["leads"] if x["leads"] else 0
        print("  %-34s %8s %7d %10s %8s"
              % (b, "{:,}".format(x["leads"]), x["q"], "${:,.0f}".format(x["revenue"]), "${:,.2f}".format(rpl)))


if __name__ == "__main__":
    main()
