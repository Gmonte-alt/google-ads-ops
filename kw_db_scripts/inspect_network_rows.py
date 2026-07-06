"""
inspect_network_rows.py
=======================
The network table has 57.7% of kpd's ROWS but 1.7% of its SPEND for 2025-07-15
— it kept cheap rows, dropped expensive ones. This inspects the actual network
rows to find the mechanism: are networks colliding on the PK, is one network
missing, or is spend landing as ~0?
"""

import sqlite3
from pathlib import Path

DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
DATE = "2025-07-15"
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

# 1. Distinct network values actually stored in the network table
print("1. Distinct `network` values in keyword_performance_network_daily:")
for r in conn.execute("""
    SELECT network, COUNT(*) c, ROUND(SUM(cost_micros)/1e6,2) spend
    FROM keyword_performance_network_daily WHERE report_date=?
    GROUP BY network ORDER BY c DESC""", (DATE,)):
    print(f"   {r['network']!r:<45} rows={r['c']:>7,}  spend=${r['spend']:>12,.2f}")

# 2. Top-spend instances in kpd for that date, and how many network rows each has
print(f"\n2. Top 10 kpd instances by spend on {DATE}, vs their network rows:")
tops = conn.execute("""
    SELECT instance_id, ROUND(cost_micros/1e6,2) kpd_spend
    FROM keyword_performance_daily WHERE report_date=? AND device='ALL'
    ORDER BY cost_micros DESC LIMIT 10""", (DATE,)).fetchall()
for t in tops:
    nets = conn.execute("""
        SELECT network, ROUND(cost_micros/1e6,2) spend
        FROM keyword_performance_network_daily
        WHERE instance_id=? AND report_date=?""", (t["instance_id"], DATE)).fetchall()
    net_desc = ", ".join(f"{n['network'][:12]}=${n['spend']}" for n in nets) or "NO NETWORK ROWS"
    net_sum = sum(n["spend"] for n in nets)
    flag = "" if abs(net_sum - t["kpd_spend"]) < 0.5 else "  <-- MISMATCH"
    print(f"   inst {t['instance_id']:<8} kpd=${t['kpd_spend']:>10,.2f}  net_sum=${net_sum:>10,.2f}{flag}")
    print(f"        networks: {net_desc}")

# 3. Are there rows in the network table with cost_micros that look wrong?
print(f"\n3. Network table cost_micros distribution on {DATE}:")
r = conn.execute("""
    SELECT COUNT(*) c, ROUND(MIN(cost_micros)/1e6,4) mn, ROUND(MAX(cost_micros)/1e6,2) mx,
           ROUND(AVG(cost_micros)/1e6,4) avg,
           SUM(CASE WHEN cost_micros=0 THEN 1 ELSE 0 END) zero_rows
    FROM keyword_performance_network_daily WHERE report_date=?""", (DATE,)).fetchone()
print(f"   rows={r['c']:,}  min=${r['mn']}  max=${r['mx']}  avg=${r['avg']}  zero-spend rows={r['zero_rows']:,}")

# 4. Same for kpd
r = conn.execute("""
    SELECT COUNT(*) c, ROUND(MIN(cost_micros)/1e6,4) mn, ROUND(MAX(cost_micros)/1e6,2) mx,
           ROUND(AVG(cost_micros)/1e6,4) avg,
           SUM(CASE WHEN cost_micros=0 THEN 1 ELSE 0 END) zero_rows
    FROM keyword_performance_daily WHERE report_date=? AND device='ALL'""", (DATE,)).fetchone()
print(f"   kpd: rows={r['c']:,}  min=${r['mn']}  max=${r['mx']}  avg=${r['avg']}  zero-spend rows={r['zero_rows']:,}")

conn.close()
