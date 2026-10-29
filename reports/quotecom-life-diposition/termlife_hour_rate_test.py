#!/usr/bin/env python3
"""
termlife_hour_rate_test.py  (v1.0)

Does SQ's realized payment per op vary with the hour-of-day mix of our leads?

Inputs:
  --csv  Looker "Daily by Advert Name" pivoted export (Date across columns,
         Advert Name rows, measures incl. SQ Actual Rev / Spend / Op Count / CRI Prem)
  --db   paid_search.db (lead_click_map + life_disposition_snapshot)

Method (per segment High/Med/Low, WebM only):
  1. Daily realized rates from Looker: SQ-rev/op and Spend/op.
  2. Daily off-hours share of OPS from the feed: paid WebM leads (latest-batch
     stitch), request-id -> lead_click_map submit hour (Central),
     off-hours = outside [IN_HOURS) provisional.
  3. Monthly calibration table; ops-weighted daily regression rev_per_op ~
     off_share (slope = implied off-vs-in $ differential per op); off-share
     quintile buckets; pre/post the 9/29 schedule cut.
Flat rev/op across off-share => no hourly pricing (their CPL remark = mix).
A tilt estimates the differential from our own data.

Usage: python termlife_hour_rate_test.py --csv Daily_by_Advert_Name_3.csv [--db ...]
"""
import argparse, csv, re, sqlite3
from collections import defaultdict
from datetime import datetime

DEF_DB = r"C:\Projects\google-ads-ops\db\paid_search.db"
IN_HOURS = (8, 17)          # provisional Central in-hours [8am, 5pm)
SEG_NAME = {"High Income":"High", "Medium Income":"Med", "Low Income":"Low"}
CODE_SEG = {"CC":"High", "CB":"Med", "CA":"Low"}
CUT = "2026-09-29"          # CR3/CR45 evening schedule cut

def num(s):
    s = (s or "").replace("$","").replace(",","").replace("%","").replace("'","").strip()
    try: return float(s)
    except ValueError: return None

def load_money(path):
    """pivoted Looker csv -> {(day, seg): {rev, spend, ops, prem}} (WebM only)"""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    dates, meas = rows[0][1:], rows[1][1:]
    out = defaultdict(lambda: dict(rev=0.0, spend=0.0, ops=0.0, prem=0.0))
    key = {"SQ Actual Rev":"rev", "Spend":"spend", "Op Count":"ops", "CRI Prem":"prem"}
    for r in rows[2:]:
        name = r[0]
        if "WebM" not in name: continue
        seg = next((v for k, v in SEG_NAME.items() if k in name), None)
        if not seg: continue
        for i, (d, m) in enumerate(zip(dates, meas)):
            if d and m in key:
                v = num(r[1+i] if 1+i < len(r) else "")
                if v is not None: out[(d, seg)][key[m]] += v
    print(f"[money] {len(out)} day-segment cells, "
          f"{min(d for d,_ in out)}..{max(d for d,_ in out)}")
    return out

def load_mix(db):
    """feed ops (latest-batch stitch, paid WebM) joined to map hours ->
       {(day, seg): {ops_mapped, ops_off}}"""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    q = """
      WITH latest AS (
        SELECT day, source_code, MAX(batch_date) b
        FROM life_disposition_snapshot GROUP BY 1,2),
      leads AS (
        SELECT s.day, s.source_code, s.op_count,
               CASE WHEN instr(s.trk_click_id,'|')>0
                    THEN substr(s.trk_click_id,1,instr(s.trk_click_id,'|')-1)
                    ELSE s.trk_click_id END req
        FROM life_disposition_snapshot s
        JOIN latest l ON l.day=s.day AND l.source_code=s.source_code
                     AND l.b=s.batch_date
        WHERE substr(s.source_code,1,2) IN ('CA','CB','CC')
          AND substr(s.source_code,3,1)='M' AND s.op_count>0),
      hrs AS (
        SELECT request_id, MIN(submitted_ct) ts FROM lead_click_map
        WHERE request_id!='' GROUP BY 1)
      SELECT leads.day, substr(leads.source_code,1,2) cc,
             CAST(leads.op_count AS REAL) op_count,
             CAST(strftime('%H', hrs.ts) AS INT) hh
      FROM leads LEFT JOIN hrs ON hrs.request_id=leads.req"""
    def iso(s):
        s = (s or "").strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}", s): return s[:10]
        for f in ("%m/%d/%Y", "%m/%d/%y"):
            try: return datetime.strptime(s, f).strftime("%Y-%m-%d")
            except ValueError: pass
        return s
    out = defaultdict(lambda: dict(ops=0.0, off=0.0, unmapped=0.0))
    for day, cc, op, hh in con.execute(q):
        d = out[(iso(day), CODE_SEG[cc])]
        if hh is None: d["unmapped"] += op; continue
        d["ops"] += op
        if not (IN_HOURS[0] <= hh < IN_HOURS[1]): d["off"] += op
    tot = sum(v["ops"] for v in out.values()); un = sum(v["unmapped"] for v in out.values())
    print(f"[mix] {tot:.0f} mapped ops, {un:.0f} unmapped ({un/(tot+un):.1%}) "
          f"| in-hours = {IN_HOURS[0]:02d}:00-{IN_HOURS[1]:02d}:00 CT provisional")
    return out

def wls_slope(xs, ys, ws):
    """ops-weighted least squares: returns slope, intercept, n"""
    W = sum(ws)
    if W == 0 or len(xs) < 5: return None, None, len(xs)
    mx = sum(w*x for w, x in zip(ws, xs))/W
    my = sum(w*y for w, y in zip(ws, ys))/W
    sxx = sum(w*(x-mx)**2 for w, x in zip(ws, xs))
    if sxx == 0: return None, None, len(xs)
    sxy = sum(w*(x-mx)*(y-my) for w, x, y, in zip(ws, xs, ys))
    b = sxy/sxx
    return b, my-b*mx, len(xs)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--db", default=DEF_DB)
    ap.add_argument("--start", default="2026-06-01")
    a = ap.parse_args()
    money = load_money(a.csv)
    mix = load_mix(a.db)

    days = sorted({d for d, _ in money} & {d for d, _ in mix} )
    days = [d for d in days if d >= a.start]
    print(f"[join] {len(days)} overlapping days from {a.start}")

    for seg in ("High", "Med", "Low"):
        print(f"\n================ {seg} ================")
        # ---- monthly calibration
        mo = defaultdict(lambda: dict(rev=0.0, spend=0.0, ops=0.0, mops=0.0, off=0.0))
        for d in days:
            mv, xv = money.get((d, seg)), mix.get((d, seg))
            if not mv or not xv: continue
            m = mo[d[:7]]
            m["rev"] += mv["rev"]; m["spend"] += mv["spend"]; m["ops"] += mv["ops"]
            m["mops"] += xv["ops"]; m["off"] += xv["off"]
        print("month    ops  SQrev/op  Spend/op  off-share")
        for k in sorted(mo):
            m = mo[k]
            if not m["ops"] or not m["mops"]: continue
            print(f"{k} {m['ops']:5.0f}  {m['rev']/m['ops']:8.2f}  "
                  f"{m['spend']/m['ops']:8.2f}  {m['off']/m['mops']:8.1%}")

        # ---- daily WLS: rev/op ~ off-share
        xs, ys, ws, ss = [], [], [], []
        for d in days:
            mv, xv = money.get((d, seg)), mix.get((d, seg))
            if not mv or not xv or mv["ops"] < 5 or xv["ops"] < 5: continue
            xs.append(xv["off"]/xv["ops"]); ys.append(mv["rev"]/mv["ops"])
            ss.append(mv["spend"]/mv["ops"]); ws.append(mv["ops"])
        b, c, n = wls_slope(xs, ys, ws)
        bs, cs, _ = wls_slope(xs, ss, ws)
        if b is not None:
            print(f"\ndaily WLS (n={n} days, >=5 ops): "
                  f"SQrev/op = {c:.0f} {b:+.0f} x off-share"
                  f"  -> implied off-vs-in differential ~ ${b:.0f}/op")
            print(f"control  : Spend/op = {cs:.0f} {bs:+.0f} x off-share "
                  f"(our flat-rate model predicts ~0)")
        # ---- off-share quintiles
        order = sorted(range(len(xs)), key=lambda i: xs[i])
        k5 = max(1, len(order)//5)
        print("off-share bucket | days |  ops | SQrev/op | Spend/op")
        for q in range(5):
            idx = order[q*k5:(q+1)*k5] if q < 4 else order[4*k5:]
            if not idx: continue
            W = sum(ws[i] for i in idx)
            print(f"  {min(xs[i] for i in idx):.0%}-{max(xs[i] for i in idx):.0%}"
                  f"        | {len(idx):4d} | {W:4.0f} | "
                  f"{sum(ys[i]*ws[i] for i in idx)/W:8.2f} | "
                  f"{sum(ss[i]*ws[i] for i in idx)/W:8.2f}")
        # ---- pre/post 9/29 schedule cut
        for lbl, lo, hi in (("pre-cut  9/07-9/27", "2026-09-07", "2026-09-27"),
                            ("post-cut 10/05-end", "2026-10-05", "2099-01-01")):
            R = S = O = MO = OF = 0.0
            for d in days:
                if not (lo <= d <= hi): continue
                mv, xv = money.get((d, seg)), mix.get((d, seg))
                if not mv or not xv: continue
                R += mv["rev"]; S += mv["spend"]; O += mv["ops"]
                MO += xv["ops"]; OF += xv["off"]
            if O and MO:
                print(f"{lbl}: ops {O:4.0f} | SQrev/op {R/O:7.2f} | "
                      f"Spend/op {S/O:7.2f} | off-share {OF/MO:.1%}")

if __name__ == "__main__":
    main()
