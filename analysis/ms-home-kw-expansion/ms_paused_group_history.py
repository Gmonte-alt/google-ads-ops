"""
ms_paused_group_history.py -- READ-ONLY history readout for paused/dormant acct-2
ad groups (default: the Home-Generic-Quote groups the expansion imports into).

Answers, per matching group:
  (1) WHEN did it run and when did it stop -- kpd date coverage per group
  (2) HOW did it perform -- monthly impressions/clicks/cost/conversions with
      CPC / CvR / CPL (NO revenue: MS revenue data does not exist pre-2026, and
      pre-2026 conversion semantics are platform-era -- compare within period only)
  (3) vs ITS ERA -- the same monthly metrics for the whole acct-2 GENERIC book,
      so 'paused for cause' vs 'died in the restructure' is judged relative to
      contemporaries, not today's account
  (4) WHAT'S INSIDE -- the group's keywords (text/match/status) for the legacy-
      contents review before re-enabling

Defensive: kpd/ki columns resolved by PRAGMA; device handling auto-detected
(uses device='ALL' rollup if present, else sums across devices), reported in the
output so the basis is explicit.

    python ms_paused_group_history.py
    python ms_paused_group_history.py --group-like "Home-Generic-Quote" --group-like "Home-Generic-Compare"
"""
import argparse
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ACCT = 2


def cols_of(conn, table):
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]
    except sqlite3.OperationalError:
        return []


def pick(cands, present, what):
    c = next((x for x in cands if x in present), None)
    if c is None:
        sys.exit("SCHEMA ERROR: none of %s found for %s. Present: %s" % (cands, what, present))
    return c


def device_clause(conn, dev_col):
    devs = {(r[0] or "").upper() for r in conn.execute(
        "SELECT DISTINCT %s FROM keyword_performance_daily LIMIT 200" % dev_col)}
    if "ALL" in devs:
        return " AND kpd.%s='ALL'" % dev_col, "device='ALL' rollup"
    return "", "summed across devices (no ALL rollup: %s)" % sorted(devs)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=r"C:\Projects\google-ads-ops\db\paid_search.db")
    p.add_argument("--group-like", action="append", default=None,
                   help="ad-group name substring(s); repeatable. Default: Home-Generic-Quote")
    p.add_argument("--out", default=r"C:\Projects\google-ads-ops\analysis\ms-home-kw-expansion"
                                    r"\output\ms_paused_group_history_%s.xlsx" % date.today().isoformat())
    a = p.parse_args()
    likes = a.group_like or ["Home-Generic-Quote"]

    conn = sqlite3.connect("file:%s?mode=ro" % a.db, uri=True)
    conn.row_factory = sqlite3.Row
    kpd_c = cols_of(conn, "keyword_performance_daily")
    ki_c = cols_of(conn, "keyword_instances")
    impr = pick(["impressions", "impr"], kpd_c, "impressions")
    clicks = pick(["clicks"], kpd_c, "clicks")
    cost = pick(["cost_micros", "cost"], kpd_c, "cost")
    conv = pick(["primary_conv_count", "conversions", "conv_count"], kpd_c, "conversions")
    dcol = pick(["device"], kpd_c, "device")
    dt = pick(["report_date", "date"], kpd_c, "date")
    cost_expr = "SUM(kpd.%s)/1e6" % cost if cost == "cost_micros" else "SUM(kpd.%s)" % cost
    dev_sql, dev_note = device_clause(conn, dcol)
    print("device handling: %s | conv column: %s (pre-2026 semantics are platform-era; "
          "no revenue exists pre-2026)" % (dev_note, conv))

    # target groups (any status) with parent status
    like_sql = " OR ".join("ag.ad_group_name LIKE ?" for _ in likes)
    groups = conn.execute(
        "SELECT ag.ad_group_id, ag.ad_group_name, ag.status, c.campaign_name, c.status cstat "
        "FROM ad_groups ag JOIN campaigns c ON c.campaign_id=ag.campaign_id "
        "AND c.account_id=ag.account_id WHERE ag.account_id=? AND (%s) "
        "ORDER BY c.campaign_name, ag.ad_group_name" % like_sql,
        [ACCT] + ["%%%s%%" % l for l in likes]).fetchall()
    print("matched %d ad groups for %s" % (len(groups), likes))
    if not groups:
        sys.exit("no matching ad groups on acct %d" % ACCT)
    gids = [g["ad_group_id"] for g in groups]
    ph = ",".join("?" * len(gids))

    # monthly perf per group (keyword grain rolled up via ki.ad_group_id)
    q = ("SELECT ki.ad_group_id gid, substr(kpd.%s,1,7) ym, SUM(kpd.%s) impr, "
         "SUM(kpd.%s) clicks, %s cost, SUM(kpd.%s) conv "
         "FROM keyword_performance_daily kpd JOIN keyword_instances ki ON ki.id=kpd.instance_id "
         "WHERE ki.account_id=? AND ki.ad_group_id IN (%s)%s "
         "GROUP BY 1,2 ORDER BY 1,2" % (dt, impr, clicks, cost_expr, conv, ph, dev_sql))
    perf = conn.execute(q, [ACCT] + gids).fetchall()

    # era baseline: whole acct-2 GENERIC book, same monthly grain
    bq = ("SELECT substr(kpd.%s,1,7) ym, SUM(kpd.%s) impr, SUM(kpd.%s) clicks, %s cost, "
          "SUM(kpd.%s) conv FROM keyword_performance_daily kpd "
          "JOIN keyword_instances ki ON ki.id=kpd.instance_id "
          "WHERE ki.account_id=? AND lower(ki.ad_group_name) NOT LIKE '%%non-generic%%' "
          "AND lower(ki.ad_group_name) NOT LIKE '%%nongeneric%%' "
          "AND lower(ki.ad_group_name) NOT LIKE '%%non generic%%'%s "
          "GROUP BY 1 ORDER BY 1" % (dt, impr, clicks, cost_expr, conv, dev_sql))
    base = {r["ym"]: r for r in conn.execute(bq, [ACCT])}

    def rates(im, cl, co, cv):
        return (co / cl if cl else 0, 100.0 * cv / cl if cl else 0, co / cv if cv else 0)

    gmeta = {g["ad_group_id"]: g for g in groups}
    monthly_rows, tot = [], defaultdict(lambda: [0, 0, 0.0, 0.0])
    span = {}
    for r in perf:
        g = gmeta[r["gid"]]
        cpc, cvr, cpl = rates(r["impr"], r["clicks"], r["cost"], r["conv"])
        b = base.get(r["ym"])
        bcpl = (b["cost"] / b["conv"]) if b and b["conv"] else 0
        bcvr = (100.0 * b["conv"] / b["clicks"]) if b and b["clicks"] else 0
        monthly_rows.append({"campaign": g["campaign_name"], "ad_group": g["ad_group_name"],
                             "month": r["ym"], "impr": r["impr"], "clicks": r["clicks"],
                             "cost": round(r["cost"], 2), "conv": round(r["conv"], 1),
                             "CPC": round(cpc, 2), "CvR%": round(cvr, 2), "CPL": round(cpl, 2),
                             "era_generic_CPL": round(bcpl, 2), "era_generic_CvR%": round(bcvr, 2),
                             "CPL_vs_era": round(cpl / bcpl, 2) if bcpl and cpl else ""})
        t = tot[r["gid"]]
        t[0] += r["impr"]; t[1] += r["clicks"]; t[2] += r["cost"]; t[3] += r["conv"]
        s = span.setdefault(r["gid"], [r["ym"], r["ym"]])
        s[0] = min(s[0], r["ym"]); s[1] = max(s[1], r["ym"])

    print("\n%-46s %-28s %-8s %9s %8s %10s %7s %7s %8s  %s" %
          ("ad group", "campaign", "status", "impr", "clicks", "cost", "conv", "CvR%", "CPL", "ran"))
    total_rows = []
    for g in groups:
        t = tot.get(g["ad_group_id"])
        if not t:
            print("%-46s %-28s %-8s %s" % (g["ad_group_name"][:46], g["campaign_name"][:28],
                                           g["status"], "NO kpd HISTORY (never ran or not backfilled)"))
            total_rows.append({"campaign": g["campaign_name"], "ad_group": g["ad_group_name"],
                               "group_status": g["status"], "campaign_status": g["cstat"],
                               "ran": "no history"})
            continue
        cpc, cvr, cpl = rates(*t)
        s = span[g["ad_group_id"]]
        print("%-46s %-28s %-8s %9s %8s %10s %7s %7.2f %8.2f  %s..%s"
              % (g["ad_group_name"][:46], g["campaign_name"][:28], g["status"],
                 f"{t[0]:,}", f"{t[1]:,}", f"{t[2]:,.0f}", f"{t[3]:,.0f}", cvr, cpl, s[0], s[1]))
        total_rows.append({"campaign": g["campaign_name"], "ad_group": g["ad_group_name"],
                           "group_status": g["status"], "campaign_status": g["cstat"],
                           "impr": t[0], "clicks": t[1], "cost": round(t[2], 2),
                           "conv": round(t[3], 1), "CPC": round(cpc, 2),
                           "CvR%": round(cvr, 2), "CPL": round(cpl, 2),
                           "ran": "%s..%s" % (s[0], s[1])})

    # legacy contents
    kws = conn.execute(
        "SELECT ki.ad_group_id gid, ki.keyword_text, ki.match_type, ki.status, "
        "ki.first_seen_date, ki.last_seen_date "
        "FROM keyword_instances ki WHERE ki.account_id=? AND ki.ad_group_id IN (%s) "
        "ORDER BY ki.ad_group_id, ki.keyword_text" % ph, [ACCT] + gids).fetchall()
    kw_rows = [{"campaign": gmeta[k["gid"]]["campaign_name"],
                "ad_group": gmeta[k["gid"]]["ad_group_name"], "keyword": k["keyword_text"],
                "match_type": k["match_type"], "status": k["status"],
                "first_seen": k["first_seen_date"], "last_seen": k["last_seen_date"]}
               for k in kws]
    print("\nlegacy keywords inside matched groups: %d (see workbook Keywords Inside sheet)"
          % len(kw_rows))
    conn.close()

    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    HEADF = PatternFill("solid", fgColor="1F3864")
    WHITE = Font(name="Arial", bold=True, color="FFFFFF", size=10)
    BOLD = Font(name="Arial", bold=True, size=10)
    REG = Font(name="Arial", size=10)
    NOTE = Font(name="Arial", size=9, italic=True, color="595959")
    TITLE = Font(name="Arial", bold=True, size=13, color="1F3864")

    def sheet_from(wb, title, heading, cols, dicts, widths, note=None):
        ws = wb.create_sheet(title)
        ws["A1"] = heading; ws["A1"].font = TITLE
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(cols))
        r0 = 3
        if note:
            ws["A2"] = note; ws["A2"].font = NOTE
            ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=len(cols))
            r0 = 4
        for j, h in enumerate(cols, 1):
            c = ws.cell(r0, j, h); c.fill = HEADF; c.font = WHITE
            c.alignment = Alignment(horizontal="center", wrap_text=True)
        r = r0 + 1
        for d in dicts:
            for j, k in enumerate(cols, 1):
                ws.cell(r, j, d.get(k)).font = BOLD if j <= 2 else REG
            r += 1
        for j, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(j)].width = w
        ws.freeze_panes = ws.cell(r0 + 1, 1).coordinate
        ws.auto_filter.ref = "A%d:%s%d" % (r0, get_column_letter(len(cols)), max(r - 1, r0))

    wb = Workbook(); wb.remove(wb.active)
    sheet_from(wb, "Group Totals", "Paused-group lifetime performance (%s)" % dev_note,
               ["campaign", "ad_group", "group_status", "campaign_status", "impr", "clicks",
                "cost", "conv", "CPC", "CvR%", "CPL", "ran"], total_rows,
               [30, 42, 10, 10, 10, 9, 11, 8, 8, 8, 9, 18],
               note="No revenue pre-2026; conv column is platform-era. Judge vs the era baseline "
                    "on the Monthly sheet, not vs today's account.")
    sheet_from(wb, "Monthly vs Era", "Monthly performance with same-month acct-2 GENERIC baseline",
               ["campaign", "ad_group", "month", "impr", "clicks", "cost", "conv", "CPC",
                "CvR%", "CPL", "era_generic_CPL", "era_generic_CvR%", "CPL_vs_era"],
               monthly_rows, [30, 42, 9, 9, 8, 10, 7, 7, 7, 8, 12, 12, 10],
               note="CPL_vs_era < 1.0 = cheaper than the generic book that month (argues 'died in "
                    "restructure'); >> 1.0 = paused for cause.")
    sheet_from(wb, "Keywords Inside", "Legacy contents of the matched groups (pre-enable review)",
               ["campaign", "ad_group", "keyword", "match_type", "status", "first_seen", "last_seen"],
               kw_rows, [30, 42, 36, 10, 10, 12, 12])
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    wb.save(a.out)
    print("wrote %s" % a.out)


if __name__ == "__main__":
    main()
