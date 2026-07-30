#!/usr/bin/env python
"""
get_variant_lines.py -- ad_assets/analysis/ -- print a variant's FULL,
untruncated line texts (descriptions by default), serve-ordered, plus a
single "|"-joined line for pasting into record sheets.

  python get_variant_lines.py --db "...\\paid_search.db" --variant V3
  python get_variant_lines.py --db "...\\paid_search.db" --variant V3 --field HEADLINE
"""
import argparse
import os
import sqlite3
import sys

ACCOUNT_ID = 1


def main():
    p = argparse.ArgumentParser(description="Full line texts for a variant (read-only).")
    p.add_argument("--db", required=True)
    p.add_argument("--variant", required=True, help="Variant label or fingerprint (e.g. V3)")
    p.add_argument("--field", default="DESCRIPTION", choices=["DESCRIPTION", "HEADLINE"])
    args = p.parse_args()
    if not os.path.isfile(args.db):
        sys.exit("[ABORT] database not found: %s" % args.db)
    conn = sqlite3.connect("file:%s?mode=ro" % args.db.replace("\\", "/"), uri=True)

    rows = conn.execute(
        """SELECT t.text, COALESCE(SUM(ap.impressions), 0) AS impr
           FROM rsa_ads r
           JOIN rsa_ad_assets c
             ON c.account_id = r.account_id AND c.ad_id = r.ad_id
            AND c.field_type = ? AND c.removed_date IS NULL
           JOIN asset_text_data t
             ON t.account_id = c.account_id AND t.asset_id = c.asset_id
           LEFT JOIN rsa_asset_performance_daily ap
             ON ap.account_id = c.account_id AND ap.ad_id = c.ad_id
            AND ap.asset_id = c.asset_id AND ap.field_type = c.field_type
            AND ap.device = 'ALL'
           WHERE r.account_id = ?
             AND COALESCE(r.variant_label, r.fingerprint) = ?
           GROUP BY t.text
           ORDER BY impr DESC""",
        (args.field, ACCOUNT_ID, args.variant)).fetchall()
    conn.close()

    if not rows:
        sys.exit("No %s lines found for %s (check the label)." % (args.field, args.variant))

    print("%s %sS (full text, serve-ordered):\n" % (args.variant, args.field))
    for i, (text, _) in enumerate(rows, 1):
        print("  %2d. %s" % (i, text))
    print("\nPipe-separated for the record sheet:\n")
    print(" | ".join(text for text, _ in rows))


if __name__ == "__main__":
    main()
