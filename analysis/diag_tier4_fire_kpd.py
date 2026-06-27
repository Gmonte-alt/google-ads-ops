"""
Quick diagnostic — what did the API write for Tier Four fire insurance
instances after the re-sync? Compares what's in kpd vs what the UI shows.
"""
import sqlite3
from pathlib import Path
from datetime import date, timedelta

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

# The two Tier Four fire insurance instances
TIER4_IIDS = [420369, 420370]  # 420369=EXACT, 420370=BROAD
TIER3_IIDS = [420740, 420741]  # 420740=EXACT, 420741=BROAD

start = "2026-03-16"
end   = "2026-04-14"

print("=" * 70)
print("Tier Four fire insurance — kpd raw rows (device=ALL, geo_state=ALL)")
print(f"Window: {start} → {end}")
print("=" * 70)

for iid in TIER4_IIDS + TIER3_IIDS:
    meta = conn.execute("""
        SELECT ki.match_type, ki.campaign_name, ki.ad_group_name,
               ki.criterion_id, ki.ad_group_id
        FROM keyword_instances ki WHERE ki.id = ?
    """, (iid,)).fetchone()

    agg = conn.execute("""
        SELECT SUM(clicks) AS clicks, SUM(cost_micros)/1e6 AS spend,
               SUM(primary_conv_count) AS convs,
               SUM(primary_conv_value) AS revenue,
               COUNT(*) AS days
        FROM keyword_performance_daily
        WHERE instance_id = ? AND device='ALL' AND geo_state='ALL'
          AND report_date BETWEEN ? AND ?
    """, (iid, start, end)).fetchone()

    camp = str(meta['campaign_name'] or '').replace("HOME INSURANCE-","").replace("-NB-SEARCH-ALL-USA-STE","")
    print(f"\n  iid={iid} [{meta['match_type']}] {camp}")
    print(f"  criterion_id={meta['criterion_id']}  ad_group_id={meta['ad_group_id']}")
    if agg['days']:
        roas = agg['revenue']/agg['spend'] if agg['spend'] > 0 else None
        roas_str = f"{roas:.3f}" if roas else "n/a"
        print(f"  days={agg['days']}  clicks={agg['clicks']}  spend=${agg['spend']:.2f}  "
              f"convs={agg['convs']:.1f}  revenue=${agg['revenue']:.2f}  ROAS={roas_str}")
    else:
        print("  NO ROWS in this window")

# Now check what criterion_ids were written for these ad_group_ids
print("\n" + "=" * 70)
print("All kpd rows written for Tier Four ad groups (any criterion_id)")
print("=" * 70)

tier4_ag_ids = [str(conn.execute("SELECT ad_group_id FROM keyword_instances WHERE id=?", (iid,)).fetchone()['ad_group_id']) for iid in TIER4_IIDS]

for ag_id in set(tier4_ag_ids):
    rows = conn.execute("""
        SELECT ki.criterion_id, ki.match_type, ki.id AS iid,
               SUM(kpd.cost_micros)/1e6 AS spend,
               SUM(kpd.primary_conv_count) AS convs,
               SUM(kpd.primary_conv_value) AS revenue
        FROM keyword_performance_daily kpd
        JOIN keyword_instances ki ON ki.id = kpd.instance_id
        WHERE ki.ad_group_id = ?
          AND kpd.device='ALL' AND kpd.geo_state='ALL'
          AND kpd.report_date BETWEEN ? AND ?
        GROUP BY ki.id
        ORDER BY spend DESC
    """, (ag_id, start, end)).fetchall()

    print(f"\n  ad_group_id={ag_id}:")
    if rows:
        for r in rows:
            print(f"    iid={r['iid']}  crit={r['criterion_id']}  [{r['match_type']}]  "
                  f"spend=${r['spend']:.2f}  convs={r['convs']:.1f}  rev=${r['revenue']:.2f}")
    else:
        print("    No rows found")

conn.close()
print("\nDone.")
