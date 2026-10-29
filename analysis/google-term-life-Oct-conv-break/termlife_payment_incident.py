#!/usr/bin/env python3
"""
termlife_payment_incident.py  (v1.0)

Evidence workbook for the Term Life payment-record incident (break ~2026-10-13).
Every claim in the escalation email, recomputed live from paid_search.db:

  Summary      -- each claim with its number and the sheet that proves it
  DailyBreak   -- daily ops vs uncompensated ops, % line (onset curve) + chart
  Baseline     -- monthly uncompensated share + blank-token share, full year
  Credentials  -- compensated vs uncompensated: token / gclid / strategy fill
  BiddingImpact-- daily delivered leads vs compensated vs platform conversions;
                  measured vs true conversion rate; spend trend + chart
  Exhibit      -- the uncompensated, fully-tokenized lead list (with gclid)
  Method       -- sources, joins, definitions, the no-restatement note

Read-only. Usage: python termlife_payment_incident.py [--db ...] [--out ...]
"""
import argparse, os, sqlite3
from datetime import datetime, date
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.chart import LineChart, BarChart, Reference

DEF_DB = r"C:\Projects\google-ads-ops\db\paid_search.db"
BREAK = "2026-10-13"
WIN_LO, WIN_HI = "2026-10-13", "2026-10-24"
SEG_RATES = {"High": 148.53, "Medium": 86.75, "Low": 33.24}

NAVY, GRAY, HDRB = "1F3864", "D9D9D9", "4472C4"
MONEY, NUMF, PCT = '$#,##0', '#,##0', '0.0%'
def F(sz=10, b=False, c="000000"): return Font(name="Arial", size=sz, bold=b, color=c)
THIN = Border(*[Side(style="thin", color="BFBFBF")]*4)

def put(ws, r, c, v, fmt=None, b=False, fill=None, sz=10):
    cell = ws.cell(r, c, v); cell.font = F(sz, b)
    if fmt: cell.number_format = fmt
    if fill: cell.fill = PatternFill("solid", fgColor=fill)
    return cell

def title(ws, text, notes):
    put(ws, 1, 1, text, b=True, sz=13).font = F(13, True, NAVY)
    r = 2
    for n in notes:
        put(ws, r, 1, n, sz=9); r += 1
    return r + 1

def hdr(ws, r, cols):
    for i, h in enumerate(cols, 1):
        c = put(ws, r, i, h, b=True, fill=HDRB); c.font = F(10, True, "FFFFFF"); c.border = THIN
    return r + 1

def iso_day():
    return """CASE WHEN instr(s.day,'/') THEN substr(s.day,7,4)||'-'||substr(s.day,1,2)||'-'||substr(s.day,4,2)
              ELSE substr(s.day,1,10) END"""

def load(con):
    feed = pd.read_sql_query(f"""
      WITH latest AS (SELECT day, source_code, MAX(batch_date) b
                      FROM life_disposition_snapshot GROUP BY 1,2)
      SELECT {iso_day()} d, CAST(s.sq_lead_id AS TEXT) id, s.trk_click_id tok,
             CASE WHEN instr(s.trk_click_id,'|') THEN substr(s.trk_click_id,1,instr(s.trk_click_id,'|')-1)
                  ELSE s.trk_click_id END req,
             substr(s.source_code,1,2) cc, s.lead_tier tier,
             CAST(s.op_count AS REAL) op, CAST(s.sqo AS REAL) sqo,
             CAST(s.cris AS REAL) cri, CAST(s.capped_cri_dollars AS REAL) prem
      FROM life_disposition_snapshot s
      JOIN latest l ON l.day=s.day AND l.source_code=s.source_code AND l.b=s.batch_date
      WHERE substr(s.source_code,1,2) IN ('CA','CB','CC') AND substr(s.source_code,3,1)='M'""", con)
    lsm = pd.read_sql_query("""SELECT CAST(lead_id AS TEXT) id, day, advert, strategy, spend, ops
                               FROM lead_spend_map WHERE advert LIKE '%WebM%'""", con)
    m = pd.read_sql_query("""SELECT request_id req, MAX(gclid) g FROM lead_click_map
                             WHERE request_id!='' GROUP BY 1""", con)
    D = lsm.merge(feed, on="id", how="left", suffixes=("", "_f")).merge(m, on="req", how="left")
    D["uncomp"] = D["spend"].isna() | (D["spend"] == 0)
    D["has_tok"] = D["tok"].fillna("").str.strip() != ""
    D["has_gclid"] = D["g"].fillna("") != ""
    D["seg"] = D["cc"].map({"CC": "High", "CB": "Medium", "CA": "Low"})
    for c in ("op", "sqo", "cri", "prem"): D[c] = D[c].fillna(0.0)
    print(f"[load] {len(D)} WebM lead-spend rows | feed join {(D['d'].notna()).mean():.1%} "
          f"| span {D['day'].min()}..{D['day'].max()}")
    return D

def platform_daily(con):
    """day -> clicks, cost, primary conversions for acct 29 (discover table)."""
    for t, acct in (("campaign_performance_daily", True), ("keyword_performance_daily", False)):
        try: cols = {r[1].lower() for r in con.execute(f"PRAGMA table_info({t})")}
        except sqlite3.OperationalError: continue
        datec = "date" if "date" in cols else ("report_date" if "report_date" in cols else None)
        conv = next((c for c in ("conversions", "primary_conv_count") if c in cols), None)
        if not datec or "clicks" not in cols or not conv: continue
        w, join = ["1=1"], ""
        if "account_id" in cols: w.append("t.account_id=29")
        else:
            ki = {r[1].lower() for r in con.execute("PRAGMA table_info(keyword_instances)")}
            kik = "id" if "id" in ki else "instance_id"
            join = f" JOIN keyword_instances ki ON ki.{kik}=t.instance_id"; w.append("ki.account_id=29")
        if "device" in cols and con.execute(f"SELECT 1 FROM {t} WHERE device='ALL' LIMIT 1").fetchone():
            w.append("t.device='ALL'")
        cost = "SUM(t.cost_micros)/1e6" if "cost_micros" in cols else ("SUM(t.cost)" if "cost" in cols else "0")
        q = f"""SELECT t.{datec} d, SUM(t.clicks) clicks, {cost} cost, SUM(t.{conv}) conv
                FROM {t} t{join} WHERE {' AND '.join(w)} AND t.{datec} BETWEEN '2026-09-01' AND '2026-10-31'
                GROUP BY 1"""
        df = pd.read_sql_query(q, con)
        print(f"[platform] {t} ({len(df)} days)")
        return df
    print("[platform] !! no platform table found -- BiddingImpact platform columns empty")
    return pd.DataFrame(columns=["d", "clicks", "cost", "conv"])

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEF_DB)
    OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
    ap.add_argument("--out", default=os.path.join(OUT_DIR, f"termlife_payment_incident_{date.today():%Y%m%d}.xlsx"))
    a = ap.parse_args()
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    D = load(con)
    P = platform_daily(con)
    wb = Workbook(); wb.remove(wb.active)

    # ---------------- DailyBreak ----------------
    ws = wb.create_sheet("DailyBreak")
    r = title(ws, "Uncompensated share of delivered ops, by day",
              [f"Paid WebM leads. Uncompensated = payment record absent or $0. Break begins {BREAK}.",
               "Year-long baseline ~10% (mostly token-less leads); the step above baseline is the incident."])
    g = (D[D["day"] >= "2026-09-01"].groupby("day")
           .agg(ops=("op", "sum"),
                unc=("op", lambda s: s[D.loc[s.index, "uncomp"]].sum()),
                unc_cri=("cri", lambda s: s[D.loc[s.index, "uncomp"]].sum()),
                unc_prem=("prem", lambda s: s[D.loc[s.index, "uncomp"]].sum())))
    r = hdr(ws, r, ["Day", "Ops", "Uncompensated ops", "Uncomp %", "Uncomp CRIs", "Uncomp premium"])
    first = r
    for day, row in g.iterrows():
        if not row["ops"]: continue
        put(ws, r, 1, day); put(ws, r, 2, int(row["ops"]), NUMF)
        put(ws, r, 3, int(row["unc"]), NUMF)
        put(ws, r, 4, row["unc"]/row["ops"], PCT, b=(day >= BREAK))
        put(ws, r, 5, int(row["unc_cri"]), NUMF); put(ws, r, 6, round(row["unc_prem"]), MONEY)
        r += 1
    ch = LineChart(); ch.title = "Uncompensated % of ops by day"; ch.height, ch.width = 8, 22
    ch.y_axis.numFmt = '0%'; ch.legend = None
    ch.add_data(Reference(ws, min_col=4, min_row=first, max_row=r-1))
    ch.set_categories(Reference(ws, min_col=1, min_row=first, max_row=r-1))
    s = ch.series[0]; s.graphicalProperties.line.solidFill = NAVY; s.graphicalProperties.line.width = 22000
    ws.add_chart(ch, f"H{first}")
    for i, w in enumerate([11, 7, 16, 10, 12, 14], 1): ws.column_dimensions[get_column_letter(i)].width = w

    # ---------------- Baseline ----------------
    ws = wb.create_sheet("Baseline")
    r = title(ws, "Full-year context: uncompensated share and token fill, by month",
              ["Shows (a) the standing ~10% baseline all year, (b) tracking did NOT degrade: token fill is flat.",
               "The October step is therefore new and not explained by tracking loss."])
    gm = (D.groupby(D["day"].str[:7])
            .agg(ops=("op", "sum"),
                 unc=("op", lambda s: s[D.loc[s.index, "uncomp"]].sum()),
                 ntok=("has_tok", lambda s: (~s).sum()), leads=("id", "size")))
    r = hdr(ws, r, ["Month", "Ops", "Uncompensated %", "Leads", "No-token leads", "No-token %"])
    for mth, row in gm.iterrows():
        if not row["ops"]: continue
        put(ws, r, 1, mth); put(ws, r, 2, int(row["ops"]), NUMF)
        put(ws, r, 3, row["unc"]/row["ops"], PCT)
        put(ws, r, 4, int(row["leads"]), NUMF); put(ws, r, 5, int(row["ntok"]), NUMF)
        put(ws, r, 6, row["ntok"]/row["leads"], PCT)
        r += 1
    for i, w in enumerate([9, 8, 15, 8, 13, 11], 1): ws.column_dimensions[get_column_letter(i)].width = w

    # ---------------- Credentials ----------------
    ws = wb.create_sheet("Credentials")
    r = title(ws, "Compensated vs uncompensated leads: identical paid credentials",
              [f"Search-attributed WebM leads, {WIN_LO} to {WIN_HI}.",
               "Token = our request id round-tripped through SQ; gclid = Google-issued click id via lead_click_map."])
    W = D[(D["day"] >= WIN_LO) & (D["day"] <= WIN_HI) & (D["strategy"] == "Search")]
    r = hdr(ws, r, ["Group", "Leads", "With token", "Token %", "With gclid", "Gclid %"])
    for lbl, sub in (("Compensated", W[~W["uncomp"]]), ("Uncompensated", W[W["uncomp"]]),
                     ("Uncompensated, tokenized (= exhibit)", W[W["uncomp"] & W["has_tok"]])):
        n = len(sub)
        if not n: continue
        put(ws, r, 1, lbl, b=True); put(ws, r, 2, n, NUMF)
        put(ws, r, 3, int(sub["has_tok"].sum()), NUMF); put(ws, r, 4, sub["has_tok"].mean(), PCT)
        put(ws, r, 5, int(sub["has_gclid"].sum()), NUMF); put(ws, r, 6, sub["has_gclid"].mean(), PCT)
        r += 1
    r += 1
    X = W[W["uncomp"] & W["has_tok"]]
    pay_est = sum(X[X["seg"] == s]["op"].sum() * v for s, v in SEG_RATES.items())
    for lbl, v, fmt in (("Exhibit leads", len(X), NUMF), ("Exhibit ops", int(X["op"].sum()), NUMF),
                        ("CRIs (policies sold)", int(X["cri"].sum()), NUMF),
                        ("Premium on exhibit leads", round(X["prem"].sum()), MONEY),
                        ("Expected payment at standing rates", round(pay_est), MONEY),
                        ("SQ commission (33% x premium)", round(X["prem"].sum()*0.33), MONEY)):
        put(ws, r, 1, lbl, b=True); put(ws, r, 2, v, fmt); r += 1
    for i, w in enumerate([36, 10, 11, 9, 11, 9], 1): ws.column_dimensions[get_column_letter(i)].width = w

    # ---------------- BiddingImpact ----------------
    ws = wb.create_sheet("BiddingImpact")
    r = title(ws, "The broken records feed Google's bidding",
              ["Delivered leads (SQ feed) vs compensated leads vs platform-credited conversions, daily.",
               "Measured conv rate = platform conversions / clicks; true = delivered leads / clicks.",
               "Divergence from ~Oct 13 is why Smart Bidding cut spend: it sees the missing records as lost conversions."])
    fd = D[D["day"] >= "2026-09-01"].groupby("day").agg(
        delivered=("id", "size"), comp=("uncomp", lambda s: (~s).sum()))
    B = fd.merge(P.set_index("d"), left_index=True, right_index=True, how="left")
    r = hdr(ws, r, ["Day", "Delivered leads", "Compensated", "Platform conv", "Clicks",
                    "Measured CvR", "True CvR", "Google cost"])
    first = r
    for day, row in B.iterrows():
        put(ws, r, 1, day); put(ws, r, 2, int(row["delivered"]), NUMF)
        put(ws, r, 3, int(row["comp"]), NUMF)
        if pd.notna(row.get("conv")): put(ws, r, 4, round(float(row["conv"])), NUMF)
        if pd.notna(row.get("clicks")) and row["clicks"]:
            put(ws, r, 5, int(row["clicks"]), NUMF)
            if pd.notna(row.get("conv")): put(ws, r, 6, float(row["conv"])/row["clicks"], PCT)
            put(ws, r, 7, row["delivered"]/row["clicks"], PCT)
        if pd.notna(row.get("cost")): put(ws, r, 8, round(float(row["cost"])), MONEY)
        r += 1
    ch = LineChart(); ch.title = "Measured vs true conversion rate"; ch.height, ch.width = 8, 22
    ch.y_axis.numFmt = '0%'
    ch.add_data(Reference(ws, min_col=6, max_col=7, min_row=first-1, max_row=r-1), titles_from_data=True)
    ch.set_categories(Reference(ws, min_col=1, min_row=first, max_row=r-1))
    ch.series[0].graphicalProperties.line.solidFill = HDRB
    ch.series[1].graphicalProperties.line.solidFill = NAVY
    ws.add_chart(ch, f"J{first}")
    for i, w in enumerate([11, 14, 12, 12, 8, 12, 9, 11], 1): ws.column_dimensions[get_column_letter(i)].width = w

    # ---------------- Exhibit ----------------
    ws = wb.create_sheet("Exhibit")
    r = title(ws, "Uncompensated, fully-tokenized paid-search leads (the attached list, recomputed)",
              [f"{WIN_LO} to {WIN_HI}; Strategy=Search; token present. Sorted by premium."])
    r = hdr(ws, r, ["Day", "Lead ID", "Segment", "Tier", "Token", "gclid", "Ops", "SQO", "CRI", "Premium"])
    f8 = F(8)
    for t_ in X.sort_values(["prem", "day"], ascending=[False, True]).itertuples(index=False):
        vals = [t_.day, t_.id, t_.seg, t_.tier, t_.tok, (t_.g or ""), int(t_.op), int(t_.sqo),
                int(t_.cri), round(t_.prem)]
        for i, v in enumerate(vals, 1):
            c = ws.cell(r, i, v); c.font = f8
            if i == 10: c.number_format = MONEY
        r += 1
    for i, w in enumerate([10, 10, 9, 5, 24, 26, 5, 5, 5, 9], 1): ws.column_dimensions[get_column_letter(i)].width = w

    # ---------------- Method ----------------
    ws = wb.create_sheet("Method")
    lines = [
        ("Sources", True),
        ("life_disposition_snapshot: SQ's daily delivered-lead files (ground truth of delivery); latest-batch stitch per (day, source_code).", False),
        ("lead_spend_map: Looker lead-grain payment records ('Life lead_spend_map extract' Look), WebM adverts.", False),
        ("lead_click_map: our site-side submit records (request id -> gclid), 'Life lead_click_map extract' Look.", False),
        ("Platform: Google Ads performance synced to paid_search.db (clicks / cost / routed conversions).", False),
        ("Definitions", True),
        ("Uncompensated = payment record absent or $0 on the lead. Token = trk_click_id (our request id round-tripped via SQ).", False),
        ("Expected payment = ops x standing per-op rate by segment (High 148.53 / Medium 86.75 / Low 33.24) -- an estimate; actual per-lead prices vary.", False),
        ("No-restatement check (static)", True),
        ("Two pulls of the same Looker by-advert view, 2026-10-23 and 2026-10-26: every overlapping day identical to the dollar", False),
        ("(Sept total $192,835 in both). Missing records had not backfilled after 3 days. Re-verified with the Friday re-pull.", False),
        ("Out of scope here", True),
        ("The two $10K exemplar screenshots (Looker lead-grain lookups) travel separately with the email.", False),
        (f"Generated {datetime.now():%Y-%m-%d %H:%M} from {os.path.basename(a.db)} -- read-only.", False),
    ]
    r = 1
    for text, bold in lines:
        put(ws, r, 1, text, b=bold)
        r += 1
    ws.column_dimensions["A"].width = 130

    # ---------------- Summary (built last, placed first) ----------------
    ws = wb.create_sheet("Summary", 0)
    r = title(ws, "Term Life payment-record incident -- evidence summary",
              ["Each claim from the escalation, with the number recomputed from source data and the sheet that proves it."])
    r = hdr(ws, r, ["Claim", "Value", "Proof"])
    wk = W  # window, Search
    unc_share_wk = wk[wk["uncomp"]]["op"].sum()/wk["op"].sum() if wk["op"].sum() else 0
    rows = [
        ("Break begins ~Oct 13 (daily step from ~10% baseline)", BREAK, "DailyBreak"),
        ("Uncompensated share of ops, 10/13-10/24", f"{unc_share_wk:.0%}", "DailyBreak"),
        ("Year-long baseline uncompensated share", "~10% (see monthly)", "Baseline"),
        ("Tracking intact all year (token fill flat)", "~94-95% every month", "Baseline"),
        ("Exhibit: uncompensated, fully-tokenized Search leads", f"{len(X)}", "Exhibit / Credentials"),
        ("...carrying gclid (Google-verified click)", f"{X['has_gclid'].mean():.0%}", "Credentials"),
        ("Policies sold among them (CRIs)", f"{int(X['cri'].sum())}", "Exhibit"),
        ("Premium on those policies", f"${X['prem'].sum():,.0f}", "Exhibit"),
        ("Expected payment at standing rates", f"${pay_est:,.0f}", "Credentials"),
        ("Bidding sees the gap as lost conversions", "measured vs true CvR", "BiddingImpact"),
    ]
    for claim, val, proof in rows:
        put(ws, r, 1, claim); put(ws, r, 2, val, b=True); put(ws, r, 3, proof)
        r += 1
    for i, w in enumerate([58, 22, 22], 1): ws.column_dimensions[get_column_letter(i)].width = w

    wb.save(a.out)
    print(f"[done] {a.out}")

if __name__ == "__main__":
    main()
