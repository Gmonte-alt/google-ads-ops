"""
check_after_single_load.py
==========================
The March-31 file just wrote 101,776 network rows (per the loader counter).
This checks what's actually in the network table for March NOW, immediately
after that load. If it's ~101,776, the write persisted and the earlier gap was
a stale-state artifact (re-running all files fixes it). If it's still ~4,000,
rows are vanishing at write time and we look at the PK/device interaction.

Also checks whether the network table accidentally contains non-'ALL' device
rows (which would mean device-level writes are landing there and colliding).
"""

import sqlite3
from pathlib import Path

DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

# 1. Network rows for March 2026 right now
r = conn.execute("""
    SELECT COUNT(*) c, ROUND(SUM(cost_micros)/1e6,2) spend
    FROM keyword_performance_network_daily
    WHERE report_date BETWEEN '2026-04-01' AND '2026-04-30'
""").fetchone()
print(f"Network rows for April 2026 NOW: {r['c']:,}  spend=${r['spend']:,}")
print(f"  (the load just reported writing 101,776 network rows for these dates)")

# 2. Distinct device values in the network table (should be ONLY 'ALL')
print("\nDistinct device values in keyword_performance_network_daily:")
for r in conn.execute("""
    SELECT device, COUNT(*) c FROM keyword_performance_network_daily
    GROUP BY device ORDER BY c DESC"""):
    print(f"  device={r['device']!r:<14} rows={r['c']:,}")

# 3. Distinct network values (looking for a collapsing value)
print("\nDistinct network values in keyword_performance_network_daily:")
for r in conn.execute("""
    SELECT network, COUNT(*) c FROM keyword_performance_network_daily
    GROUP BY network ORDER BY c DESC"""):
    print(f"  network={r['network']!r:<40} rows={r['c']:,}")

# 4. For ONE specific March date the file covered, kpd vs network row counts
for d in ('2026-04-15', '2026-04-30'):
    k = conn.execute("SELECT COUNT(*) FROM keyword_performance_daily WHERE report_date=? AND device='ALL'", (d,)).fetchone()[0]
    n = conn.execute("SELECT COUNT(*) FROM keyword_performance_network_daily WHERE report_date=?", (d,)).fetchone()[0]
    print(f"\n{d}: kpd ALL rows={k:,}   network rows={n:,}")

# 5. The KEY test: for one instance+date, how many network rows (should be 1-3, one per network)?
sample = conn.execute("""
    SELECT instance_id, report_date, COUNT(*) c
    FROM keyword_performance_network_daily
    WHERE report_date='2026-04-30'
    GROUP BY instance_id, report_date
    ORDER BY c DESC LIMIT 5
""").fetchall()
print("\nInstances with most network rows on 2026-04-30 (expect up to 3: Audience/MS/AOL):")
for r in sample:
    nets = conn.execute("""SELECT network, ROUND(cost_micros/1e6,2) s
        FROM keyword_performance_network_daily
        WHERE instance_id=? AND report_date='2026-04-30'""", (r['instance_id'],)).fetchall()
    print(f"  inst {r['instance_id']}: {r['c']} rows -> " +
          ", ".join(f"{n['network'][:10]}=${n['s']}" for n in nets))

conn.close()
