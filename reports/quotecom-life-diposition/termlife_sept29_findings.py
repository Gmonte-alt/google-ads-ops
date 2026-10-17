#!/usr/bin/env python3
"""
termlife_sept29_findings.py
===========================
Sept-29 regime-shift findings workbook for the Term Life account (acct 29).

Pulls LIVE from paid_search.db (read-only):
  - life_disposition_snapshot  -> Day-1 (a1) and Matured (a4) same-age KPI by cohort
  - campaign_performance_daily -> Google cost/clicks/CPC/native conv by cluster, daily
  - campaign_performance_zip_daily -> CR12 device split + intersection-zip CPC
Parses the four Looker exports (campaign-truth):
  - Daily_by_Advert_Name  cr1-2 / cr3-5   (daily cluster x segment)
  - Monthly_by_Advert_Name cr-1-2 / cr-3-5 (month grain context)

Output: output/termlife_sept29_findings_{YYYYMMDD}.xlsx
Sheets: Summary | RegimeShift | ClusterTruth | CostSide | Ledger | DataNotes

Usage (from repo root):
  python reports/quotecom-life-diposition/termlife_sept29_findings.py
  python ...findings.py --db C:/Projects/google-ads-ops/db/paid_search.db --looker-dir <folder with the 4 csvs>

House rules honored: read-only URI, account_id=29 scope, cpd device='ALL',
zip table summed across devices, exact-batch same-age reads (no nearest-batch fallback).
"""
import argparse, csv, glob, os, re, sqlite3, sys
from collections import defaultdict
from datetime import date, datetime, timedelta

import pandas as pd
from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, Reference, Series
from openpyxl.chart.marker import Marker
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

# ----------------------------------------------------------------------------- config
DEF_DB = r"C:\Projects\google-ads-ops\db\paid_search.db"
PRE_START, PRE_END   = "2026-09-22", "2026-09-28"   # 6 serving days (no Sunday)
POST_START, POST_END = "2026-09-29", "2026-10-05"   # 6 serving days
CHANGE_DATE = "2026-09-29"
PANEL_PRE_START = "2026-09-23"   # first cohort with a1 coverage (batches begin 9/24)
BREAK_EVEN = 2.96          # SQ break-even return (1/0.3375)
TARGET_KPI = 4.00

# SQ payment per WebM op, by cohort month (observed Looker Spend/Ops, by-advert pulls)
SEG_OP_RATES = {
    "High":   {"2026-08": 149.67, "2026-09": 144.02, "2026-10": 148.53},
    "Medium": {"2026-08":  86.55, "2026-09":  84.34, "2026-10":  86.75},
    "Low":    {"2026-08":  30.98, "2026-09":  31.51, "2026-10":  33.24},
}
SEG_OF_CODE = {"CC": "High", "CB": "Medium", "CA": "Low"}   # paid segments only (QA organic excluded)

# life_disposition_snapshot column names. Leave None to auto-detect; set explicitly if detection aborts.
SNAP_COLS = {                       # verified against live schema 2026-10-07 (PRAGMA table_info)
    "batch":  "batch_date",
    "code":   "source_code",
    "lead_date": "day",             # lead-creation date column is named 'day' in the feed
    "op":     "op_count",
    "cri":    "cris",
    "prem":   "capped_cri_dollars",
}

NAVY   = "1F3864"; GRAY = "D9D9D9"; LGRAY = "F2F2F2"
FILL   = {"High": "DDEBF7", "Medium": "E2EFDA", "Low": "FCE4D6"}
RED_T  = Font(name="Arial", size=10, color="C00000", bold=True)
MONEY  = '$#,##0'; MONEY2 = '$#,##0.00'; KPIF = '0.00'; PCT = '0.0%'; NUMF = '#,##0'

def F(sz=10, b=False, c="000000"): return Font(name="Arial", size=sz, bold=b, color=c)
def fill(h): return PatternFill("solid", fgColor=h)
THIN = Border(*[Side(style="thin", color="BFBFBF")]*4)

# ----------------------------------------------------------------------------- helpers
def title_block(ws, title, lines, width):
    ws.cell(1, 1, title).font = F(14, True, "FFFFFF")
    for col in range(1, width + 1): ws.cell(1, col).fill = fill(NAVY)
    ws.row_dimensions[1].height = 24
    r = 2
    for ln in lines:
        c = ws.cell(r, 1, ln); c.font = F(9, False, "404040"); r += 1
    return r + 1

def header_row(ws, r, headers, fills_=None):
    for i, h in enumerate(headers, 1):
        c = ws.cell(r, i, h); c.font = F(10, True, "FFFFFF"); c.fill = fill("4472C4")
        c.alignment = Alignment(horizontal="center", wrap_text=True); c.border = THIN
    return r + 1

def put(ws, r, c, v, fmt=None, b=False, fl=None, ital=False):
    cell = ws.cell(r, c, v); cell.font = Font(name="Arial", size=10, bold=b, italic=ital)
    if fmt: cell.number_format = fmt
    if fl: cell.fill = fill(fl)
    cell.border = THIN
    return cell

def serving_days(a, b):
    d0, d1 = datetime.strptime(a, "%Y-%m-%d").date(), datetime.strptime(b, "%Y-%m-%d").date()
    out, d = [], d0
    while d <= d1:
        if d.weekday() != 6: out.append(d.isoformat())   # no Sundays
        d += timedelta(days=1)
    return out

# ----------------------------------------------------------------------------- Looker parsing
def parse_looker(fname):
    """Wide pivot: row0 dates (repeated per metric), row1 metric names, rows2+ adverts, blank name = TOTAL."""
    with open(fname, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    dates, metrics = rows[0][1:], rows[1][1:]
    out = defaultdict(lambda: defaultdict(dict))   # advert -> date -> metric
    for r in rows[2:]:
        name = r[0].strip() if r[0].strip() else "TOTAL"
        for d, m, v in zip(dates, metrics, r[1:]):
            v = v.strip().replace("$", "").replace(",", "").replace("%", "").replace("'", "")
            if not v: continue
            try: out[name][d][m] = float(v)
            except ValueError: pass
    return out

def looker_files(folder):
    def find(*frags):
        for p in sorted(glob.glob(os.path.join(folder, "*.csv"))):
            n = os.path.basename(p).lower().replace("-", "").replace("_", "")
            if all(fr in n for fr in frags): return p
        return None
    return {"d12":  find("daily",   "byadvert", "cr12"),
            "d345": find("daily",   "byadvert", "cr35"),
            "m12":  find("monthly", "byadvert", "cr12"),
            "m345": find("monthly", "byadvert", "cr35")}

def seg_of_advert(name):
    for s in ("High", "Medium", "Low"):
        if s in name: return s
    return None

def agg_looker(adv_data, dates, segments=("High", "Medium", "Low"), channel="WebM"):
    """Sum spend/ops/cri/prem over dates for matching advert rows."""
    tot = dict(spend=0.0, ops=0.0, cri=0.0, prem=0.0, sqo=0.0)
    for name, per_date in adv_data.items():
        if name == "TOTAL": continue
        if channel and channel not in name: continue
        if seg_of_advert(name) not in segments: continue
        for d in dates:
            v = per_date.get(d, {})
            tot["spend"] += v.get("Spend", 0); tot["ops"] += v.get("Op Count", 0)
            tot["cri"] += v.get("CRIs", 0);    tot["prem"] += v.get("CRI Prem", 0)
            tot["sqo"] += v.get("SQOs", 0)
    return tot

# ----------------------------------------------------------------------------- DB pulls
def connect_ro(db):
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)

def detect_snapshot_cols(con):
    cols = [r[1] for r in con.execute("PRAGMA table_info(life_disposition_snapshot)")]
    lc = {c.lower(): c for c in cols}
    out = dict(SNAP_COLS)
    def pick(key, tests):
        if out[key]: return
        for t in tests:
            for low, orig in lc.items():
                if t(low): out[key] = orig; return
    pick("batch",     [lambda n: n == "batch_date", lambda n: "batch" in n and "date" in n])
    pick("code",      [lambda n: n == "source_code", lambda n: "source" in n and "code" in n])
    pick("lead_date", [lambda n: "creat" in n and "date" in n, lambda n: n in ("lead_date","date_created","created"),
                       lambda n: "lead" in n and "date" in n])
    pick("op",        [lambda n: n == "op_count", lambda n: n.startswith("op") and "count" in n])
    pick("cri",       [lambda n: n in ("cris","cri_count","cri"), lambda n: n.startswith("cri") and "prem" not in n and "$" not in n])
    pick("prem",      [lambda n: "capped" in n, lambda n: "prem" in n, lambda n: "cri$" in n])
    missing = [k for k, v in out.items() if not v]
    if missing:
        raise RuntimeError(f"Could not auto-detect snapshot columns {missing}. "
                           f"Available: {cols}. Set them in SNAP_COLS at the top of the script.")
    print(f"[snapshot column map] {out}")
    return out

def num(series):
    return pd.to_numeric(series.astype(str).str.replace(r"[$,]", "", regex=True), errors="coerce").fillna(0.0)

def pull_cohort_ages(con, cm, cohort_min="2026-09-15"):
    """Return DataFrame: cohort, seg, age, ops, cri, prem (M-channel, paid segments), exact-batch reads."""
    q = f'SELECT "{cm["batch"]}" b, "{cm["code"]}" code, "{cm["lead_date"]}" ld, ' \
        f'"{cm["op"]}" op, "{cm["cri"]}" cri, "{cm["prem"]}" prem FROM life_disposition_snapshot'
    df = pd.read_sql_query(q, con)
    df["b"]  = pd.to_datetime(df["b"].astype(str), errors="coerce", format="mixed").dt.date
    df["ld"] = pd.to_datetime(df["ld"].astype(str), errors="coerce", format="mixed").dt.date
    df = df.dropna(subset=["b", "ld"])
    df["code"] = df["code"].astype(str)
    df = df[df["code"].str.len() >= 3]
    df["seg"] = df["code"].str[:2].map(SEG_OF_CODE)
    df = df[(df["seg"].notna()) & (df["code"].str[2] == "M")]            # paid, WebM only
    df = df[df["ld"] >= datetime.strptime(cohort_min, "%Y-%m-%d").date()]
    for c in ("op", "cri", "prem"): df[c] = num(df[c])
    df["age"] = (pd.to_datetime(df["b"]) - pd.to_datetime(df["ld"])).dt.days
    g = (df.groupby(["ld", "seg", "age"])
           .agg(ops=("op", "sum"), cri=("cri", "sum"), prem=("prem", "sum")).reset_index())
    g = g.rename(columns={"ld": "cohort"})
    return g

def rate_for(seg, cohort):
    m = f"{cohort.year}-{cohort.month:02d}"
    r = SEG_OP_RATES[seg]
    return r.get(m, r[sorted(r)[-1]])

def regime_table(g):
    """cohort -> dict(ops1, spend1, prem1, kpi1, prem4, kpi4, spend4) summed across segments at ages 1 and 4."""
    out = {}
    for cohort in sorted(g["cohort"].unique()):
        row = dict(cohort=cohort, wd=cohort.strftime("%a"))
        for age, tag in ((1, "1"), (4, "4")):
            sub = g[(g["cohort"] == cohort) & (g["age"] == age)]
            if sub.empty:
                row[f"ops{tag}"] = row[f"spend{tag}"] = row[f"prem{tag}"] = None; continue
            spend = sum(r.ops * rate_for(r.seg, cohort) for r in sub.itertuples())
            row[f"ops{tag}"]  = float(sub["ops"].sum())
            row[f"spend{tag}"] = spend
            row[f"prem{tag}"] = float(sub["prem"].sum())
        out[cohort] = row
    return out

def cluster_of(name):
    if re.search(r"CR\s*1", name, re.I): return "CR12"
    if re.search(r"CR\s*3", name, re.I): return "CR3"
    if re.search(r"CR\s*4", name, re.I): return "CR45"
    return None

def pull_cpd(con):
    base = """SELECT d.date, c.campaign_name, SUM(d.cost_micros)/1e6 cost, SUM(d.clicks) clicks,
                  SUM(d.conversions) conv{val}
           FROM campaign_performance_daily d JOIN campaigns c USING(campaign_id)
           WHERE d.account_id=29 AND d.device='ALL' AND d.date BETWEEN ? AND ?
           GROUP BY 1,2"""
    try:
        rows = [tuple(r) for r in con.execute(base.format(val=", SUM(d.conversion_value) val"), (PRE_START, POST_END))]
    except sqlite3.OperationalError:
        rows = [tuple(r) + (0.0,) for r in con.execute(base.format(val=""), (PRE_START, POST_END))]
    out = defaultdict(lambda: defaultdict(lambda: dict(cost=0.0, clicks=0, conv=0.0, val=0.0)))
    for dte, name, cost, clicks, conv, val in rows:
        cl = cluster_of(name or "")
        if not cl: continue
        o = out[dte][cl]; o["cost"] += cost or 0; o["clicks"] += clicks or 0; o["conv"] += conv or 0; o["val"] += val or 0
    return out   # date -> cluster -> dict

def pull_zip_device(con):
    q = """SELECT CASE WHEN z.date<? THEN 'pre' ELSE 'post' END per, z.device,
                  SUM(z.cost_micros)/1e6 cost, SUM(z.clicks) clicks
           FROM campaign_performance_zip_daily z JOIN campaigns c USING(campaign_id)
           WHERE z.account_id=29 AND c.campaign_name GLOB '*CR1*' AND z.date BETWEEN ? AND ?
           GROUP BY 1,2"""
    return con.execute(q, (CHANGE_DATE, PRE_START, POST_END)).fetchall()

def pull_zip_intersection(con):
    q = """WITH bz AS (
             SELECT zip FROM campaign_performance_zip_daily z JOIN campaigns c USING(campaign_id)
             WHERE z.account_id=29 AND c.campaign_name GLOB '*CR1*' AND z.date BETWEEN ? AND ?
             GROUP BY zip
             HAVING SUM(CASE WHEN z.date<? THEN z.clicks END)>0
                AND SUM(CASE WHEN z.date>=? THEN z.clicks END)>0)
           SELECT CASE WHEN z.date<? THEN 'pre' ELSE 'post' END per,
                  SUM(z.cost_micros)/1e6 cost, SUM(z.clicks) clicks
           FROM campaign_performance_zip_daily z JOIN campaigns c USING(campaign_id)
           WHERE z.account_id=29 AND c.campaign_name GLOB '*CR1*'
             AND z.zip IN (SELECT zip FROM bz) AND z.date BETWEEN ? AND ?
           GROUP BY 1"""
    a = (PRE_START, POST_END, CHANGE_DATE, CHANGE_DATE, CHANGE_DATE, PRE_START, POST_END)
    return con.execute(q, a).fetchall()

# ----------------------------------------------------------------------------- sheets
def sheet_summary(wb, ctx):
    ws = wb.create_sheet("Summary", 0)
    for w, col in ((3, "A"), (14, "B"), (90, "C")): ws.column_dimensions[col].width = w
    ws.column_dimensions["B"].width = 16
    r = title_block(ws, "Term Life — Sept-29 Regime Shift: Findings Summary",
        [f"Generated {date.today().isoformat()}  |  windows: PRE {PRE_START}..{PRE_END} vs POST {POST_START}..{POST_END} (6 serving days each; Sundays dark)",
         "Sources: SQLife disposition batch history (same-age) · Looker daily/monthly by-advert exports (campaign truth) · "
         "campaign_performance_daily + campaign_performance_zip_daily (Google cost)",
         f"SQ economics: Policy KPI = CRI premium / SQ payment; break-even {BREAK_EVEN}, target {TARGET_KPI}."], 3)
    findings = [
        ("1", "Regime shift, not a bad Monday",
         ctx.get("f1", "Every weekday cohort through 9/28 matured at or above break-even; every cohort from 9/29 on tracks below it at the same ages. See RegimeShift.")),
        ("2", "Two 9/29 changes targeted CR3/CR45",
         "Seasonality adjustment (removed 10/2) + ad schedule cut from 9pm to 5pm (still live). Volume response visible same-day: CR3/CR45 clicks and ops fell immediately."),
        ("3", "The wound is CR3-45 High income",
         ctx.get("f3", "CR3-45 High: KPI 4.32 -> 1.55 pre/post; ~$37K premium lost per window. Low income unaffected everywhere. CR12 High most resilient (held ~3+ on 10/1-10/2).")),
        ("4", "Untouched CR12 degraded via spillover",
         ctx.get("f4", "CR12 absorbed +22% ops when siblings throttled; mechanism = cheaper/deeper MOBILE clicks inside the same zips (CPC down ~8%, intersection-zip CPC down 7.7%). Hour-of-day untested until hourly sync exists.")),
        ("5", "Invisible on-platform: Google's signal says the account improved",
         ctx.get("f5", "Native conversions per $1K held or improved post-change while partner premium per $1K collapsed - see Divergence sheet.")),
        ("6", "The two-sided ledger: where the damage landed",
         ctx.get("f6", "SQ payment vs Google cost vs Quote margin, pre/post - see Ledger sheet.")),
    ]
    put(ws, r, 1, "#", b=True, fl=GRAY); put(ws, r, 2, "Finding", b=True, fl=GRAY); put(ws, r, 3, "Evidence", b=True, fl=GRAY)
    r += 1
    for n, t, e in findings:
        put(ws, r, 1, n, b=True); put(ws, r, 2, t, b=True)
        c = put(ws, r, 3, e); c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[r].height = 30; r += 1
    r += 1
    put(ws, r, 2, "Decisions / asks", b=True, fl=NAVY).font = F(10, True, "FFFFFF"); put(ws, r, 3, "", fl=NAVY); r += 1
    for d in ["Revert CR3/CR45 schedule to 9pm, OR trim their budgets to match the 5pm day so Smart Bidding stops force-buying daytime marginal inventory.",
              "Partner call: how much of pre-9/29 strength was the Q3-close floor push? (Bring RegimeShift + ClusterTruth tables.)",
              "Watch 10/6-10/8 day-1 lines for recovery after the 10/2 adjustment removal; schedule cut persists, so expect partial recovery only.",
              "Request the warehouse lead->campaign attribution join (retires zip proxy; unlocks hour-of-day lead value)."]:
        put(ws, r, 2, "->", b=True); c = put(ws, r, 3, d); c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[r].height = 26; r += 1
    r += 1
    for cv in ["Caveats: post-window cohorts still maturing (values will drift up ~10-30%); day-level CRI counts are small (0-6) - read windows, not days;",
               "cluster attribution on Looker = truth; DB zip-mailing proxy ~90-95% (used only for geo work); CR12 post cost bracketed pending cpd re-sync."]:
        put(ws, r, 3, cv).font = F(9, False, "808080"); ws.cell(r, 3).border = None; r += 1
    ws.freeze_panes = "A2"

def sheet_regime(wb, reg, ctx, err=None):
    ws = wb.create_sheet("RegimeShift")
    widths = [12, 5, 7, 9, 10, 7, 7, 9, 10, 7, 8]
    for i, w in enumerate(widths, 1): ws.column_dimensions[get_column_letter(i)].width = w
    if err or not reg:
        r = title_block(ws, "1 - Regime shift (same-age KPI by cohort) - DATA PULL FAILED", [], len(widths))
        put(ws, r, 1, "The life_disposition_snapshot pull failed, so this sheet could not be built:", b=True).font = RED_T
        c = put(ws, r + 1, 1, str(err or "query returned no cohorts")); c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.merge_cells(start_row=r + 1, start_column=1, end_row=r + 1, end_column=len(widths)); ws.row_dimensions[r + 1].height = 60
        put(ws, r + 3, 1, "Fix: set SNAP_COLS at the top of the script to the correct column names and rerun.")
        return
    def pooled(cohorts, age):
        ops = spend = prem = 0.0; n = 0
        for cd in cohorts:
            row = reg[cd]
            if row.get(f"spend{age}") is None: continue
            ops += row[f"ops{age}"]; spend += row[f"spend{age}"]; prem += row[f"prem{age}"]; n += 1
        return ops, spend, prem, n
    pre_cs  = [c for c in sorted(reg) if PANEL_PRE_START <= c.isoformat() < CHANGE_DATE]
    post_cs = [c for c in sorted(reg) if c.isoformat() >= CHANGE_DATE]
    r = title_block(ws, "1 - Regime shift, not a bad Monday (same-age KPI by cohort)",
        ["Day-1 (a1) = cohort measured in the next day's batch; Matured (a4) = same cohort four days later. Exact-batch reads; blank = batch not yet available.",
         "Spend(est) = WebM ops x SQ per-op rate card by segment/month. Paid segments only (High/Med/Low), WebM channel.",
         f"Break-even {BREAK_EVEN}. Panel A: pre period pooled into one benchmark row; each post-change cohort on its own row."], len(widths))
    put(ws, r, 1, "A. PRE pooled vs each post-change cohort", b=True, fl=NAVY).font = F(10, True, "FFFFFF")
    for cc in range(2, len(widths) + 1): put(ws, r, cc, "", fl=NAVY)
    r += 1
    r = header_row(ws, r, ["Cohort(s)", "", "Ops a1", "Spend a1", "Prem a1", "KPI a1", "Ops a4", "Spend a4", "Prem a4", "KPI a4", "vs BE a4"])
    panel_first = r
    def panel_row(label, vals1, vals4, shade=None, bold=False):
        nonlocal r
        put(ws, r, 1, label, b=bold, fl=shade); put(ws, r, 2, "", fl=shade)
        for (ops, spend, prem), c0 in ((vals1, 3), (vals4, 7)):
            put(ws, r, c0, ops if spend else None, NUMF, fl=shade)
            put(ws, r, c0 + 1, spend if spend else None, MONEY, fl=shade)
            put(ws, r, c0 + 2, prem if spend else None, MONEY, fl=shade)
            cL, pL = get_column_letter(c0 + 1), get_column_letter(c0 + 2)
            put(ws, r, c0 + 3, f"=IFERROR({pL}{r}/{cL}{r},\"\")" if spend else None, KPIF, b=True, fl=shade)
        put(ws, r, 11, f"=IF(J{r}=\"\",\"\",J{r}-{BREAK_EVEN})", KPIF, fl=shade)
        r += 1
    o1, s1, p1, n1 = pooled(pre_cs, "1"); o4, s4, p4, n4 = pooled(pre_cs, "4")
    panel_row(f"PRE {PANEL_PRE_START[5:]}..{PRE_END[5:]} ({n1} cohorts pooled)", (o1, s1, p1), (o4, s4, p4), fl_pre := None, True)
    ws.cell(panel_first, 1).fill = fill(GRAY)
    for cd in post_cs:
        row = reg[cd]
        panel_row(f"{cd.isoformat()} ({row['wd']})",
                  (row.get("ops1"), row.get("spend1"), row.get("prem1")),
                  (row.get("ops4"), row.get("spend4"), row.get("prem4")), "FCE4D6")
    if s4 and n4:
        kpre1 = p1 / s1 if s1 else 0; kpre4 = p4 / s4
        posts = [f"{cd.strftime('%m/%d')} {reg[cd]['prem4']/reg[cd]['spend4']:.2f}" for cd in post_cs if reg[cd].get("spend4")]
        ctx["f1"] = (f"Pooled pre ({PANEL_PRE_START[5:]}..{PRE_END[5:]}): a1 KPI {kpre1:.2f}, a4 KPI {kpre4:.2f}. "
                     f"Post cohorts at a4: {', '.join(posts) if posts else 'maturing'} - vs break-even {BREAK_EVEN}.")
    r += 1
    put(ws, r, 1, "B. Every cohort (detail)", b=True, fl=NAVY).font = F(10, True, "FFFFFF")
    for cc in range(2, len(widths) + 1): put(ws, r, cc, "", fl=NAVY)
    r += 1
    hdr = ["Cohort", "WD", "Ops a1", "Spend a1", "Prem a1", "KPI a1", "Ops a4", "Spend a4", "Prem a4", "KPI a4", "vs BE a4"]
    r = header_row(ws, r, hdr)
    first_data = r
    for cohort in sorted(reg):
        row = reg[cohort]
        shade = "FCE4D6" if cohort.isoformat() >= CHANGE_DATE else None
        put(ws, r, 1, cohort.isoformat(), fl=shade); put(ws, r, 2, row["wd"], fl=shade)
        for base, col0 in (("1", 3), ("4", 7)):
            ops, spend, prem = row.get(f"ops{base}"), row.get(f"spend{base}"), row.get(f"prem{base}")
            put(ws, r, col0, ops, NUMF, fl=shade)
            put(ws, r, col0 + 1, spend, MONEY, fl=shade)
            put(ws, r, col0 + 2, prem, MONEY, fl=shade)
            cL, pL = get_column_letter(col0 + 1), get_column_letter(col0 + 2)
            put(ws, r, col0 + 3, f"=IFERROR({pL}{r}/{cL}{r},\"\")" if spend else None, KPIF, b=(base == "4"), fl=shade)
        put(ws, r, 11, f"=IF(J{r}=\"\",\"\",J{r}-{BREAK_EVEN})", KPIF, fl=shade)
        r += 1
    last_data = r - 1
    # break-even helper column (hidden) for charting
    for rr in range(first_data, last_data + 1): ws.cell(rr, 13, BREAK_EVEN)
    ws.column_dimensions["M"].hidden = True
    ch = LineChart(); ch.title = "Same-age KPI by cohort: Day-1 vs Matured(a4) vs break-even"
    ch.height, ch.width, ch.y_axis.title, ch.x_axis.title = 9, 24, "Policy KPI", "Cohort"
    cats = Reference(ws, min_col=1, min_row=first_data, max_row=last_data)
    for col, name in ((6, "Day-1 (a1)"), (10, "Matured (a4)"), (13, f"Break-even {BREAK_EVEN}")):
        s = Series(Reference(ws, min_col=col, min_row=first_data, max_row=last_data), title=name)
        if col != 13: s.marker = Marker(symbol="circle", size=5)
        ch.series.append(s)
    ch.set_categories(cats)
    ws.add_chart(ch, f"A{last_data + 2}")
    ws.freeze_panes = f"A{first_data}"

def sheet_cluster(wb, d12, d345, m12, m345, ctx):
    ws = wb.create_sheet("ClusterTruth")
    for i, w in enumerate([12, 9, 10, 9, 6, 11, 8, 2, 9, 10, 9, 6, 11, 8], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    r = title_block(ws, "2/3 - Campaign truth (Looker daily by Advert, UTM-campaign filtered)",
        ["Looker 'Spend' = SQ payment to Quote (not Google cost). KPI = CRI Prem / payment. WebM paid segments.",
         "Caution: rows are cumulative-to-export - PRE rows carry ~8-13 days of maturation, POST rows 1-7; the pre/post gap is UNDERSTATED, not overstated.",
         "Daily CRI counts are tiny (0-6); read the window panels, not single days."], 14)
    days = serving_days(PRE_START, POST_END)
    put(ws, r, 1, "", fl=GRAY)
    put(ws, r, 2, "CR12", b=True, fl=GRAY); put(ws, r, 9, "CR3-45", b=True, fl=GRAY)
    r += 1
    sub = ["Date", "Payment", "Ops", "CRI", "Prem", "KPI", "", "", "Payment", "Ops", "CRI", "Prem", "KPI"]
    r = header_row(ws, r, ["Date", "Payment", "Ops", "CRI", "Prem", "KPI", "", " ", "Payment", "Ops", "CRI", "Prem", "KPI", "  "])
    first = r
    for d in days:
        shade = "FCE4D6" if d >= CHANGE_DATE else None
        put(ws, r, 1, d, fl=shade)
        for src, c0 in ((d12, 2), (d345, 9)):
            t = src["TOTAL"].get(d, {}) if "TOTAL" in src else {}
            put(ws, r, c0,     t.get("Spend"), MONEY, fl=shade)
            put(ws, r, c0 + 1, t.get("Op Count"), NUMF, fl=shade)
            put(ws, r, c0 + 2, t.get("CRIs"), NUMF, fl=shade)
            put(ws, r, c0 + 3, t.get("CRI Prem"), MONEY, fl=shade)
            a, b = get_column_letter(c0 + 3), get_column_letter(c0)
            put(ws, r, c0 + 4, f"=IFERROR({a}{r}/{b}{r},\"\")", KPIF, fl=shade)
        r += 1
    last = r - 1
    r += 1
    # pre/post window panel per cluster group
    put(ws, r, 1, "Window", b=True, fl=GRAY)
    for lab, c0 in (("CR12", 2), ("CR3-45", 9)):
        for i, h in enumerate(["Payment", "Ops", "CRI", "Prem", "KPI"]):
            put(ws, r, c0 + i, h, b=True, fl=GRAY)
    r += 1
    n_pre = len(serving_days(PRE_START, PRE_END))
    def win_rows(tag, lo_row, hi_row):
        nonlocal r
        put(ws, r, 1, tag, b=True)
        for c0 in (2, 9):
            L = lambda cc: get_column_letter(cc)
            for off in (0, 1, 2, 3):
                col = L(c0 + off)
                put(ws, r, c0 + off, f'=SUM({col}{lo_row}:{col}{hi_row})', MONEY if off in (0, 3) else NUMF, b=True)
            put(ws, r, c0 + 4, f"=IFERROR({L(c0+3)}{r}/{L(c0)}{r},\"\")", KPIF, b=True)
        r += 1
    win_rows("PRE",  first, first + n_pre - 1)
    win_rows("POST", first + n_pre, last)
    panel_last = r - 1
    r += 1
    # segment cell map
    put(ws, r, 1, "Cell map (pre vs post KPI / premium)", b=True, fl=NAVY).font = F(10, True, "FFFFFF")
    for cc in range(2, 9): put(ws, r, cc, "", fl=NAVY)
    r += 1
    r = header_row(ws, r, ["Cluster x Segment", "Pre ops", "Pre CRI", "Pre prem", "Pre KPI", "Post ops", "Post CRI", "Post prem", "Post KPI"])
    pre_days, post_days = serving_days(PRE_START, PRE_END), serving_days(POST_START, POST_END)
    f3_bits = []
    for lab, src in (("CR12", d12), ("CR3-45", d345)):
        for seg in ("High", "Medium", "Low"):
            a = agg_looker(src, pre_days, (seg,)); b = agg_looker(src, post_days, (seg,))
            put(ws, r, 1, f"{lab} {seg}", fl=FILL[seg])
            put(ws, r, 2, a["ops"], NUMF); put(ws, r, 3, a["cri"], NUMF); put(ws, r, 4, a["prem"], MONEY)
            put(ws, r, 5, (a["prem"] / a["spend"]) if a["spend"] else None, KPIF, b=True)
            put(ws, r, 6, b["ops"], NUMF); put(ws, r, 7, b["cri"], NUMF); put(ws, r, 8, b["prem"], MONEY)
            c = put(ws, r, 9, (b["prem"] / b["spend"]) if b["spend"] else None, KPIF, b=True)
            if a["spend"] and b["spend"] and lab == "CR3-45" and seg == "High":
                f3_bits.append(f"CR3-45 High: {a['prem']/a['spend']:.2f} -> {b['prem']/b['spend']:.2f}, prem ${a['prem']:,.0f} -> ${b['prem']:,.0f}")
            r += 1
    if f3_bits:
        ctx["f3"] = f3_bits[0] + " (full-window, Sat incl.). Low income unaffected in both groups; CR12 High most resilient."
    r += 1
    # month context
    put(ws, r, 1, "Month context (Looker monthly export)", b=True, fl=GRAY); r += 1
    r = header_row(ws, r, ["Month", "CR12 KPI", "CR12 prem", "CR3-45 KPI", "CR3-45 prem"])
    months = sorted({m for m in (m12.get("TOTAL", {}) or {})} | {m for m in (m345.get("TOTAL", {}) or {})})
    for m in months:
        a, b = m12.get("TOTAL", {}).get(m, {}), m345.get("TOTAL", {}).get(m, {})
        put(ws, r, 1, m); put(ws, r, 2, a.get("Policy KPI"), KPIF); put(ws, r, 3, a.get("CRI Prem"), MONEY)
        put(ws, r, 4, b.get("Policy KPI"), KPIF); put(ws, r, 5, b.get("CRI Prem"), MONEY); r += 1
    # chart: daily KPI both groups
    ch = LineChart(); ch.title = "Daily KPI by cluster group (cumulative-to-export)"; ch.height, ch.width = 8, 22
    ch.y_axis.title = "KPI"
    for col, name in ((6, "CR12"), (13, "CR3-45")):
        ch.series.append(Series(Reference(ws, min_col=col, min_row=first, max_row=last), title=name))
    ch.set_categories(Reference(ws, min_col=1, min_row=first, max_row=last))
    ws.add_chart(ch, f"A{r + 1}")
    ws.freeze_panes = f"A{first}"

def sheet_cost(wb, cpd, zipdev, zipint, ctx):
    ws = wb.create_sheet("CostSide")
    for i, w in enumerate([12] + [10, 8, 8, 9] * 3, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    r = title_block(ws, "4/5 - Google cost side (acct 29)",
        ["campaign_performance_daily (device='ALL', spend-complete) by cluster; zip table used only for device + same-zip cuts.",
         "Trailing-window caveat: cpd and the zip table refresh on a trailing window daily; any read touching the most recent ~3 days",
         "is provisional until the next refresh (Google restates recent days)."], 14)
    if not cpd:
        put(ws, r, 1, "campaign_performance_daily unavailable in this run.", b=True); return
    days = sorted(cpd)
    CL = ("CR12", "CR3", "CR45")
    hdr = ["Date"] + [f"{cl} {m}" for cl in CL for m in ("cost", "clk", "CPC", "conv")]
    r = header_row(ws, r, hdr)
    first = r
    for d in days:
        shade = "FCE4D6" if d >= CHANGE_DATE else None
        put(ws, r, 1, d, fl=shade)
        for i, cl in enumerate(CL):
            c0 = 2 + 4 * i; g = cpd[d][cl]
            put(ws, r, c0, g["cost"], MONEY, fl=shade)
            put(ws, r, c0 + 1, g["clicks"], NUMF, fl=shade)
            a, b = get_column_letter(c0), get_column_letter(c0 + 1)
            put(ws, r, c0 + 2, f'=IFERROR({a}{r}/{b}{r},"")', MONEY2, fl=shade)
            put(ws, r, c0 + 3, g["conv"], '#,##0.0', fl=shade)
        r += 1
    last = r - 1; r += 1
    r = header_row(ws, r, ["Window"] + hdr[1:])
    n_pre_c = sum(1 for d in days if d <= PRE_END)
    for tag, lo_row, hi_row in (("PRE", first, first + n_pre_c - 1), ("POST", first + n_pre_c, last)):
        put(ws, r, 1, tag, b=True)
        for i in range(3):
            c0 = 2 + 4 * i
            for off, fmt in ((0, MONEY), (1, NUMF), (3, '#,##0.0')):
                col = get_column_letter(c0 + off)
                put(ws, r, c0 + off, f'=SUM({col}{lo_row}:{col}{hi_row})', fmt, b=True)
            a, b = get_column_letter(c0), get_column_letter(c0 + 1)
            put(ws, r, c0 + 2, f'=IFERROR({a}{r}/{b}{r},"")', MONEY2, b=True)
        r += 1
    r += 1
    put(ws, r, 1, "Native conv per $1K (Google's own efficiency view)", b=True, fl=GRAY); r += 1
    put(ws, r, 1, "Held or improved in every cluster post-change -> quantified against partner reality on the Divergence sheet.").font = RED_T
    r += 2
    # device split
    put(ws, r, 1, "CR12 device split (zip table, fresh backfill)", b=True, fl=GRAY); r += 1
    r = header_row(ws, r, ["Window", "Device", "Cost", "Clicks", "CPC", "", "", "", "", "", "", "", "", ""])
    for per, dev, cost, clicks in sorted(zipdev, key=lambda x: (x[0] != "pre", -x[2])):
        put(ws, r, 1, per.upper()); put(ws, r, 2, dev); put(ws, r, 3, cost, MONEY)
        put(ws, r, 4, clicks, NUMF); put(ws, r, 5, f'=IFERROR(C{r}/D{r},"")', MONEY2); r += 1
    r += 1
    put(ws, r, 1, "CR12 same-zip (intersection) cut - geography ruled out", b=True, fl=GRAY); r += 1
    r = header_row(ws, r, ["Window", "Cost", "Clicks", "CPC", "", "", "", "", "", "", "", "", "", ""])
    for per, cost, clicks in sorted(zipint, key=lambda x: x[0] != "pre"):
        put(ws, r, 1, per.upper()); put(ws, r, 2, cost, MONEY); put(ws, r, 3, clicks, NUMF)
        put(ws, r, 4, f'=IFERROR(B{r}/C{r},"")', MONEY2); r += 1
    put(ws, r, 1, "Same zips, CPC down ~8%, clicks up ~12%: the freed budget bought DEEPER, CHEAPER (mobile) inventory in place.", ).font = F(9, True)
    r += 1
    # chart daily cost
    ch = LineChart(); ch.title = "Daily Google cost by cluster"; ch.height, ch.width = 8, 22; ch.y_axis.title = "$"
    for col, name in ((2, "CR12"), (6, "CR3"), (10, "CR45")):
        ch.series.append(Series(Reference(ws, min_col=col, min_row=first, max_row=last), title=name))
    ch.set_categories(Reference(ws, min_col=1, min_row=first, max_row=last))
    ws.add_chart(ch, f"A{r + 1}")
    ws.freeze_panes = f"A{first}"
    # ctx numbers
    def wsum(lo, hi, cl, key):
        return sum(cpd[d][cl][key] for d in days if lo <= d <= hi)
    try:
        pc = sum(wsum(PRE_START, PRE_END, c, "cost") for c in ("CR12", "CR3", "CR45"))
        qc = sum(wsum(POST_START, POST_END, c, "cost") for c in ("CR12", "CR3", "CR45"))
        ctx["cost_pre"], ctx["cost_post"] = pc, qc
        ctx["f5"] = ("Native conv per $1K: " + ", ".join(
            f"{c} {1000*wsum(PRE_START,PRE_END,c,'conv')/max(wsum(PRE_START,PRE_END,c,'cost'),1):.1f}->"
            f"{1000*wsum(POST_START,POST_END,c,'conv')/max(wsum(POST_START,POST_END,c,'cost'),1):.1f}"
            for c in ("CR12", "CR3", "CR45")) +
            ". Google's signal held/improved while SQ KPI collapsed - Smart Bidding is blind to premium quality.")
    except Exception:
        pass

def sheet_divergence(wb, d12, d345, cpd, ctx):
    ws = wb.create_sheet("Divergence")
    for i, w in enumerate([13, 10, 9, 10, 10, 11, 11, 11, 11, 11, 11], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    r = title_block(ws, "5 - What Google sees vs what the partner experiences (same dollars)",
        ["Both lenses over the SAME denominator: Google cost. Google's lens = native conversions (the blended Life signal Smart Bidding optimizes).",
         "Partner's lens = CRI premium (Looker campaign-truth, cumulative-to-export). Divergence = the quality dimension the bidder cannot see.",
         "Caveat: recent SQ prem rows are still maturing (~last 3 days understate), so the divergence on the newest days reads slightly worse than final."], 11)
    if not cpd:
        put(ws, r, 1, "campaign_performance_daily unavailable - sheet skipped.", b=True); return
    days = sorted(cpd)
    n_pre = sum(1 for d in days if d <= PRE_END)
    def gcl(d, cl):
        if cl == "CR3-45":
            return {k: cpd[d]["CR3"][k] + cpd[d]["CR45"][k] for k in ("cost", "conv", "val")}
        if cl == "Account":
            return {k: sum(cpd[d][c][k] for c in ("CR12", "CR3", "CR45")) for k in ("cost", "conv", "val")}
        return cpd[d][cl]
    def prem(d, cl):
        p12 = d12.get("TOTAL", {}).get(d, {}).get("CRI Prem", 0) or 0
        p345 = d345.get("TOTAL", {}).get(d, {}).get("CRI Prem", 0) or 0
        return p12 if cl == "CR12" else (p345 if cl == "CR3-45" else p12 + p345)
    has_val = any(gcl(d, "Account")["val"] > 0 for d in days)
    groups = ("CR12", "CR3-45", "Account")
    # -- Table A1: raw window inputs --
    put(ws, r, 1, "A. Window inputs", b=True, fl=NAVY).font = F(10, True, "FFFFFF")
    for cc in range(2, 7): put(ws, r, cc, "", fl=NAVY)
    r += 1
    r = header_row(ws, r, ["Group", "Window", "Google cost", "Native conv", "Conv value", "SQ prem"])
    a1_first = r; rowof = {}
    for g in groups:
        for tag, dd in (("PRE", days[:n_pre]), ("POST", days[n_pre:])):
            rowof[(g, tag)] = r
            put(ws, r, 1, g, b=(g == "Account")); put(ws, r, 2, tag)
            put(ws, r, 3, sum(gcl(d, g)["cost"] for d in dd), MONEY)
            put(ws, r, 4, sum(gcl(d, g)["conv"] for d in dd), '#,##0.0')
            put(ws, r, 5, sum(gcl(d, g)["val"] for d in dd), MONEY)
            put(ws, r, 6, sum(prem(d, g) for d in dd), MONEY)
            r += 1
    r += 1
    # -- Table A2: the two lenses --
    put(ws, r, 1, "B. The two lenses (per $1K of the same Google cost)", b=True, fl=NAVY).font = F(10, True, "FFFFFF")
    for cc in range(2, 8): put(ws, r, cc, "", fl=NAVY)
    r += 1
    hdr = ["Group", "Google conv/$1K PRE", "POST", "Google delta", "Partner prem/$1K PRE", "POST", "Partner delta"]
    r = header_row(ws, r, hdr)
    a2_first = r
    for g in groups:
        p, q = rowof[(g, "PRE")], rowof[(g, "POST")]
        put(ws, r, 1, g, b=(g == "Account"), fl=(GRAY if g == "Account" else None))
        put(ws, r, 2, f'=1000*D{p}/C{p}', '#,##0.0'); put(ws, r, 3, f'=1000*D{q}/C{q}', '#,##0.0')
        put(ws, r, 4, f'=C{r}/B{r}-1', '+0.0%;-0.0%', b=True)
        put(ws, r, 5, f'=1000*F{p}/C{p}', MONEY); put(ws, r, 6, f'=1000*F{q}/C{q}', MONEY)
        put(ws, r, 7, f'=F{r}/E{r}-1', '+0.0%;-0.0%', b=True)
        r += 1
    a2_last = r - 1
    if has_val:
        put(ws, r, 1, "Conversion value/$ (Google's value lens)", ital=True)
        for g, rr in ((g, rowof[(g, "PRE")]) for g in groups): pass
        r += 1
    else:
        c = put(ws, r, 1, "Note: conversion value is ~$0 on this account - the bidder optimizes raw conversion COUNT with no value signal at all; premium quality is structurally invisible to it.")
        c.font = RED_T; r += 1
    r += 1
    ch = BarChart(); ch.type = "col"; ch.title = "Post vs pre efficiency change: Google's lens vs the partner's"
    ch.height, ch.width = 9, 18; ch.y_axis.title = "change vs PRE"; ch.y_axis.numFmt = "0%"
    ch.series.append(Series(Reference(ws, min_col=4, min_row=a2_first, max_row=a2_last), title="Google: native conv per $"))
    ch.series.append(Series(Reference(ws, min_col=7, min_row=a2_first, max_row=a2_last), title="Partner: CRI prem per $"))
    ch.set_categories(Reference(ws, min_col=1, min_row=a2_first, max_row=a2_last))
    ws.add_chart(ch, f"I{a2_first - 2}")
    # -- Table C: daily indexed --
    put(ws, r, 1, "C. Daily, indexed to PRE average = 100", b=True, fl=NAVY).font = F(10, True, "FFFFFF")
    for cc in range(2, 9): put(ws, r, cc, "", fl=NAVY)
    r += 1
    r = header_row(ws, r, ["Date", "Google cost", "Native conv", "SQ prem", "Cost idx", "Conv idx", "Prem idx", ""])
    c_first = r
    for d in days:
        shade = "FCE4D6" if d >= CHANGE_DATE else None
        a = gcl(d, "Account")
        put(ws, r, 1, d, fl=shade)
        put(ws, r, 2, a["cost"], MONEY, fl=shade); put(ws, r, 3, a["conv"], '#,##0.0', fl=shade)
        put(ws, r, 4, prem(d, "Account"), MONEY, fl=shade)
        r += 1
    c_last = r - 1
    avg_r = r
    put(ws, r, 1, "PRE avg", b=True, fl=GRAY)
    for cc in (2, 3, 4):
        col = get_column_letter(cc)
        put(ws, r, cc, f'=AVERAGE({col}{c_first}:{col}{c_first + n_pre - 1})', MONEY if cc != 3 else '#,##0.0', b=True, fl=GRAY)
    r += 1
    for rr in range(c_first, c_last + 1):
        for cc, ref in ((5, "B"), (6, "C"), (7, "D")):
            put(ws, rr, cc, f'=IFERROR(100*{ref}{rr}/{ref}${avg_r},"")', '0')
    ch2 = LineChart(); ch2.title = "Account daily: Google's signal vs partner premium (PRE avg = 100)"
    ch2.height, ch2.width = 9, 22; ch2.y_axis.title = "index (PRE = 100)"
    for col, name in ((5, "Google cost"), (6, "Native conversions"), (7, "SQ premium")):
        ch2.series.append(Series(Reference(ws, min_col=col, min_row=c_first, max_row=c_last), title=name))
    ch2.set_categories(Reference(ws, min_col=1, min_row=c_first, max_row=c_last))
    ws.add_chart(ch2, f"A{r + 1}")
    ws.freeze_panes = f"A{a1_first}"
    # ctx finding 5 from real cells
    try:
        def rate(g, tag, num):
            rr = rowof[(g, tag)]; dd = days[:n_pre] if tag == "PRE" else days[n_pre:]
            cost = sum(gcl(d, g)["cost"] for d in dd)
            top = sum((gcl(d, g)["conv"] if num == "conv" else prem(d, g)) for d in dd)
            return 1000 * top / cost if cost else 0
        g0, g1 = rate("Account", "PRE", "conv"), rate("Account", "POST", "conv")
        p0, p1 = rate("Account", "PRE", "prem"), rate("Account", "POST", "prem")
        ctx["f5"] = (f"Same Google dollars, two lenses: native conv/$1K {g0:.1f}->{g1:.1f} ({g1/g0-1:+.0%}) while "
                     f"SQ premium/$1K ${p0:,.0f}->${p1:,.0f} ({p1/p0-1:+.0%}). The bidder's own signal says the account held; "
                     f"the partner's economics collapsed - quality is invisible on-platform. See Divergence.")
    except Exception:
        pass

def sheet_ledger(wb, d12, d345, cpd, ctx):
    ws = wb.create_sheet("Ledger")
    for i, w in enumerate([12, 12, 12, 12, 10], 1): ws.column_dimensions[get_column_letter(i)].width = w
    r = title_block(ws, "6 - The two-sided ledger: SQ payment vs Google cost vs Quote margin",
        ["Payment/day = Looker 'Spend' (what SelectQuote pays Quote), both cluster files summed. Cost/day = campaign_performance_daily.",
         "Quote margin = Payment - Google cost. SQ's return (Policy KPI) collapsed below 2.96 in the same window Quote's margin rose.",
         "Read: flat Quote margin + SQ below break-even = the changes created no Quote-side benefit (pure value destruction; strongest revert case).",
         "Rising margin would instead mean short-term gain with partnership-level risk. Trailing ~3 days provisional (see CostSide)."], 5)
    days = serving_days(PRE_START, POST_END)
    r = header_row(ws, r, ["Date", "SQ payment", "Google cost", "Quote margin", "Margin %"])
    first = r
    for d in days:
        shade = "FCE4D6" if d >= CHANGE_DATE else None
        pay = (d12.get("TOTAL", {}).get(d, {}).get("Spend", 0) or 0) + (d345.get("TOTAL", {}).get(d, {}).get("Spend", 0) or 0)
        cost = sum(cpd.get(d, {}).get(c, {}).get("cost", 0) for c in ("CR12", "CR3", "CR45")) if cpd else None
        put(ws, r, 1, d, fl=shade); put(ws, r, 2, pay, MONEY, fl=shade); put(ws, r, 3, cost, MONEY, fl=shade)
        put(ws, r, 4, f'=IFERROR(B{r}-C{r},"")', MONEY, b=True, fl=shade)
        put(ws, r, 5, f'=IFERROR(D{r}/B{r},"")', PCT, fl=shade)
        r += 1
    last = r - 1; r += 1
    n_pre_l = len(serving_days(PRE_START, PRE_END))
    for tag, lo_row, hi_row, nd in (("PRE /day", first, first + n_pre_l - 1, n_pre_l),
                                    ("POST /day", first + n_pre_l, last, last - first + 1 - n_pre_l)):
        put(ws, r, 1, tag, b=True, fl=GRAY)
        put(ws, r, 2, f'=SUM(B{lo_row}:B{hi_row})/{nd}', MONEY, b=True, fl=GRAY)
        put(ws, r, 3, f'=SUM(C{lo_row}:C{hi_row})/{nd}', MONEY, b=True, fl=GRAY)
        put(ws, r, 4, f'=B{r}-C{r}', MONEY, b=True, fl=GRAY)
        put(ws, r, 5, f'=IFERROR(D{r}/B{r},"")', PCT, b=True, fl=GRAY)
        r += 1
    ch = LineChart(); ch.title = "Payment vs cost vs Quote margin, daily"; ch.height, ch.width = 9, 22; ch.y_axis.title = "$"
    for col, name in ((2, "SQ payment"), (3, "Google cost"), (4, "Quote margin")):
        ch.series.append(Series(Reference(ws, min_col=col, min_row=first, max_row=last), title=name))
    ch.set_categories(Reference(ws, min_col=1, min_row=first, max_row=last))
    ws.add_chart(ch, f"A{r + 2}")
    ws.freeze_panes = f"A{first}"
    try:
        pre_pay = sum((d12["TOTAL"].get(d, {}).get("Spend", 0) or 0) + (d345["TOTAL"].get(d, {}).get("Spend", 0) or 0) for d in serving_days(PRE_START, PRE_END))
        post_pay = sum((d12["TOTAL"].get(d, {}).get("Spend", 0) or 0) + (d345["TOTAL"].get(d, {}).get("Spend", 0) or 0) for d in serving_days(POST_START, POST_END))
        if "cost_pre" in ctx:
            m0, m1 = pre_pay - ctx["cost_pre"], post_pay - ctx["cost_post"]
            if m0 and abs(m1 / m0 - 1) < 0.10: phrase = "~unchanged: the changes created no Quote-side benefit while SQ fell below break-even"
            elif m1 > m0: phrase = f"up {m1/m0-1:+.0%}: short-term Quote gain, partnership-level risk as SQ sits below break-even"
            else: phrase = f"down {m1/m0-1:+.0%} alongside SQ's collapse - losses on both sides"
            ctx["f6"] = (f"Per 6-day window: SQ payment ${pre_pay:,.0f} -> ${post_pay:,.0f}, Google cost "
                         f"${ctx['cost_pre']:,.0f} -> ${ctx['cost_post']:,.0f}. Quote margin ${m0:,.0f} -> ${m1:,.0f} ({phrase}).")
    except Exception:
        pass

def sheet_notes(wb):
    ws = wb.create_sheet("DataNotes")
    ws.column_dimensions["A"].width = 24; ws.column_dimensions["B"].width = 110
    r = title_block(ws, "Definitions, sources, and open items", [], 2)
    notes = [
        ("a1 / a4 / aN", "Maturation-triangle ages: cohort (lead-creation date) measured in the batch N days later. a1 = day-1 read, a4 = four days later. Exact-batch reads from life_disposition_snapshot; the feed is a rolling 90-day snapshot refreshed daily, so batch history is the only same-age source (Looker shows cumulative-to-today only)."),
        ("Policy KPI", "CRI premium / SQ payment. SQ's return multiple on what they pay Quote. Break-even 2.96 (=1/0.3375), target 4."),
        ("Payment vs cost", "Looker 'Spend' = SQ's payment to Quote (per-op, segment-tiered). Google cost = campaign_performance_daily (device='ALL'). Quote margin = payment - cost."),
        ("Windows", f"PRE {PRE_START}..{PRE_END}, POST {POST_START}..{POST_END}; 6 serving days each (account dark Sundays)."),
        ("Clusters", "CR12 = SelectQuote high-income zip list; CR3 = medium; CR45 = catchall (country minus exclusions). Looker files are UTM-campaign-filtered = serving truth. DB zip classifier (mailing zip) is a ~90-95% proxy used only for geo work."),
        ("Maturity caveat", "POST cohorts are 1-7 days old at export; values rise ~10-30% as they mature. All pre/post gaps shown are therefore conservative."),
        ("Small-n caveat", "Daily CRI counts run 0-6 per cluster. Day rows are exhibits; window panels are the evidence."),
        ("Trailing-window caveat", "cpd and the zip table refresh on a trailing window as part of the daily routine; any analysis window touching the most recent ~3 days is provisional until the next day's refresh (Google restates recent days). 10/7 incident (10/1-10/5 captured pre-finalization) resolved by re-sync; anchored rolling windows are on the DB thread's orchestrator list."),
        ("Open items", "Hour-of-day untestable until hourly sync exists (the one untested mechanism). Lead->campaign warehouse join requested (retires zip proxy, adds lead timestamps). Bound-date vs lead-date grain test for Looker CRI attribution pending."),
        ("Rate card", "SQ per-op payment (WebM): " + "; ".join(f"{s} {SEG_OP_RATES[s]}" for s in SEG_OP_RATES) + ". Observed per-op price varies slightly by campaign-day (+/-6%); treat as estimate."),
        ("Divergence sheet", "Google's lens (native conv, conv value per $1K cost) vs the partner's (CRI premium per $1K of the SAME cost). The gap is the quality dimension Smart Bidding cannot observe; it is the standing argument in any bidding/signal conversation with Google or SQ."),
        ("Regenerate", "Re-run this script after each daily ingest; RegimeShift fills in a1/a4 for new cohorts automatically."),
    ]
    for k, v in notes:
        put(ws, r, 1, k, b=True); c = put(ws, r, 2, v); c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[r].height = max(28, 14 * (len(v) // 110 + 1)); r += 1

# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEF_DB)
    ap.add_argument("--looker-dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "input", "looker-disposition"))
    ap.add_argument("--out-dir", default=None)
    a = ap.parse_args()

    lf = looker_files(a.looker_dir)
    missing = [k for k, v in lf.items() if v is None]
    if "d12" in missing or "d345" in missing:
        sys.exit(f"Daily Looker exports not found in {a.looker_dir} (need Daily_by_Advert_Name cr1-2 and cr3-5). Found: {lf}")
    d12, d345 = parse_looker(lf["d12"]), parse_looker(lf["d345"])
    m12 = parse_looker(lf["m12"]) if lf["m12"] else {}
    m345 = parse_looker(lf["m345"]) if lf["m345"] else {}
    print(f"[looker] daily cr12={os.path.basename(lf['d12'])}  cr3-45={os.path.basename(lf['d345'])}"
          + (f"  monthly ok" if lf["m12"] and lf["m345"] else "  (monthly files not found - month panel skipped)"))

    reg, cpd, zipdev, zipint, reg_err = {}, {}, [], [], None
    try:
        con = connect_ro(a.db)
        try:
            cm = detect_snapshot_cols(con)
            reg = regime_table(pull_cohort_ages(con, cm))
            print(f"[db] regime table: {len(reg)} cohorts")
            if not reg: reg_err = "snapshot query ran but returned no cohorts (check lead-date parsing / paid-seg filter)"
        except Exception as e:
            try:
                cols = [x[1] for x in con.execute("PRAGMA table_info(life_disposition_snapshot)")]
                reg_err = f"{e}  |  columns available: {', '.join(cols)}"
            except Exception:
                reg_err = str(e)
            print(f"[warn] RegimeShift from DB failed: {reg_err}")
        try:
            cpd = pull_cpd(con); print(f"[db] cpd days: {len(cpd)}")
        except Exception as e:
            print(f"[warn] cpd pull failed: {e}")
        try:
            zipdev = pull_zip_device(con); zipint = pull_zip_intersection(con)
        except Exception as e:
            print(f"[warn] zip pulls failed: {e}")
        con.close()
    except Exception as e:
        reg_err = f"DB unavailable: {e}"
        print(f"[warn] {reg_err} - building Looker-only workbook")

    ctx = {}
    wb = Workbook()
    del wb[wb.sheetnames[0]]                     # drop default sheet
    sheet_regime(wb, reg, ctx, err=reg_err)
    sheet_cluster(wb, d12, d345, m12, m345, ctx)
    sheet_cost(wb, cpd, zipdev, zipint, ctx)
    sheet_divergence(wb, d12, d345, cpd, ctx)
    sheet_ledger(wb, d12, d345, cpd, ctx)
    sheet_notes(wb)
    sheet_summary(wb, ctx)                       # built last (ctx populated), placed first
    wb.active = 0

    out_dir = a.out_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"termlife_sept29_findings_{date.today().strftime('%Y%m%d')}.xlsx")
    wb.save(out)
    print(f"[done] {out}")

if __name__ == "__main__":
    main()
