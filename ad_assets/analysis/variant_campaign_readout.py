#!/usr/bin/env python
"""
variant_campaign_readout.py -- ad_assets/analysis/ -- the first pure-DB
creative analysis: fingerprint/variant x campaign performance on the
ADDITIVE grain (rsa_ad_performance_daily), with the gate-ruled click floor.

Answers: "is variant X's win in Tier1 replicated in Tier2, or was it the
tier, not the copy?"

Optional one-time label import: --tag-from-csv <rsa_variants_*.csv> loads
the analysis-owned variant_label column on rsa_ads from a v2-report export
(matches on ad_id). New ads since the export simply stay unlabeled and show
their fingerprint.

  python variant_campaign_readout.py --db "...\\paid_search.db"
  python variant_campaign_readout.py --db "...\\paid_search.db" --tag-from-csv "...\\exports\\rsa_variants_2026-07-18.csv"
  python variant_campaign_readout.py --db "...\\paid_search.db" --floor 500 --from 2026-06-08
"""
import argparse
import csv
import os
import sqlite3
import sys

ACCOUNT_ID = 1


def tag_from_csv(conn, path):
    if not os.path.isfile(path):
        sys.exit("[ABORT] CSV not found: %s" % path)
    n = 0
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            cur = conn.execute(
                "UPDATE rsa_ads SET variant_label = ? WHERE account_id = ? AND ad_id = ?",
                (row["variant"], ACCOUNT_ID, int(row["ad_id"])))
            n += cur.rowcount
    conn.commit()
    print("[TAG] variant_label set on %d rsa_ads rows from %s" % (n, os.path.basename(path)))


def main():
    p = argparse.ArgumentParser(description="Variant x campaign readout (additive grain).")
    p.add_argument("--db", required=True)
    p.add_argument("--from", dest="date_from", default="2026-01-01", metavar="YYYY-MM-DD")
    p.add_argument("--floor", type=int, default=300,
                   help="Minimum clicks per (variant, campaign) cell (default 300)")
    p.add_argument("--tag-from-csv", default=None,
                   help="Optional: import variant labels from a v2-report rsa_variants CSV first")
    args = p.parse_args()

    if not os.path.isfile(args.db):
        sys.exit("[ABORT] database not found: %s" % args.db)
    conn = sqlite3.connect(args.db)

    if args.tag_from_csv:
        tag_from_csv(conn, args.tag_from_csv)

    rows = conn.execute(
        """SELECT COALESCE(r.variant_label, r.fingerprint) AS variant,
                  r.campaign_id,
                  SUM(p.clicks) AS clicks,
                  ROUND(SUM(p.conversions), 1) AS conv,
                  ROUND(SUM(p.conversions_value), 0) AS value,
                  ROUND(100.0 * SUM(p.conversions) / SUM(p.clicks), 1) AS cvr_pct,
                  ROUND(SUM(p.conversions_value) / SUM(p.clicks), 2) AS vpc
           FROM rsa_ad_performance_daily p
           JOIN rsa_ads r ON r.account_id = p.account_id AND r.ad_id = p.ad_id
           WHERE p.account_id = ? AND p.device = 'ALL' AND p.report_date >= ?
           GROUP BY variant, r.campaign_id
           HAVING SUM(p.clicks) >= ?
           ORDER BY variant, conv DESC""",
        (ACCOUNT_ID, args.date_from, args.floor)).fetchall()
    conn.close()

    if not rows:
        print("No cells clear the %d-click floor since %s -- lower --floor or widen --from."
              % (args.floor, args.date_from))
        return

    print("\nVariant x campaign (device=ALL, since %s, floor %d clicks):" % (args.date_from, args.floor))
    print("%-12s %-14s %8s %8s %10s %7s %7s" % ("variant", "campaign_id", "clicks", "conv", "value", "CVR%", "VPC$"))
    prev = None
    for v, c, clicks, conv, value, cvr, vpc in rows:
        if prev is not None and v != prev:
            print("-" * 70)
        prev = v
        print("%-12s %-14s %8d %8.1f %10.0f %7.1f %7.2f" % (v, c, clicks, conv, value, cvr, vpc))
    print("\nRead across a variant's rows: consistent CVR/VPC across campaigns = the copy;")
    print("divergent = the tier/query mix. Cells below the floor are hidden, not zero.")


if __name__ == "__main__":
    main()
