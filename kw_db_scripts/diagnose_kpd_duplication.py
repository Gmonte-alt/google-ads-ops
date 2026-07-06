"""
diagnose_kpd_duplication.py
===========================
kpd is inflated ~23x by duplicate instance_ids per keyword (farmers insurance =
55 instances). Before scoping the fix, this determines the CAUSE:

  (1) Append-accumulation: the 55 instances were created at different times
      across restructures (spread of first_seen_date / different ad_group_ids).
      Fix: reset-then-reload rebuilds keyword_instances clean; duplicates
      collapse because each keyword maps to its current single instance.

  (2) Instance-minting bug: the loader creates a NEW instance for the same
      (keyword, ad_group) even when one exists — duplicates would recur even
      after a clean reload. Fix: the instance-resolution/creation logic.

If the 55 instances have DIFFERENT ad_group_ids (different campaigns/structure),
it's (1) — restructure accumulation, benign, fixed by clean reload.
If they have the SAME ad_group_id + criterion_id, it's (2) — a real minting bug.
"""

import sqlite3
from pathlib import Path

DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

# Take the worst offender: farmers insurance BROAD
print("The 55 instances of 'farmers insurance' (BROAD) — what distinguishes them?\n")
rows = conn.execute("""
    SELECT ki.id, ki.criterion_id, ki.ad_group_id, ki.campaign_name,
           ki.first_seen_date, ki.last_seen_date, ki.status
    FROM keyword_instances ki
    WHERE ki.account_id=2 AND lower(ki.keyword_text)='farmers insurance'
      AND ki.match_type='BROAD'
    ORDER BY ki.first_seen_date
""").fetchall()
print(f"Total instances: {len(rows)}")
print(f"\n  {'inst_id':<8} {'criterion_id':<16} {'ad_group_id':<16} {'first_seen':<12} {'campaign'}")
for r in rows[:20]:
    print(f"  {r['id']:<8} {r['criterion_id']:<16} {r['ad_group_id']:<16} "
          f"{r['first_seen_date']:<12} {(r['campaign_name'] or '')[:35]}")

# Key discriminator: distinct ad_group_ids and criterion_ids among them
n_ag = len(set(r["ad_group_id"] for r in rows))
n_cr = len(set(r["criterion_id"] for r in rows))
n_pairs = len(set((r["ad_group_id"], r["criterion_id"]) for r in rows))
print(f"\n  distinct ad_group_ids: {n_ag}")
print(f"  distinct criterion_ids: {n_cr}")
print(f"  distinct (ad_group_id, criterion_id) pairs: {n_pairs}")
print(f"  total instances: {len(rows)}")

print("\nDiagnosis:")
if n_pairs == len(rows):
    print("  Each instance has a UNIQUE (ad_group_id, criterion_id) pair.")
    print("  => CAUSE (1): restructure accumulation. The same keyword lives under")
    print("     many ad groups/campaigns built over time. A clean reset-then-reload")
    print("     rebuilds instances against CURRENT structure only; duplicates collapse.")
elif n_pairs < len(rows):
    print(f"  {len(rows)-n_pairs} instances DUPLICATE an existing (ad_group_id, criterion_id).")
    print("  => CAUSE (2): instance-minting bug — the loader created a new instance")
    print("     for a pair that already had one. This would RECUR after a clean reload")
    print("     unless the creation logic is fixed. Needs investigation before reload.")

# Scale check: how many total Microsoft instances vs how many DISTINCT
# (ad_group_id, criterion_id) pairs — the ratio is the overall inflation factor
tot = conn.execute("SELECT COUNT(*) FROM keyword_instances WHERE account_id=2").fetchone()[0]
dist = conn.execute("""
    SELECT COUNT(*) FROM (SELECT DISTINCT ad_group_id, criterion_id
    FROM keyword_instances WHERE account_id=2)""").fetchone()[0]
print(f"\nAccount-wide: {tot:,} instances, {dist:,} distinct (ad_group,criterion) pairs")
if dist:
    print(f"  ratio: {tot/dist:.2f}  (1.0 = no duplication; >1 = minting duplicates)")

conn.close()
