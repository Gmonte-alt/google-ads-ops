#!/usr/bin/env python
"""
sitelink_direct_report.py -- ad_assets/analysis/ -- sitelink direct-contribution
workbook: one tab per window (Last 30/60/90 days, YTD), each with the table

  Sitelink | Link text | Asset Lvl. | Metric | <campaign cols...> | Total

  Impr                     = SUM(impressions), device='ALL', click_type='ALL'
                             (authoritative; presence-conditioned)
  Share of Campaign Impr % = Impr / campaign impressions (campaign table)
  ROAS                     = direct conv value / direct cost
  Value per click ($)      = direct conv value / direct clicks
  "direct" = click_type='SITELINKS' rows -- clicks ON the sitelink itself.
  Click types PARTITION, so direct clicks/value/cost are exclusive and
  additive: this is the one sitelink read that needs no co-serve caveat.

GRAIN LIMIT (by design, gate 2026-07-17): CUSTOMER-linked (account-level)
sitelinks have NO campaign split in asset_performance_daily -- their metrics
appear in the Total column only; campaign cells stay blank. CAMPAIGN-linked
sitelinks get full campaign columns. The UI can split shared assets by
campaign; the synced grain cannot. (Extension path: probe customer_asset for
campaign segmentation, then a gated sync amendment.)

Sitelink codes C1..Cn are assigned by YTD Impr rank (stable across tabs).
Every metric cell is a FORMULA over the RAW INPUTS block at the bottom of
each tab, so the workbook recalculates and documents its own sources.

  python sitelink_direct_report.py --db "C:\\Projects\\google-ads-ops\\db\\paid_search.db"
"""
import argparse
import datetime
import os
import re
import sqlite3
import sys

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

ACCOUNT_ID = 1
EXPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "exports")
LEVEL_LABEL = {"CUSTOMER": "Account", "CAMPAIGN": "Campaign", "AD_GROUP": "Ad group"}
METRICS = ["Impr", "Share of Campaign Impr %", "CTR %", "ROAS",
           "Conversion rate %", "Value per click ($)"]
FMT = {"Impr": "#,##0", "Share of Campaign Impr %": "0.0%", "CTR %": "0.00%",
       "ROAS": "0.00", "Conversion rate %": "0.0%", "Value per click ($)": "$0.00"}

CAMP_PERF_CANDIDATES = ["campaign_performance_daily", "campaign_performance",
                        "campaign_daily", "campaigns_performance_daily"]
CAMP_NAME_CANDIDATES = [("campaigns", "campaign_id", "campaign_name"),
                        ("campaigns", "campaign_id", "name"),
                        ("campaign_history", "campaign_id", "campaign_name")]


WORD_NUM = {"ONE": "1", "TWO": "2", "THREE": "3", "FOUR": "4", "FIVE": "5",
            "SIX": "6", "SEVEN": "7", "EIGHT": "8", "NINE": "9"}


def short_label(name):
    n = (name or "").upper()
    if "PMAX" in n:
        return "PMax"
    for token, lbl in (("CALIFORNIA", "CA"), ("TEXAS", "TX"), ("FLORIDA", "FL")):
        if token in n:
            return lbl
    m = re.search(r"TIER[\s_-]*(\d)", n)
    if m:
        return "Tier %s" % m.group(1)
    m = re.search(r"TIER[\s_-]*(ONE|TWO|THREE|FOUR|FIVE|SIX|SEVEN|EIGHT|NINE)", n)
    if m:
        return "Tier %s" % WORD_NUM[m.group(1)]
    return (name or "?")[:14]


def find_campaign_tables(conn, perf_override, names_override):
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    perf = None
    for t in ([perf_override] if perf_override else CAMP_PERF_CANDIDATES):
        if t in tables:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % t)}
            if "campaign_id" in cols and "impressions" in cols:
                datecol = next((c for c in ("report_date", "date", "segment_date")
                                if c in cols), None)
                if datecol:
                    perf = (t, datecol)
                    break
    if not perf:
        sys.exit("[ABORT] no campaign performance table found (tried %s). Pass "
                 "--campaign-perf-table." % (perf_override or CAMP_PERF_CANDIDATES))
    names = {}
    cand = ([tuple(names_override.split(":"))] if names_override else CAMP_NAME_CANDIDATES)
    for t, idc, namec in cand:
        if t in tables:
            try:
                q = "SELECT %s, %s FROM %s" % (idc, namec, t)
                try:
                    names = dict(conn.execute(q + " WHERE account_id=?", (ACCOUNT_ID,)))
                except sqlite3.Error:
                    names = dict(conn.execute(q))
                break
            except sqlite3.Error:
                continue
    print("[SOURCES] campaign perf: %s (date col %s) | names: %d campaign(s)"
          % (perf[0], perf[1], len(names)))
    return perf, {int(k): v for k, v in names.items()}


def window_bounds(tab, today):
    yday = today - datetime.timedelta(days=1)
    if tab == "YTD":
        return today.replace(month=1, day=1).isoformat(), yday.isoformat()
    days = int(tab.split()[1])
    return (today - datetime.timedelta(days=days)).isoformat(), yday.isoformat()


def has_table(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def direct_ct(conn, date_from, date_to):
    """Direct-click source: THIS_ASSET rows (true per-asset interaction, gate
    2026-09-03) when the window has them; SITELINKS fallback pre-backfill."""
    has = conn.execute(
        """SELECT 1 FROM asset_performance_daily WHERE account_id=?
           AND click_type='THIS_ASSET' AND report_date BETWEEN ? AND ? LIMIT 1""",
        (ACCOUNT_ID, date_from, date_to)).fetchone()
    return "THIS_ASSET" if has else "SITELINKS"


def pull(conn, perf, date_from, date_to):
    """Returns per-(asset, level, campaign-or-None) raw metrics + campaign impr.
    campaign=None rows are account-wide (link grain). When the campaign-split
    table exists, CUSTOMER-level assets also get per-campaign rows from it."""
    ct = direct_ct(conn, date_from, date_to)
    rows = conn.execute(
        f"""SELECT p.asset_id, p.association_level,
                  CASE WHEN p.association_level='CAMPAIGN' THEN p.association_id END,
                  SUM(CASE WHEN p.click_type='ALL' THEN p.impressions ELSE 0 END),
                  SUM(CASE WHEN p.click_type='{ct}' THEN p.clicks ELSE 0 END),
                  SUM(CASE WHEN p.click_type='{ct}' THEN p.conversions ELSE 0 END),
                  SUM(CASE WHEN p.click_type='{ct}' THEN p.conversions_value ELSE 0 END),
                  SUM(CASE WHEN p.click_type='{ct}' THEN p.cost_micros ELSE 0 END) / 1e6
           FROM asset_performance_daily p
           JOIN asset_sitelink_data s
             ON s.account_id = p.account_id AND s.asset_id = p.asset_id
           WHERE p.account_id=? AND p.device='ALL'
             AND p.report_date BETWEEN ? AND ?
           GROUP BY 1, 2, 3""",
        (ACCOUNT_ID, date_from, date_to)).fetchall()
    if has_table(conn, "asset_campaign_performance_daily"):
        rows = list(rows) + conn.execute(
            f"""SELECT p.asset_id, 'CUSTOMER', p.campaign_id,
                      SUM(CASE WHEN p.click_type='ALL' THEN p.impressions ELSE 0 END),
                      SUM(CASE WHEN p.click_type='{ct}' THEN p.clicks ELSE 0 END),
                      SUM(CASE WHEN p.click_type='{ct}' THEN p.conversions ELSE 0 END),
                      SUM(CASE WHEN p.click_type='{ct}' THEN p.conversions_value ELSE 0 END),
                      SUM(CASE WHEN p.click_type='{ct}' THEN p.cost_micros ELSE 0 END) / 1e6
               FROM asset_campaign_performance_daily p
               JOIN asset_sitelink_data s
                 ON s.account_id = p.account_id AND s.asset_id = p.asset_id
               WHERE p.account_id=? AND p.device='ALL'
                 AND p.report_date BETWEEN ? AND ?
               GROUP BY 1, 3""",
            (ACCOUNT_ID, date_from, date_to)).fetchall()
    perf_table, datecol = perf
    camp_impr = dict(conn.execute(
        f"""SELECT campaign_id, SUM(impressions) FROM {perf_table}
            WHERE account_id=? AND {datecol} BETWEEN ? AND ?
            GROUP BY campaign_id""", (ACCOUNT_ID, date_from, date_to)))
    return rows, {int(k): v or 0 for k, v in camp_impr.items()}


def main():
    ap = argparse.ArgumentParser(description="Sitelink direct-contribution workbook.")
    ap.add_argument("--db", required=True)
    ap.add_argument("--campaign-perf-table", default=None)
    ap.add_argument("--campaign-names-table", default=None,
                    help="Override as table:idcol:namecol")
    ap.add_argument("--campaigns", default=None,
                    help="Comma-separated campaign_ids to use as columns (recommended: "
                         "the core Home campaigns). Default: every campaign with YTD "
                         "impressions -- can be noisy in accounts with legacy campaigns.")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    if not os.path.isfile(args.db):
        sys.exit("[ABORT] database not found: %s" % args.db)
    os.makedirs(EXPORT_DIR, exist_ok=True)
    conn = sqlite3.connect("file:%s?mode=ro" % args.db.replace("\\", "/"), uri=True)
    perf_src, camp_names = find_campaign_tables(
        conn, args.campaign_perf_table, args.campaign_names_table)
    link_text = dict(conn.execute(
        "SELECT asset_id, link_text FROM asset_sitelink_data WHERE account_id=?",
        (ACCOUNT_ID,)))

    today = datetime.date.today()
    tabs = ["Last 30 days", "Last 60 days", "Last 90 days", "YTD"]
    data = {t: pull(conn, perf_src, *window_bounds(t, today)) for t in tabs}
    data_max = conn.execute(
        """SELECT MAX(report_date) FROM asset_performance_daily
           WHERE account_id=?""", (ACCOUNT_ID,)).fetchone()[0] or "none"
    conn.close()
    yday = (today - datetime.timedelta(days=1)).isoformat()
    if data_max < yday:
        print("[FRESHNESS] data ends %s but windows run through %s -- run the "
              "weekly syncs before sharing this workbook." % (data_max, yday))

    # C-codes: YTD Impr rank per (asset, level)
    ytd_tot = {}
    for a, lvl, camp, impr, clk, conv, val, cost in data["YTD"][0]:
        k = (a, lvl)
        ytd_tot[k] = ytd_tot.get(k, 0) + (impr or 0)
    order = sorted(ytd_tot, key=lambda k: -ytd_tot[k])
    code = {k: "S%d" % (i + 1) for i, k in enumerate(order)}

    # campaign columns: default = campaigns ACTIVE NOW (impressions in the
    # last-30-days window) so paused legacy campaigns can't become columns;
    # --campaigns overrides with an explicit id list (checked against YTD so
    # an explicitly requested campaign always gets its column).
    ytd_camps = data["YTD"][1]
    active_camps = data["Last 30 days"][1]
    allowed = ({int(x) for x in args.campaigns.split(",")} if args.campaigns else None)
    pool = ytd_camps if allowed is not None else active_camps
    camps = sorted((c for c in pool if pool.get(c, 0) > 0
                    and (allowed is None or c in allowed)),
                   key=lambda c: ({"CA": 0, "TX": 1, "FL": 2}.get(
                       short_label(camp_names.get(c, "")), 9),
                       short_label(camp_names.get(c, ""))))
    labels = []
    seen = {}
    for c in camps:
        lbl = short_label(camp_names.get(c, str(c)))
        if lbl in seen:
            lbl = "%s (%d)" % (lbl, c)
        seen[lbl] = c
        labels.append(lbl)

    wb = Workbook()
    wb.remove(wb.active)
    thin = Border(bottom=Side(style="thin", color="DDDDDD"))
    head_fill = PatternFill("solid", fgColor="1F4E79")
    DEFS = ("Impr = impressions where the sitelink was shown (click_type ALL)  |  "
            "Share of Campaign Impr % = Impr \u00f7 campaign impressions  |  "
            "CTR % = direct sitelink clicks \u00f7 Impr  |  "
            "ROAS = direct conversion value \u00f7 direct cost  |  "
            "Conversion rate % = direct conversions \u00f7 direct clicks  |  "
            "Value per click ($) = direct conversion value \u00f7 direct clicks  |  "
            "'Direct' = clicks ON THIS sitelink specifically (per-asset interaction "
            "segment = the UI's 'This extension'; reconciles with the UI to the "
            "click). Pre-backfill windows fall back to sitelink-type presence "
            "clicks (~1.5x)  |  "
            "Total = the sitelink's ACCOUNT-WIDE serving (all campaigns) | "
            "'All other active/paused' = campaigns not shown as columns, split by "
            "whether they served in the last 30 days; shown cells + residuals ~= Total "
            "(small gap = Google's attribution shading)")
    HEADER_ROW = 5
    for tab in tabs:
        rows, camp_impr = data[tab]
        d_from, d_to = window_bounds(tab, today)
        ws = wb.create_sheet(tab)
        # aggregate per key
        agg = {}   # (asset,lvl) -> {camp_or_None: [impr,clk,val,cost]}
        for a, lvl, camp, impr, clk, conv, val, cost in rows:
            d = agg.setdefault((a, lvl), {})
            cell = d.setdefault(camp, [0, 0, 0.0, 0.0, 0.0])
            cell[0] += impr or 0
            cell[1] += clk or 0
            cell[2] += conv or 0.0
            cell[3] += val or 0.0
            cell[4] += cost or 0.0

        # residual columns: activity split of NON-displayed campaigns; each
        # appears only when it has data in this tab's window
        displayed_ids = set(camps)
        active_ids = {c for c, v in data["Last 30 days"][1].items() if v > 0}
        resid = {"All other active": {}, "All other paused": {}}
        resid_members = {"All other active": set(), "All other paused": set()}
        for k2, cells2 in agg.items():
            for camp2, vals2 in cells2.items():
                if camp2 is None or camp2 in displayed_ids:
                    continue
                rl = "All other active" if camp2 in active_ids else "All other paused"
                acc = resid[rl].setdefault(k2, [0, 0, 0.0, 0.0, 0.0])
                for i2 in range(5):
                    acc[i2] += vals2[i2]
                resid_members[rl].add(camp2)
        resid_show = [rl for rl in ("All other active", "All other paused")
                      if any(v[0] > 0 or v[1] > 0 for v in resid[rl].values())]
        tab_labels = labels + resid_show
        headers = ["Sitelink", "Link text", "Asset Lvl.", "Metric"] + tab_labels + ["Total"]
        last_col = get_column_letter(len(headers))
        t = ws.cell(row=1, column=1,
                    value="Sitelink Direct Contribution Report \u2014 %s" % tab)
        t.font = Font(name="Arial", bold=True, size=14, color="1F4E79")
        ws.merge_cells("A1:%s1" % last_col)
        s = ws.cell(row=2, column=1,
                    value="Window: %s to %s  \u00b7  Data through: %s  \u00b7  "
                          "Generated %s  \u00b7  Google Home (account_id=1)"
                          % (d_from, d_to, data_max, today.isoformat()))
        s.font = Font(name="Arial", italic=True, size=10)
        ws.merge_cells("A2:%s2" % last_col)
        dfn = ws.cell(row=3, column=1, value=DEFS)
        dfn.font = Font(name="Arial", italic=True, size=8, color="666666")
        dfn.alignment = Alignment(wrap_text=True, vertical="top")
        ws.merge_cells("A3:%s3" % last_col)
        ws.row_dimensions[3].height = 30
        for j, h in enumerate(headers, 1):
            c = ws.cell(row=HEADER_ROW, column=j, value=h)
            c.font = Font(name="Arial", bold=True, color="FFFFFF")
            c.fill = head_fill
            c.alignment = Alignment(horizontal="center")
        ws.freeze_panes = "E%d" % (HEADER_ROW + 1)
        ws.column_dimensions["B"].width = 30
        for j in range(5, len(headers) + 1):
            ws.column_dimensions[get_column_letter(j)].width = 11

        present = [k for k in order if k in agg]
        # RAW block position: after display block + 3 spacer rows
        disp_rows = len(present) * len(METRICS)
        raw_start = HEADER_ROW + disp_rows + 4
        ws.cell(row=raw_start - 1, column=1, value=(
            "RAW INPUTS -- asset_performance_daily (device='ALL'; impressions from "
            "click_type='ALL' rows; direct clicks/value/cost from click_type='SITELINKS' "
            "rows); campaign impressions from %s. Account-level sitelinks carry no "
            "campaign split in this grain (gate 2026-07-17): campaign cells blank, "
            "Total only." % perf_src[0])).font = Font(name="Arial", italic=True, size=9)
        rh = ["code", "level", "campaign", "asset_impr", "direct_clicks",
              "direct_conversions", "direct_value", "direct_cost", "campaign_impr"]
        for j, h in enumerate(rh, 1):
            ws.cell(row=raw_start, column=j, value=h).font = Font(name="Arial", bold=True, size=9)

        raw_addr = {}
        r = raw_start + 1
        for k in present:
            a, lvl = k
            cells = agg[k]
            per_c = {}
            for ci, camp in enumerate(camps):
                if camp in cells:
                    per_c[camp] = cells[camp]
            if None in cells:   # account-wide link-grain row = authoritative Total
                tot = list(cells[None])
            else:
                tot = [0, 0, 0.0, 0.0, 0.0]
                for v in cells.values():
                    for i in range(5):
                        tot[i] += v[i]
            entries = [(short_label(camp_names.get(ck, str(ck))), vals,
                        camp_impr.get(ck, 0)) for ck, vals in per_c.items()]
            for rl in resid_show:
                if k in resid[rl] and (resid[rl][k][0] > 0 or resid[rl][k][1] > 0):
                    entries.append((rl, resid[rl][k],
                                    sum(camp_impr.get(c, 0) for c in resid_members[rl])))
            entries.append(("TOTAL", tot,
                            sum(camp_impr.values())))  # account-wide denominator,
                            # matching the Total numerator's account-wide scope
            for lbl, vals, ci in entries:
                ws.cell(row=r, column=1, value=code[k]).font = Font(name="Arial", size=9)
                ws.cell(row=r, column=2, value=LEVEL_LABEL.get(lvl, lvl)).font = Font(name="Arial", size=9)
                ws.cell(row=r, column=3, value=lbl).font = Font(name="Arial", size=9)
                for j, v in enumerate([vals[0], vals[1], round(vals[2], 2),
                                       round(vals[3], 2), round(vals[4], 2), ci], 4):
                    ws.cell(row=r, column=j, value=v).font = Font(name="Arial", size=9)
                raw_addr[(k, lbl)] = r
                r += 1

        # display block
        r = HEADER_ROW + 1
        for k in present:
            a, lvl = k
            for mi, metric in enumerate(METRICS):
                if mi == 0:
                    ws.cell(row=r, column=1, value=code[k])
                    ws.cell(row=r, column=2, value=link_text.get(a, "(asset %d)" % a))
                    ws.cell(row=r, column=3, value=LEVEL_LABEL.get(lvl, lvl))
                ws.cell(row=r, column=4, value=metric)
                for j, lbl in enumerate(tab_labels + ["Total"], 5):
                    rr = raw_addr.get((k, lbl if lbl != "Total" else "TOTAL"))
                    if rr is None:
                        continue
                    col = ws.cell(row=r, column=j)
                    if metric == "Impr":
                        col.value = "=D%d" % rr
                    elif metric == "Share of Campaign Impr %":
                        col.value = "=IFERROR(D%d/I%d,\"\")" % (rr, rr)
                    elif metric == "CTR %":
                        col.value = "=IF(D%d=0,\"\",E%d/D%d)" % (rr, rr, rr)
                    elif metric == "ROAS":
                        col.value = "=IF(H%d=0,\"\",G%d/H%d)" % (rr, rr, rr)
                    elif metric == "Conversion rate %":
                        col.value = "=IF(E%d=0,\"\",F%d/E%d)" % (rr, rr, rr)
                    else:
                        col.value = "=IF(E%d=0,\"\",G%d/E%d)" % (rr, rr, rr)
                    col.number_format = FMT[metric]
                for j in range(1, len(headers) + 1):
                    c = ws.cell(row=r, column=j)
                    c.font = Font(name="Arial")
                    if mi == len(METRICS) - 1:
                        c.border = thin
                r += 1

    out = args.out or os.path.join(
        EXPORT_DIR, "sitelink_direct_report_%s.xlsx" % today.isoformat())
    wb.save(out)
    print("[DONE] %s  (%d sitelink/level rows; campaign columns: %s)"
          % (out, len(order), ", ".join(labels)))
    print("[NOTE] run this on Windows; recalculate opens automatically in Excel.")


if __name__ == "__main__":
    main()
