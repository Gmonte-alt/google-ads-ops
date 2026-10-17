"""
termlife_disposition_probe.py — v2
Profiles the daily partner disposition CSVs:
  - Day range (snapshot window) per file
  - Status vocabulary with counts
  - Where the funnel/revenue signal sits (Sqo / Cris / Capped_Cri$)
  - Account_Id uniqueness within and across files (upsert key check)
"""
import csv
from pathlib import Path
from collections import Counter, defaultdict

folder = Path(r"C:\Projects\google-ads-ops\campaign_db_scripts\input\life-SQL-Generic-disposition\20260924")
ENC = "cp1252"

def nonzero(v):
    return v.strip() not in ("", "0", "0.00", "0.0000")

all_ids = defaultdict(list)          # Account_Id -> [codes it appears in]
global_status = Counter()

for f in sorted(folder.glob("*.csv")):
    code = f.stem.split("_")[4]
    with open(f, newline="", encoding=ENC) as fh:
        rows = list(csv.DictReader(fh))

    days = sorted(r["Day"] for r in rows if r["Day"].strip())
    statuses = Counter(r["Status"] for r in rows)
    global_status.update(statuses)

    ids = [r["Account_Id"] for r in rows]
    dupes_within = len(ids) - len(set(ids))
    for i in set(ids):
        all_ids[i].append(code)

    n_sqo  = sum(1 for r in rows if nonzero(r["Sqo"]))
    n_cri  = sum(1 for r in rows if nonzero(r["Cris"]))
    n_rev  = sum(1 for r in rows if nonzero(r["Capped_Cri$"]))
    rev    = sum(float(r["Capped_Cri$"]) for r in rows if nonzero(r["Capped_Cri$"]))
    n_bound = sum(1 for r in rows if r["Bound_Date"].strip())
    n_txn  = sum(1 for r in rows if r["Transaction_Id"].strip())

    print(f"=== {code} | rows: {len(rows)} ===")
    print(f"  Day range: {days[0]} -> {days[-1]}" if days else "  Day range: (none)")
    print(f"  Account_Id: {len(set(ids))} distinct, {dupes_within} dupes within file")
    print(f"  Sqo>0: {n_sqo} | Cris>0: {n_cri} | Capped_Cri$>0: {n_rev} (sum ${rev:,.2f}) | Bound_Date set: {n_bound}")
    print(f"  Transaction_Id populated: {n_txn}/{len(rows)}")
    print(f"  Status values ({len(statuses)}):")
    for s, c in statuses.most_common():
        print(f"    {c:5d}  {s}")
    print()

# cross-file overlap
overlap = {i: codes for i, codes in all_ids.items() if len(codes) > 1}
print("=== CROSS-FILE ===")
print(f"Account_Ids appearing in >1 file: {len(overlap)}")
for i, codes in list(overlap.items())[:10]:
    print(f"  {i}: {codes}")

print(f"\n=== GLOBAL STATUS VOCABULARY ({len(global_status)} distinct) ===")
for s, c in global_status.most_common():
    print(f"  {c:5d}  {s}")


# Looke at 63 dupes directly.
print("\n=== DUPE DRILL-DOWN ===")
for f in sorted(folder.glob("*M?_*.csv")) or sorted(folder.glob("*.csv")):
    code = f.stem.split("_")[4]
    if not code.endswith("MQ"):
        continue
    with open(f, newline="", encoding=ENC) as fh:
        rows = list(csv.DictReader(fh))
    by_id = defaultdict(list)
    for r in rows:
        by_id[r["Account_Id"]].append(r)
    for aid, grp in by_id.items():
        if len(grp) > 1:
            print(f"\n{code} Account_Id={aid} ({len(grp)} rows)")
            for r in grp:
                print(f"  Day={r['Day']} Status={r['Status']!r} Sqo={r['Sqo']} "
                      f"Cris={r['Cris']} Cri$={r['Capped_Cri$']} OP={r['OP_Count']} "
                      f"Txn={r['Transaction_Id'][:8]}")