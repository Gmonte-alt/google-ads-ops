#!/usr/bin/env python
"""
claims_audit_google.py -- ad_assets/analysis/ -- v2: claims audit WORKBOOK
over live Google Home copy, windowed (default: last 30 days) with
performance context per claim line: impressions, clicks, cost, revenue,
ROAS. For the manager's claims doc.

Scope = current compliance exposure: texts on ENABLED RSAs (headlines +
descriptions) and all synced sitelinks (link text + descriptions).

METRIC SEMANTICS (stated in the sheet header): line metrics are
PRESENCE-CONDITIONED -- totals for ad impressions where the line was shown
(headlines co-serve ~2.78x), so they size a claim's exposure and the money
around it; they do NOT attribute causation to the line. ROAS = revenue /
cost on those same presence rows, computed as an in-sheet formula.

Output: ad_assets\\exports\\claims_audit_google_<date>.xlsx
  "Claims" tab: one row per claim-bearing line, impression-sorted
  "Category Summary" tab: SUMIF-driven rollup per claim category
READ-ONLY on the DB.

  python claims_audit_google.py --db "...\\paid_search.db"
  python claims_audit_google.py --db "...\\paid_search.db" --from 2026-07-01 --to 2026-07-31
"""
import argparse
import datetime
import os
import re
import sqlite3
import sys
from collections import defaultdict

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

ACCOUNT_ID = 1
EXPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "exports")

CATEGORIES = [
    ("price_point", re.compile(r"\$\s?\d[\d,]*")),
    ("savings", re.compile(r"\bsav(?:e|ings?)\b", re.I)),
    ("percentage", re.compile(r"\b\d{1,3}\s?%")),
    ("free", re.compile(r"\bfree\b", re.I)),
    ("no_x", re.compile(r"\bno (?:cost|obligation|fees?|down payment|hidden \w+|deposit)\b", re.I)),
    ("speed", re.compile(r"\b(?:instant(?:ly)?|in minutes|under \d+|less than \d+|fast|in \d+ min)\b", re.I)),
    ("superlative", re.compile(r"\b(?:best|cheapest|lowest|top[- ]rated|#\s?1|guaranteed|trusted)\b", re.I)),
    ("dated", re.compile(r"\b20\d{2}\b")),
]
CAT_NAMES = [c for c, _ in CATEGORIES]


def classify(text):
    hits = {}
    for cat, rx in CATEGORIES:
        m = [mt.group(0) for mt in rx.finditer(text)]
        if m:
            hits[cat] = ", ".join(sorted(set(m))[:4])
    return hits


def main():
    p = argparse.ArgumentParser(description="Claims audit workbook (dry facts, read-only).")
    p.add_argument("--db", required=True)
    p.add_argument("--from", dest="date_from", default=None, metavar="YYYY-MM-DD",
                   help="Window start (default: 30 days before yesterday... i.e. last 30 days)")
    p.add_argument("--to", dest="date_to", default=None, metavar="YYYY-MM-DD",
                   help="Window end (default: yesterday)")
    p.add_argument("--out", default=None)
    args = p.parse_args()
    if not os.path.isfile(args.db):
        sys.exit("[ABORT] database not found: %s" % args.db)
    os.makedirs(EXPORT_DIR, exist_ok=True)

    today = datetime.date.today()
    date_to = args.date_to or (today - datetime.timedelta(days=1)).isoformat()
    date_from = args.date_from or (today - datetime.timedelta(days=30)).isoformat()
    datetime.date.fromisoformat(date_from), datetime.date.fromisoformat(date_to)

    conn = sqlite3.connect("file:%s?mode=ro" % args.db.replace("\\", "/"), uri=True)
    lines = defaultdict(lambda: {"sources": set(), "ads": set(), "camps": set(),
                                 "impr": 0, "clicks": 0, "cost": 0.0, "rev": 0.0})

    # RSA lines on ENABLED ads, presence metrics over the window
    for text, ft, ad_id, camp, impr, clk, cost, rev in conn.execute(
            """SELECT t.text, c.field_type, r.ad_id, r.campaign_id,
                      COALESCE(SUM(ap.impressions),0), COALESCE(SUM(ap.clicks),0),
                      COALESCE(SUM(ap.cost_micros),0)/1e6,
                      COALESCE(SUM(ap.conversions_value),0)
               FROM rsa_ads r
               JOIN rsa_ad_assets c ON c.account_id=r.account_id AND c.ad_id=r.ad_id
                AND c.removed_date IS NULL
               JOIN asset_text_data t ON t.account_id=c.account_id AND t.asset_id=c.asset_id
               LEFT JOIN rsa_asset_performance_daily ap
                 ON ap.account_id=c.account_id AND ap.ad_id=c.ad_id
                AND ap.asset_id=c.asset_id AND ap.field_type=c.field_type
                AND ap.device='ALL' AND ap.report_date BETWEEN ? AND ?
               WHERE r.account_id=? AND r.ad_status='ENABLED'
               GROUP BY t.text, c.field_type, r.ad_id, r.campaign_id""",
            (date_from, date_to, ACCOUNT_ID)):
        d = lines[text]
        d["sources"].add("RSA headline" if ft == "HEADLINE" else "RSA description")
        d["ads"].add(ad_id)
        d["camps"].add(camp)
        d["impr"] += impr; d["clicks"] += clk; d["cost"] += cost; d["rev"] += rev

    # Sitelink lines, presence metrics (ALL rows) over the window
    for aid, lt, d1, d2 in conn.execute(
            "SELECT asset_id, link_text, description1, description2 "
            "FROM asset_sitelink_data WHERE account_id=?", (ACCOUNT_ID,)):
        impr, clk, cost, rev = conn.execute(
            """SELECT COALESCE(SUM(impressions),0), COALESCE(SUM(clicks),0),
                      COALESCE(SUM(cost_micros),0)/1e6, COALESCE(SUM(conversions_value),0)
               FROM asset_performance_daily
               WHERE account_id=? AND asset_id=? AND device='ALL' AND click_type='ALL'
                 AND report_date BETWEEN ? AND ?""",
            (ACCOUNT_ID, aid, date_from, date_to)).fetchone()
        for txt, src in ((lt, "Sitelink text"), (d1, "Sitelink description"),
                         (d2, "Sitelink description")):
            if txt and txt.strip():
                d = lines[txt.strip()]
                d["sources"].add(src)
                d["ads"].add("SL%d" % aid)
                d["impr"] += impr; d["clicks"] += clk; d["cost"] += cost; d["rev"] += rev

    data_max = conn.execute(
        """SELECT MAX(d) FROM (
             SELECT MAX(report_date) d FROM rsa_asset_performance_daily WHERE account_id=?
             UNION ALL
             SELECT MAX(report_date) FROM asset_performance_daily WHERE account_id=?)""",
        (ACCOUNT_ID, ACCOUNT_ID)).fetchone()[0] or "none"
    conn.close()

    rows = []
    cat_seen = defaultdict(int)
    for text, d in lines.items():
        hits = classify(text)
        if not hits:
            continue
        for c in hits:
            cat_seen[c] += 1
        rows.append((text, d, hits))
    rows.sort(key=lambda r: -r[1]["impr"])

    wb = Workbook()
    ws = wb.active
    ws.title = "Claims"
    headers = ["Line text", "Sources", "Categories", "Matched claims", "Ads/Links",
               "Campaigns", "Impressions", "Clicks", "Cost", "Revenue", "ROAS"]
    HEADER_ROW = 5
    last_col = get_column_letter(len(headers))
    t = ws.cell(row=1, column=1, value="Ad Copy Claims Audit \u2014 Google Home")
    t.font = Font(name="Arial", bold=True, size=14, color="1F4E79")
    ws.merge_cells("A1:%s1" % last_col)
    s = ws.cell(row=2, column=1, value="Window: %s to %s  \u00b7  Data through: %s  \u00b7  "
                "Generated %s  \u00b7  Scope: ENABLED RSAs + sitelinks (account_id=1; "
                "Microsoft is a separate pull)" % (date_from, date_to, data_max, today.isoformat()))
    s.font = Font(name="Arial", italic=True, size=10)
    ws.merge_cells("A2:%s2" % last_col)
    dfn = ws.cell(row=3, column=1, value=(
        "Metrics are PRESENCE-CONDITIONED: totals for ad impressions where the line was "
        "shown (headlines co-serve ~2.78x) \u2014 they size a claim's exposure and the money "
        "around it, not the line's causal contribution. ROAS = Revenue \u00f7 Cost on those "
        "rows. Source: rsa_asset_performance_daily + asset_performance_daily (device=ALL)."))
    dfn.font = Font(name="Arial", italic=True, size=8, color="666666")
    dfn.alignment = Alignment(wrap_text=True, vertical="top")
    ws.merge_cells("A3:%s3" % last_col)
    ws.row_dimensions[3].height = 34

    head_fill = PatternFill("solid", fgColor="1F4E79")
    for j, h in enumerate(headers, 1):
        c = ws.cell(row=HEADER_ROW, column=j, value=h)
        c.font = Font(name="Arial", bold=True, color="FFFFFF")
        c.fill = head_fill
        c.alignment = Alignment(horizontal="center")
    ws.freeze_panes = "B%d" % (HEADER_ROW + 1)
    ws.column_dimensions["A"].width = 52
    ws.column_dimensions["B"].width = 22
    ws.column_dimensions["C"].width = 26
    ws.column_dimensions["D"].width = 34
    for j in range(5, len(headers) + 1):
        ws.column_dimensions[get_column_letter(j)].width = 12

    r = HEADER_ROW + 1
    for text, d, hits in rows:
        ws.cell(row=r, column=1, value=text)
        ws.cell(row=r, column=2, value="; ".join(sorted(d["sources"])))
        ws.cell(row=r, column=3, value="; ".join(sorted(hits)))
        ws.cell(row=r, column=4, value=" | ".join("%s: %s" % (c, s) for c, s in sorted(hits.items())))
        ws.cell(row=r, column=5, value=len(d["ads"]))
        ws.cell(row=r, column=6, value=len(d["camps"]))
        ws.cell(row=r, column=7, value=d["impr"]).number_format = "#,##0"
        ws.cell(row=r, column=8, value=d["clicks"]).number_format = "#,##0"
        ws.cell(row=r, column=9, value=round(d["cost"], 2)).number_format = "$#,##0.00"
        ws.cell(row=r, column=10, value=round(d["rev"], 2)).number_format = "$#,##0.00"
        k = ws.cell(row=r, column=11, value='=IF(I%d=0,"",J%d/I%d)' % (r, r, r))
        k.number_format = "0.00"
        for j in range(1, 12):
            ws.cell(row=r, column=j).font = Font(name="Arial")
        r += 1
    first_data, last_data = HEADER_ROW + 1, r - 1

    ws2 = wb.create_sheet("Category Summary")
    t2 = ws2.cell(row=1, column=1, value="Claims by category \u2014 %s to %s" % (date_from, date_to))
    t2.font = Font(name="Arial", bold=True, size=12, color="1F4E79")
    ws2.merge_cells("A1:G1")
    sum_headers = ["Category", "Lines", "Impressions", "Clicks", "Cost", "Revenue", "ROAS"]
    for j, h in enumerate(sum_headers, 1):
        c = ws2.cell(row=3, column=j, value=h)
        c.font = Font(name="Arial", bold=True, color="FFFFFF")
        c.fill = head_fill
    ws2.column_dimensions["A"].width = 16
    for j in range(2, 8):
        ws2.column_dimensions[get_column_letter(j)].width = 13
    rr = 4
    for cat in CAT_NAMES:
        if cat_seen.get(cat, 0) == 0:
            continue
        crit = '"*%s*"' % cat
        rng = "Claims!$C$%d:$C$%d" % (first_data, last_data)
        ws2.cell(row=rr, column=1, value=cat)
        ws2.cell(row=rr, column=2, value="=COUNTIF(%s,%s)" % (rng, crit))
        for j, col in ((3, "G"), (4, "H"), (5, "I"), (6, "J")):
            ws2.cell(row=rr, column=j,
                     value="=SUMIF(%s,%s,Claims!%s$%d:%s$%d)"
                           % (rng, crit, col, first_data, col, last_data))
        ws2.cell(row=rr, column=3).number_format = "#,##0"
        ws2.cell(row=rr, column=4).number_format = "#,##0"
        ws2.cell(row=rr, column=5).number_format = "$#,##0.00"
        ws2.cell(row=rr, column=6).number_format = "$#,##0.00"
        ws2.cell(row=rr, column=7, value='=IF(E%d=0,"",F%d/E%d)' % (rr, rr, rr)).number_format = "0.00"
        for j in range(1, 8):
            ws2.cell(row=rr, column=j).font = Font(name="Arial")
        rr += 1

    out = args.out or os.path.join(EXPORT_DIR, "claims_audit_google_%s.xlsx" % today.isoformat())
    wb.save(out)
    print("[DONE] %d claim-bearing line(s) -> %s" % (len(rows), out))
    print("[WINDOW] %s to %s | data through %s" % (date_from, date_to, data_max))
    if data_max < date_to:
        print("[FRESHNESS] data ends %s but the window runs to %s -- run the weekly "
              "syncs before sharing." % (data_max, date_to))
    print("[CATEGORIES] " + ", ".join("%s: %d" % (c, cat_seen[c])
                                      for c in CAT_NAMES if cat_seen.get(c)))


if __name__ == "__main__":
    main()
