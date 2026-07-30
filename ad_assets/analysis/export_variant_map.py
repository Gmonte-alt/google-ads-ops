#!/usr/bin/env python
"""
export_variant_map.py -- ad_assets/analysis/ -- the canonical variant
identity map, as a CSV. One row per fingerprint: label (if any), scale,
placements, flagged-territory marker, YTD performance. Ends "label lore" --
durable documents cite fingerprints; this file is the translation table.

Output: ad_assets\\exports\\variant_identity_map_<date>.csv
READ-ONLY.

  python export_variant_map.py --db "C:\\Projects\\google-ads-ops\\db\\paid_search.db"
"""
import argparse
import csv
import datetime
import os
import sqlite3
import sys

ACCOUNT_ID = 1
FLAG_PATTERNS = ["geico", "state farm", "statefarm"]
EXPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "exports")
NAME_TABLE_CANDIDATES = [
    ("ad_groups", "ad_group_id", "ad_group_name"),
    ("ad_groups", "ad_group_id", "name"),
    ("ad_groups", "id", "name"),
]


def find_names(conn):
    for table, idc, namec in NAME_TABLE_CANDIDATES:
        for scoped in (True, False):
            try:
                q = f"SELECT {idc}, {namec} FROM {table}"
                params = ()
                if scoped:
                    q += " WHERE account_id = ?"
                    params = (ACCOUNT_ID,)
                return {int(i): (n or "") for i, n in conn.execute(q, params).fetchall()}
            except sqlite3.Error:
                continue
    return {}


def main():
    p = argparse.ArgumentParser(description="Export the canonical variant identity map.")
    p.add_argument("--db", required=True)
    args = p.parse_args()
    if not os.path.isfile(args.db):
        sys.exit("[ABORT] database not found: %s" % args.db)
    os.makedirs(EXPORT_DIR, exist_ok=True)
    conn = sqlite3.connect("file:%s?mode=ro" % args.db.replace("\\", "/"), uri=True)
    names = find_names(conn)

    rows = conn.execute(
        """SELECT r.fingerprint,
                  MAX(r.variant_label),
                  COUNT(*) AS n_ads,
                  SUM(CASE WHEN r.ad_status='ENABLED' THEN 1 ELSE 0 END),
                  COUNT(DISTINCT r.campaign_id),
                  GROUP_CONCAT(DISTINCT r.campaign_id),
                  COUNT(DISTINCT r.ad_group_id),
                  MIN(r.first_seen_date), MAX(r.last_seen_date),
                  SUM(p.clicks), SUM(p.conversions), SUM(p.conversions_value)
           FROM rsa_ads r
           LEFT JOIN rsa_ad_performance_daily p
             ON p.account_id=r.account_id AND p.ad_id=r.ad_id AND p.device='ALL'
           WHERE r.account_id=?
           GROUP BY r.fingerprint
           ORDER BY SUM(p.clicks) DESC""", (ACCOUNT_ID,)).fetchall()

    # ad-group names + flagged-territory marker per fingerprint
    ag_rows = conn.execute(
        "SELECT fingerprint, DISTINCT_LIST.ad_group_id FROM "
        "(SELECT DISTINCT fingerprint, ad_group_id FROM rsa_ads WHERE account_id=?) "
        "AS DISTINCT_LIST", (ACCOUNT_ID,)).fetchall() if False else conn.execute(
        "SELECT DISTINCT fingerprint, ad_group_id FROM rsa_ads WHERE account_id=?",
        (ACCOUNT_ID,)).fetchall()
    conn.close()

    fp_groups = {}
    for fp, ag in ag_rows:
        fp_groups.setdefault(fp, []).append(ag)

    stamp = datetime.date.today().isoformat()
    out = os.path.join(EXPORT_DIR, "variant_identity_map_%s.csv" % stamp)
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["fingerprint", "variant_label", "n_ads", "n_enabled_ads",
                    "n_campaigns", "campaign_ids", "n_ad_groups",
                    "sample_ad_group_names", "in_flagged_territory",
                    "first_seen", "last_seen",
                    "ytd_clicks", "ytd_conversions", "ytd_value"])
        for (fp, label, n_ads, n_en, n_c, camps, n_ag,
             first, last, clicks, conv, value) in rows:
            group_names = [names.get(ag, "") for ag in fp_groups.get(fp, [])]
            flagged = any(any(pat in gn.lower() for pat in FLAG_PATTERNS)
                          for gn in group_names if gn)
            sample = "; ".join(sorted({gn for gn in group_names if gn})[:3])
            w.writerow([fp, label or "", n_ads, n_en, n_c, camps, n_ag, sample,
                        "YES" if flagged else "no", first, last,
                        clicks or 0, round(conv or 0, 1), round(value or 0, 0)])
    print("[DONE] %s (%d fingerprints; cite these, not labels, in durable docs)"
          % (out, len(rows)))


if __name__ == "__main__":
    main()
