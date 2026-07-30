#!/usr/bin/env python
"""
query_bcc01_ordered_content.py -- ad_assets/diagnostics/ -- READ-ONLY probe:
the AS-LOADED, SLOT-ORDERED headlines and descriptions for the pause-target
ads, straight from ad_group_ad.ad.responsive_search_ad (the one API surface
that preserves storage-slot order). This is the Editor content-matching
input: Editor diffs slot-by-slot, so pause rows must carry each ad's lines
in its own slot order -- which the DB deliberately does not store (identity
and measurement are order-blind by design; fingerprints sort).

Output: ad_assets\\exports\\bcc01_ordered_content_<date>.csv
        (ad_id, field_type, slot, text, pinned_field)
The generator fills pause-row Headline/Description slots from THIS file,
positionally, instead of from CreativeDB's alphabetical sets. CreativeDB
compositions remain the verification cross-check (same sets, different
order = expected; different SETS = drift, stop).

  python query_bcc01_ordered_content.py ^
      --pause-csv "...\\exports\\bcc01_pause_ranking_2026-07-23.csv"
"""
import argparse
import csv
import datetime
import os
import sys

from dotenv import load_dotenv

load_dotenv()

try:
    from google.ads.googleads.client import GoogleAdsClient
except ImportError:
    sys.exit("[ABORT] google-ads library not found -- run inside the paid-search conda env.")

CUSTOMER_ID = os.environ.get("PROBE_CUSTOMER_ID", "8886502147").replace("-", "")
EXPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "exports")


def main():
    p = argparse.ArgumentParser(description="Pull slot-ordered RSA content for pause ads.")
    p.add_argument("--pause-csv", required=True,
                   help="Any CSV with an ad_id column (use the DKI-filtered pause set "
                        "or the full ranking file)")
    args = p.parse_args()
    if not os.path.isfile(args.pause_csv):
        sys.exit("[ABORT] not found: %s" % args.pause_csv)
    os.makedirs(EXPORT_DIR, exist_ok=True)

    with open(args.pause_csv, newline="", encoding="utf-8") as f:
        ad_ids = sorted({int(r["ad_id"]) for r in csv.DictReader(f)})

    client = GoogleAdsClient.load_from_storage()
    ga = client.get_service("GoogleAdsService")
    rows = []
    for batch in ga.search_stream(customer_id=CUSTOMER_ID, query=f"""
        SELECT ad_group_ad.ad.id,
               ad_group_ad.ad.responsive_search_ad.headlines,
               ad_group_ad.ad.responsive_search_ad.descriptions
        FROM ad_group_ad
        WHERE ad_group_ad.ad.id IN ({",".join(str(i) for i in ad_ids)})
    """):
        rows.extend(batch.results)

    stamp = datetime.date.today().isoformat()
    out = os.path.join(EXPORT_DIR, "bcc01_ordered_content_%s.csv" % stamp)
    n_lines = 0
    seen = set()
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ad_id", "field_type", "slot", "text", "pinned_field"])
        for r in rows:
            ad_id = r.ad_group_ad.ad.id
            seen.add(ad_id)
            rsa = r.ad_group_ad.ad.responsive_search_ad
            for i, h in enumerate(rsa.headlines, 1):
                pin = h.pinned_field.name
                w.writerow([ad_id, "HEADLINE", i, h.text,
                            "" if pin in ("UNSPECIFIED", "UNKNOWN") else pin])
                n_lines += 1
            for i, d in enumerate(rsa.descriptions, 1):
                pin = d.pinned_field.name
                w.writerow([ad_id, "DESCRIPTION", i, d.text,
                            "" if pin in ("UNSPECIFIED", "UNKNOWN") else pin])
                n_lines += 1

    missing = [a for a in ad_ids if a not in seen]
    print("[DONE] %d ad(s), %d slot rows -> %s" % (len(seen), n_lines, out))
    print("       slot order is AS-LOADED (API list order) -- the order the "
          "Editor's records hold.")
    if missing:
        print("[CHECK] %d ad_id(s) not returned (removed ads?): %s"
              % (len(missing), missing[:10]))


if __name__ == "__main__":
    main()
