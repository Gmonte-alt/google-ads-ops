#!/usr/bin/env python
"""
fix_geo_constants_termlife.py -- finds the geo_target_constant ids Term Life's
geographic_view serves that are MISSING from geo_target_constants (the FK
parents whose absence zeroed the geo backfill), fetches their details from the
API, and seeds them. Dry-run by default.

    python fix_geo_constants_termlife.py
    python fix_geo_constants_termlife.py --apply
"""
import argparse
import re
import sqlite3

from dotenv import load_dotenv
load_dotenv()
from google.ads.googleads.client import GoogleAdsClient

DB = r"C:\Projects\google-ads-ops\db\paid_search.db"
CUSTOMER_ID = "1671155176"
START, END = "2024-10-01", "2026-09-23"

ap = argparse.ArgumentParser()
ap.add_argument("--apply", action="store_true")
args = ap.parse_args()

client = GoogleAdsClient.load_from_storage()
ga = client.get_service("GoogleAdsService")

print("1) distinct geo constants served by Term Life %s..%s:" % (START, END))
served = set()
for batch in ga.search_stream(customer_id=CUSTOMER_ID, query=f"""
        SELECT segments.geo_target_state, metrics.impressions
        FROM geographic_view
        WHERE segments.date BETWEEN '{START}' AND '{END}'"""):
    for row in batch.results:
        m = re.search(r"(\d+)$", row.segments.geo_target_state or "")
        if m:
            served.add(int(m.group(1)))
print("   %d distinct constants" % len(served))

conn = sqlite3.connect(DB)
cols = [r[1] for r in conn.execute("PRAGMA table_info(geo_target_constants)")]
print("2) geo_target_constants columns:", cols)
known = {r[0] for r in conn.execute("SELECT criterion_id FROM geo_target_constants")}
missing = sorted(served - known)
print("3) MISSING parents (the FK killers): %s" % (missing if missing else "NONE"))

if not missing:
    print("   -> zero missing constants: the FK failure is NOT this table.")
    print("      Run: PRAGMA foreign_key_list(campaign_performance_geo_daily) and send output.")
    conn.close()
    raise SystemExit(0)

print("4) fetching details for missing ids:")
details = []
ids_csv = ",".join(str(x) for x in missing)
for batch in ga.search_stream(customer_id=CUSTOMER_ID, query=f"""
        SELECT geo_target_constant.id, geo_target_constant.name,
               geo_target_constant.target_type, geo_target_constant.country_code,
               geo_target_constant.canonical_name
        FROM geo_target_constant
        WHERE geo_target_constant.id IN ({ids_csv})"""):
    for row in batch.results:
        g = row.geo_target_constant
        details.append((g.id, g.name, g.target_type, g.country_code, g.canonical_name))
        print("   %-10s %-24s %-10s %s | %s" % (g.id, g.name, g.target_type, g.country_code, g.canonical_name[:40]))

if not args.apply:
    print("[DRY RUN] nothing written. Re-run with --apply to seed these parents.")
else:
    ins_cols = [c for c in ("criterion_id", "name", "target_type", "country_code", "canonical_name") if c in cols]
    ph = ",".join("?" * len(ins_cols))
    field_idx = {"criterion_id": 0, "name": 1, "target_type": 2, "country_code": 3, "canonical_name": 4}
    for d in details:
        conn.execute("INSERT OR REPLACE INTO geo_target_constants (%s) VALUES (%s)"
                     % (",".join(ins_cols), ph),
                     tuple(d[field_idx[c]] for c in ins_cols))
    conn.commit()
    print("[APPLY] seeded %d constants. Re-run the geo backfill now." % len(details))
conn.close()
