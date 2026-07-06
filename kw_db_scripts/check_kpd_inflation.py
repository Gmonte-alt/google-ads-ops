"""
check_kpd_inflation.py
======================
The source keyword file says 2026-03-10 spend = $1,970. The network table
agrees ($1,970). But kpd says $103,095 for that date — inflated 52x.

kpd is the one that's wrong. This finds the inflation mechanism:
  A) multiple instance_ids per real keyword (same keyword loaded many times
     under different instance rows, each carrying full spend), or
  B) kpd 'ALL' device rows that are themselves sums of already-summed rows, or
  C) kpd holds many more keyword rows than the file (data from another source).

If it's (A), we'll see keywords with N distinct instance_ids each contributing
spend. If kpd's per-date spend genuinely reflects real keywords the file lacks,
that's a different story (kpd built from a richer source than these files).
"""

import sqlite3
from pathlib import Path

DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
DATE = "2026-03-10"
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

# A) Duplicate instance_ids per keyword text on this date
print(f"A) Keywords with MULTIPLE instance_ids in kpd on {DATE} (device='ALL'):")
rows = conn.execute("""
    SELECT ki.keyword_text, ki.match_type,
           COUNT(DISTINCT k.instance_id) n_inst,
           ROUND(SUM(k.cost_micros)/1e6,2) spend
    FROM keyword_performance_daily k
    JOIN keyword_instances ki ON ki.id = k.instance_id
    WHERE k.report_date=? AND k.device='ALL'
    GROUP BY ki.keyword_text, ki.match_type
    HAVING COUNT(DISTINCT k.instance_id) > 1
    ORDER BY spend DESC LIMIT 10
""", (DATE,)).fetchall()
if rows:
    for r in rows:
        print(f"   {r['keyword_text'][:40]:<40} ({r['match_type']}) "
              f"{r['n_inst']} instances  ${r['spend']:,.2f}")
    dup_spend = conn.execute("""
        SELECT ROUND(SUM(s)/1e6,2) FROM (
          SELECT SUM(k.cost_micros) s
          FROM keyword_performance_daily k
          JOIN keyword_instances ki ON ki.id=k.instance_id
          WHERE k.report_date=? AND k.device='ALL'
          GROUP BY ki.keyword_text, ki.match_type
          HAVING COUNT(DISTINCT k.instance_id) > 1
        )
    """, (DATE,)).fetchone()[0]
    print(f"   => total spend on multi-instance keywords: ${dup_spend:,.2f}")
else:
    print("   none — each keyword has one instance_id. Not the cause.")

# B) total kpd rows vs distinct keyword texts on this date
tot_rows = conn.execute("SELECT COUNT(*) FROM keyword_performance_daily WHERE report_date=? AND device='ALL'", (DATE,)).fetchone()[0]
dist_kw = conn.execute("""
    SELECT COUNT(DISTINCT ki.keyword_text||'::'||ki.match_type)
    FROM keyword_performance_daily k JOIN keyword_instances ki ON ki.id=k.instance_id
    WHERE k.report_date=? AND k.device='ALL'""", (DATE,)).fetchone()[0]
print(f"\nB) kpd rows on {DATE}: {tot_rows:,}   distinct keyword+matchtype: {dist_kw:,}")
print(f"   ratio (rows per keyword): {tot_rows/dist_kw:.2f}  (>1 means duplication)")

# C) Sanity: what campaigns is kpd's spend under on this date? (old vs new structure)
print(f"\nC) kpd spend by campaign structure on {DATE}:")
rows = conn.execute("""
    SELECT CASE
             WHEN ki.campaign_name LIKE '%STATE-GROUP%Tier%' THEN 'NEW tier-group'
             WHEN ki.campaign_name LIKE '%-NB-SEARCH-ALL-USA-STE' THEN 'OLD -NB-'
             WHEN ki.campaign_name LIKE '%-SEARCH-MOBILE-USA-STE' THEN 'OLD -MOBILE-'
             ELSE 'other'
           END struct,
           COUNT(DISTINCT k.instance_id) n_inst,
           ROUND(SUM(k.cost_micros)/1e6,2) spend
    FROM keyword_performance_daily k JOIN keyword_instances ki ON ki.id=k.instance_id
    WHERE k.report_date=? AND k.device='ALL'
    GROUP BY struct ORDER BY spend DESC
""", (DATE,)).fetchall()
for r in rows:
    print(f"   {r['struct']:<18} instances={r['n_inst']:>6,}  spend=${r['spend']:>12,.2f}")

conn.close()
