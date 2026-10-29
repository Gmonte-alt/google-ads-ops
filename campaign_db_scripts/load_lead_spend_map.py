#!/usr/bin/env python3
"""
load_lead_spend_map.py  (v1.0)

Loads the Looker 'Life lead_spend_map extract' CSV (Ext Select Quote Life
Disposition Report View, lead grain) into paid_search.db as `lead_spend_map`,
then prints the monetization-break diagnostics:

  1. DAILY $0-SPEND SHARE  -- ops with no payment record, by day (onset curve).
  2. UNPAID DOLLARS        -- rate-card value of unpaid ops, by day and total.
  3. FEED JOIN RATE        -- share of map leads found in life_disposition_snapshot
                              (sq_lead_id = map account id), latest-batch stitch.
  4. CRI EXPOSURE          -- CRIs/premium sitting on unpaid leads.

Usage: python load_lead_spend_map.py --csv "G:\\My Drive\\lead_spend_map_2026-10-26.csv" [--db ...] [--replace]
"""
import argparse, csv, os, re, sqlite3, sys
from collections import defaultdict
from datetime import datetime

DEF_DB = r"C:\Projects\google-ads-ops\db\paid_search.db"
SEG_OP_RATES = {"High": 148.53, "Medium": 86.75, "Low": 33.24}   # Oct card (per-op)

FRAGS = [  # (normalized-header fragment, column) -- first match wins, order matters
    ("bound date", "bound_date"),
    ("inserted date", "inserted_date"),
    ("date month", "month"),
    ("date date", "day"),
    ("account id", "lead_id"),
    ("advert name", "advert"),
    ("strategy", "strategy"),
    ("sum spend", "spend"),
    ("sum op count", "ops"),
    ("sum sqo", "sqo"),
    ("sum cris", "cris"),
    ("sum capped cri dollars", "prem"),
    ("sq actual rev", "sq_rev"),
]

def norm(h):
    h = re.sub(r"ext select quote life disposition report view", "", h.lower())
    return re.sub(r"\s+", " ", h).strip()

def money(s):
    s = (s or "").replace("$", "").replace(",", "").replace("%", "").strip()
    if s in ("", "--"): return None
    try: return float(s)
    except ValueError: return None

def iso(s):
    s = (s or "").strip()
    for f in ("%m/%d/%Y", "%Y-%m-%d"):
        try: return datetime.strptime(s[:10], f).strftime("%Y-%m-%d")
        except ValueError: pass
    return s or None

def seg(advert):
    a = advert or ""
    return "High" if "High" in a else ("Medium" if "Medium" in a else ("Low" if "Low" in a else None))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--db", default=DEF_DB)
    ap.add_argument("--replace", action="store_true")
    a = ap.parse_args()
    con = sqlite3.connect(a.db)
    if a.replace: con.execute("DROP TABLE IF EXISTS lead_spend_map")
    con.execute("""CREATE TABLE IF NOT EXISTS lead_spend_map(
        lead_id TEXT, day TEXT, advert TEXT, strategy TEXT, spend REAL,
        ops REAL, sqo REAL, cris REAL, prem REAL, sq_rev REAL,
        bound_date TEXT, inserted_date TEXT, source_file TEXT)""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_lsm_id ON lead_spend_map(lead_id)")

    with open(a.csv, newline="", encoding="utf-8-sig") as f:
        rdr = csv.reader(f)
        hdr = next(rdr)
        pos = {}
        for i, h in enumerate(hdr):
            n = norm(h)
            for frag, col in FRAGS:
                if frag in n and col not in pos:
                    pos[col] = i; break
        missing = [c for c in ("lead_id", "day", "spend", "ops") if c not in pos]
        if missing: sys.exit(f"required columns not found: {missing} -- headers were {hdr}")
        print(f"[map] columns found: {sorted(pos)}")
        rows, src = [], os.path.basename(a.csv)
        for r in rdr:
            def g(c): return r[pos[c]].strip() if c in pos and pos[c] < len(r) else ""
            if not g("lead_id"): continue
            rows.append((g("lead_id"), iso(g("day")), g("advert"), g("strategy"),
                         money(g("spend")), money(g("ops")), money(g("sqo")),
                         money(g("cris")), money(g("prem")), money(g("sq_rev")),
                         iso(g("bound_date")), iso(g("inserted_date")), src))
        con.executemany("INSERT INTO lead_spend_map VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        con.commit()
        print(f"[load] {len(rows)} lead rows")

    # ---------- diagnostics ----------
    print("\n[1] DAILY $0-SPEND SHARE of ops (paid WebM adverts; Search + unattributed)")
    q = """SELECT day, SUM(ops) ops,
                  SUM(CASE WHEN spend IS NULL OR spend=0 THEN ops ELSE 0 END) unpaid_ops,
                  SUM(CASE WHEN spend IS NULL OR spend=0 THEN cris ELSE 0 END) unpaid_cris,
                  SUM(CASE WHEN spend IS NULL OR spend=0 THEN prem ELSE 0 END) unpaid_prem
           FROM lead_spend_map WHERE advert LIKE '%WebM%' AND day IS NOT NULL
           GROUP BY day ORDER BY day"""
    tot_unpaid_val = 0.0
    for day, ops, uo, uc, up in con.execute(q):
        if not ops: continue
        print(f"   {day}: ops {ops:4.0f} | unpaid {uo:4.0f} ({uo/ops:5.1%}) | unpaid CRIs {uc or 0:3.0f} | unpaid prem ${up or 0:9,.0f}")

    print("\n[2] UNPAID DOLLARS at rate card, by segment (WebM)")
    q2 = """SELECT advert, SUM(CASE WHEN spend IS NULL OR spend=0 THEN ops ELSE 0 END) uo,
                   SUM(CASE WHEN spend IS NULL OR spend=0 THEN prem ELSE 0 END) uprem
            FROM lead_spend_map WHERE advert LIKE '%WebM%' GROUP BY advert"""
    for advert, uo, uprem in con.execute(q2):
        s = seg(advert)
        if not s or not uo: continue
        val = uo * SEG_OP_RATES[s]; tot_unpaid_val += val
        print(f"   {s:6s}: {uo:5.0f} unpaid ops x ${SEG_OP_RATES[s]:.2f} = ${val:10,.0f} | unpaid prem ${uprem or 0:,.0f}")
    print(f"   TOTAL unpaid at card: ${tot_unpaid_val:,.0f}")

    print("\n[3] FEED JOIN RATE (map lead_id = feed sq_lead_id, latest batch)")
    q3 = """WITH latest AS (SELECT day, source_code, MAX(batch_date) b
                            FROM life_disposition_snapshot GROUP BY 1,2),
            feed AS (SELECT DISTINCT CAST(s.sq_lead_id AS TEXT) id
                     FROM life_disposition_snapshot s
                     JOIN latest l ON l.day=s.day AND l.source_code=s.source_code AND l.b=s.batch_date)
            SELECT COUNT(*), SUM(CASE WHEN feed.id IS NOT NULL THEN 1 ELSE 0 END)
            FROM lead_spend_map m LEFT JOIN feed ON feed.id=CAST(m.lead_id AS TEXT)"""
    try:
        n, k = con.execute(q3).fetchone()
        print(f"   {k}/{n} map leads found in feed ({(k or 0)/n:.1%})" if n else "   no rows")
    except sqlite3.OperationalError as e:
        print(f"   !! join skipped: {e}")

    print("\n[4] STRATEGY mix of unpaid ops (is the break attribution-shaped?)")
    q4 = """SELECT COALESCE(NULLIF(strategy,''),'(blank)') s, SUM(ops)
            FROM lead_spend_map WHERE (spend IS NULL OR spend=0) AND advert LIKE '%WebM%'
            GROUP BY 1 ORDER BY 2 DESC"""
    for s, o in con.execute(q4): print(f"   {s:12s}: {o:5.0f} ops")

if __name__ == "__main__":
    main()
