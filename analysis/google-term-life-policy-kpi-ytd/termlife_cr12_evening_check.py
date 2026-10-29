#!/usr/bin/env python3
"""
termlife_cr12_evening_check.py (v1.2)
CR12 by hour bucket: connect AND Policy KPI (CRI prem / SQ pay-est).
Pay(est) = ops x per-op SQ payment rate by segment x lead month (observed Looker rates).
Read against SQ break-even ~3.03 and the 4.0 target.
Buckets: in-hours 8-17, evening 17-21, other. Clicks from 2026-07-25 (ledger horizon).
v1.2: adds prem / Pay(est) / KPI columns to the month and day-of-week tables.
Usage: python termlife_cr12_evening_check.py [--db ...]
"""
import argparse, re, sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta

DEF_DB = r"C:\Projects\google-ads-ops\db\paid_search.db"
CR12 = "22899566624"
SEG_OP_RATES = {   # per-op SQ payment by lead month (observed Looker Spend/Ops)
    "High":   {"2026-06": 150.0, "2026-07": 150.0, "2026-08": 149.67, "2026-09": 144.02, "2026-10": 148.53},
    "Medium": {"2026-06":  87.0, "2026-07":  87.0, "2026-08":  86.55, "2026-09":  84.33, "2026-10":  86.75},
    "Low":    {"2026-06":  31.0, "2026-07":  31.0, "2026-08":  30.98, "2026-09":  31.51, "2026-10":  33.24},
}
def op_rate(seg, m):
    r = SEG_OP_RATES[seg]; return r.get(m, r[sorted(r)[-1]])
SEG = {"CC": "High", "CB": "Medium", "CA": "Low"}

ap = argparse.ArgumentParser(); ap.add_argument("--db", default=DEF_DB)
a = ap.parse_args()
con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
q = """
  WITH latest AS (SELECT day, source_code, MAX(batch_date) b
                  FROM life_disposition_snapshot GROUP BY 1,2),
  leads AS (SELECT s.day, substr(s.source_code,1,2) cc,
            CAST(s.op_count AS REAL) op, CAST(s.sqo AS REAL) sqo,
            CAST(s.capped_cri_dollars AS REAL) prem,
            CASE WHEN instr(s.trk_click_id,'|')>0
                 THEN substr(s.trk_click_id,1,instr(s.trk_click_id,'|')-1)
                 ELSE s.trk_click_id END req
     FROM life_disposition_snapshot s
     JOIN latest l ON l.day=s.day AND l.source_code=s.source_code AND l.b=s.batch_date
     WHERE substr(s.source_code,1,2) IN ('CA','CB','CC') AND substr(s.source_code,3,1)='M'),
  m AS (SELECT request_id, MIN(submitted_ct) ts, MAX(gclid) g
        FROM lead_click_map WHERE request_id!='' GROUP BY 1)
  SELECT leads.cc, CAST(strftime('%H', m.ts) AS INT) hh,
         strftime('%Y-%m', m.ts) mo, CAST(strftime('%w', m.ts) AS INT) dw,
         date(m.ts) dt, leads.op, leads.sqo, leads.prem
  FROM leads JOIN m ON m.request_id=leads.req
  JOIN gclid_clicks g ON g.gclid=m.g
  WHERE g.campaign_id = ?"""
MATURE = (date.today() - timedelta(days=7)).isoformat()
DOW = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 0: "Sun"}
Z = lambda: [0.0, 0.0, 0.0, 0.0]            # ops, sqo, prem, pay
agg = defaultdict(Z); hr = defaultdict(Z); dow = defaultdict(Z)
for cc, hh, mo, dw, dt, op, sqo, prem in con.execute(q, (CR12,)):
    pay = (op or 0) * op_rate(SEG[cc], mo)
    row = [op or 0, sqo or 0, prem or 0, pay]
    b = "in-hours 8a-5p" if 8 <= hh < 17 else ("evening 5p-9p" if 17 <= hh < 21 else "other off-hrs")
    for i, v in enumerate(row):
        agg[(mo, b)][i] += v
        if mo >= "2026-09": hr[hh][i] += v
        if dt <= MATURE: dow[(DOW[dw], b)][i] += v

def line(lbl, v):
    o, s, p, y = v
    if not o: return
    kpi = p / y if y else 0
    flag = "  << below SQ break-even" if kpi < 3.03 else ("" if kpi < 4 else "  (>= target)")
    print(f"{lbl} {o:5.0f} {s:5.0f}  {s/o:6.1%}  ${p:9,.0f}  ${y:8,.0f}  {kpi:5.2f}{flag}")

HDR = "                      ops   SQO  connect   CRI prem   Pay(est)   KPI"
print("CR12 month x bucket (KPI = prem / SQ pay-est; SQ break-even ~3.03, target 4.0):")
print("month    bucket" + HDR[14:])
for (mo, b), v in sorted(agg.items()):
    line(f"{mo}  {b:14s}", v)
print("\nCR12 by hour (Sep 1 - today):")
for hh in sorted(hr):
    if hr[hh][0] >= 5: line(f"  {hh:02d}:00         ", hr[hh])
print(f"\nCR12 day-of-week x bucket (submits through {MATURE} for SQO maturity):")
for d in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"):
    for b in ("in-hours 8a-5p", "evening 5p-9p", "other off-hrs"):
        if dow[(d, b)][0] >= 5: line(f"{d}  {b:14s}", dow[(d, b)])
