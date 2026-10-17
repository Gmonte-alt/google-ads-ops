#!/usr/bin/env python
"""Print the headline set for given fingerprints (drift diff vs build cards).
   python dump_fp_headlines.py --db "C:\\...\\paid_search.db" 4740d4a5d3 bb4ee9d935"""
import argparse
import sqlite3

p = argparse.ArgumentParser()
p.add_argument("--db", required=True)
p.add_argument("fps", nargs="+")
a = p.parse_args()
conn = sqlite3.connect("file:%s?mode=ro" % a.db.replace("\\", "/"), uri=True)
for fp in a.fps:
    rows = conn.execute(
        """SELECT DISTINCT t.text FROM rsa_ads r
           JOIN rsa_ad_assets c ON c.account_id=r.account_id AND c.ad_id=r.ad_id
            AND c.field_type='HEADLINE' AND c.removed_date IS NULL
           JOIN asset_text_data t ON t.account_id=c.account_id AND t.asset_id=c.asset_id
           WHERE r.account_id=1 AND r.fingerprint=? ORDER BY t.text""", (fp,)).fetchall()
    print("\n%s  (%d headlines)" % (fp, len(rows)))
    for (txt,) in rows:
        print("  %-46s (%d)" % (txt, len(txt)))
conn.close()
