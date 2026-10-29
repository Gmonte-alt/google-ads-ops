#!/usr/bin/env python3
"""
load_lead_click_map.py  (v1.0)

Loads the Looker 'Life lead_click_map extract' CSV (ext_zip_submits) into
paid_search.db as table `lead_click_map`, then prints the three validations:

  1. JOIN MATCH RATE  -- share of paid disposition leads (latest batch, by
     lead month) whose request-id first part finds a row in the map.
  2. HOUR-OF-DAY MIX  -- share of mapped paid submits by Central-time bucket
     per month (independent replication of SQ's "in hours of operation" chart;
     bucket edges are provisional until SQ's official hours are known).
  3. GCLID COVERAGE   -- share of mapped paid submits carrying a gclid.

Join key: feed P / trk_click_id = '<18 chars>|<suffix>' -> first part before
the pipe matches the map's 'Request Ids First Part'.

Usage: python load_lead_click_map.py --csv <downloaded.csv> [--db ...] [--replace]
"""
import argparse, csv, os, re, sqlite3, sys
from datetime import datetime

DEF_DB = r"C:\Projects\google-ads-ops\db\paid_search.db"
IN_HOURS = (8, 17)   # provisional Central-time "in hours" bucket [8am, 5pm)

COLS = {   # normalized header fragment -> our column
    "request ids first part": "request_id",
    "submitted central time": "submitted_ct",
    "utm source not changed": "utm_source_raw",
    "utm source": "utm_source",
    "utm campaign new": "utm_campaign_new",
    "utm campaign": "utm_campaign",
    "utm adgroup": "utm_adgroup",
    "utm content": "utm_content",
    "utm term": "utm_term",
    "paid click id no gclid": "gclid",
    "paid click id": "click_id_raw",
    "type": "lead_type",
    "zip code": "zip",
    "device type": "device",
}

def norm(h): return re.sub(r"\s+", " ", h.lower().replace("ext zip submits", "")).strip()

def parse_ts(s):
    s = (s or "").strip()
    for f in ("%m/%d/%Y %H:%M", "%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try: return datetime.strptime(s, f).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError: pass
    return None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--db", default=DEF_DB)
    ap.add_argument("--replace", action="store_true", help="drop and reload the table")
    a = ap.parse_args()

    con = sqlite3.connect(a.db)
    if a.replace:
        con.execute("DROP TABLE IF EXISTS lead_click_map")
    con.execute("""CREATE TABLE IF NOT EXISTS lead_click_map(
        request_id TEXT, submitted_ct TEXT, utm_source TEXT, utm_source_raw TEXT,
        utm_campaign TEXT, utm_campaign_new TEXT, utm_adgroup TEXT, utm_content TEXT,
        utm_term TEXT, gclid TEXT, click_id_raw TEXT, lead_type TEXT, zip TEXT,
        device TEXT, source_file TEXT)""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_lcm_req ON lead_click_map(request_id)")

    with open(a.csv, newline="", encoding="utf-8-sig") as f:
        rdr = csv.reader(f)
        hdr = next(rdr)
        pos = {}
        for i, h in enumerate(hdr):
            n = norm(h)
            for frag, col in COLS.items():
                if n == frag and col not in pos:
                    pos[col] = i; break
        missing = [c for c in ("request_id", "submitted_ct") if c not in pos]
        if missing: sys.exit(f"required columns not found: {missing} -- headers were {hdr}")
        print(f"[map] columns found: {sorted(pos)}")
        seen, rows, raw = set(), [], 0
        src = os.path.basename(a.csv)
        for r in rdr:
            raw += 1
            def g(c): return (r[pos[c]].strip() if c in pos and pos[c] < len(r) else "")
            key = tuple(r)
            if key in seen: continue
            seen.add(key)
            rows.append((g("request_id"), parse_ts(g("submitted_ct")), g("utm_source"),
                         g("utm_source_raw"), g("utm_campaign"), g("utm_campaign_new"),
                         g("utm_adgroup"), g("utm_content"), g("utm_term"), g("gclid"),
                         g("click_id_raw"), g("lead_type"), g("zip").zfill(5) if g("zip") else "",
                         g("device"), src))
        con.executemany("INSERT INTO lead_click_map VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        con.commit()
        print(f"[load] {len(rows)} rows ({raw} raw, {raw-len(rows)} exact dupes dropped)")

    # ---------------- validations ----------------
    print("\n[1] JOIN MATCH RATE (paid disposition leads, latest batch, by lead month)")
    q1 = """
      SELECT strftime('%Y-%m', s.day) m, COUNT(*) leads,
             SUM(CASE WHEN m2.request_id IS NOT NULL THEN 1 ELSE 0 END) matched
      FROM life_disposition_snapshot s
      LEFT JOIN (SELECT DISTINCT request_id FROM lead_click_map) m2
        ON CASE WHEN instr(s.trk_click_id,'|')>0
                THEN substr(s.trk_click_id,1,instr(s.trk_click_id,'|')-1)
                ELSE s.trk_click_id END = m2.request_id
      WHERE s.batch_date=(SELECT MAX(batch_date) FROM life_disposition_snapshot)
        AND substr(s.source_code,1,2) IN ('CA','CB','CC')
      GROUP BY 1 ORDER BY 1"""
    for m, n, k in con.execute(q1):
        print(f"   {m}: {k}/{n} ({k/n:.1%})" if n else f"   {m}: 0")

    print(f"\n[2] HOUR-OF-DAY MIX of mapped paid submits (Central; in-hours = {IN_HOURS[0]:02d}:00-{IN_HOURS[1]:02d}:00 provisional)")
    q2 = f"""
      SELECT strftime('%Y-%m', submitted_ct) m, COUNT(*) n,
             SUM(CASE WHEN CAST(strftime('%H',submitted_ct) AS INT) >= {IN_HOURS[0]}
                       AND CAST(strftime('%H',submitted_ct) AS INT) < {IN_HOURS[1]}
                      THEN 1 ELSE 0 END) inh
      FROM lead_click_map
      WHERE submitted_ct IS NOT NULL AND utm_source='google'
      GROUP BY 1 ORDER BY 1"""
    for m, n, inh in con.execute(q2):
        print(f"   {m}: in-hours {inh/n:.1%} | off-hours {1-inh/n:.1%}  (n={n})")

    print("\n[3] GCLID COVERAGE of mapped paid submits")
    q3 = """
      SELECT strftime('%Y-%m', submitted_ct) m, COUNT(*) n,
             SUM(CASE WHEN gclid!='' THEN 1 ELSE 0 END) g
      FROM lead_click_map WHERE utm_source='google' GROUP BY 1 ORDER BY 1"""
    for m, n, g in con.execute(q3):
        print(f"   {m}: {g}/{n} ({g/n:.1%})")

if __name__ == "__main__":
    main()
