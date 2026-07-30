"""
diag_classify_other.py
======================
Print the full names of every campaign that Step 1 classified as OTHER,
plus the INTERMEDIATE campaigns (which should be 9 state-named bridge
campaigns per earlier spot check). This identifies what's getting
misrouted so we can fix classify_campaign() correctly.


    python C:\Projects\google-ads-ops\analysis\google-geo-analysis\diag_classify_other.py
    
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
ACCOUNT_ID = 1

SINGLE_STATE_CAMPAIGNS = {"CALIFORNIA", "TEXAS", "FLORIDA"}
TIER_KWS = ("TIER ONE", "TIER TWO", "TIER THREE", "TIER FOUR")


def classify(name):
    n = name.upper()
    for kw in TIER_KWS:
        if kw in n and "HOME INSURANCE" in n:
            return kw
    if "PMAX" in n:
        return "PMAX"
    if n.endswith("-SEARCH-MOBILE-CPA-STE"):
        return "TEST"
    if n.endswith("-SEARCH-MOBILE-USA-STE"):
        return "LEGACY"
    if n.startswith("HOME INSURANCE-"):
        for s in SINGLE_STATE_CAMPAIGNS:
            if s in n:
                return "SINGLE_STATE"
        return "OTHER"
    return "INTERMEDIATE"


conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

rows = conn.execute("""
    SELECT DISTINCT c.campaign_id, c.campaign_name
    FROM campaigns c
    WHERE c.account_id = ?
      AND c.campaign_id IN (
        SELECT DISTINCT campaign_id
        FROM campaign_performance_geo_daily_search
        WHERE account_id = ?
      )
    ORDER BY c.campaign_name
""", (ACCOUNT_ID, ACCOUNT_ID)).fetchall()

from collections import defaultdict
by_class = defaultdict(list)
for r in rows:
    by_class[classify(r["campaign_name"])].append(r["campaign_name"])

print("Campaigns with geo data, grouped by current classification:")
print()
for cls in sorted(by_class.keys()):
    names = by_class[cls]
    print(f"── {cls}  ({len(names)} campaigns) ──")
    for name in names:
        print(f"  {name}")
    print()

conn.close()
