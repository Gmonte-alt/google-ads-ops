#!/usr/bin/env python
"""
query_bcc01_incumbent_urls.py -- ad_assets/diagnostics/ -- READ-ONLY probe
answering the bulk-import thread's questions 2 and 3 from ground truth:

  Q-URL:  final URL, paths, and tracking template for every incumbent ad on
          the pause list (the ads BCC-01 inherits from), pulled live from
          ad_group_ad. Blank tracking template = inherits from campaign/
          account level (leave blank in the Editor build to keep inheriting).
          The summary line answers "do all ad groups share one URL?"
          empirically instead of by assumption.
  Q-NAME: current campaign names for the target campaign ids, live from the
          API -- authoritative as of the run (answers the Tier Four rename
          question factually: whatever prints IS today's name).

Inputs: the two load CSVs from export_bcc01_load_targets.py.
Outputs (ad_assets\\exports\\):
  bcc01_incumbent_urls_<date>.csv
  bcc01_campaign_names_<date>.csv

  python query_bcc01_incumbent_urls.py ^
      --pause-csv "...\\exports\\bcc01_pause_list_2026-07-23.csv" ^
      --targets-csv "...\\exports\\bcc01_target_adgroups_2026-07-23.csv"
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


def gaql(ga, query):
    rows = []
    for batch in ga.search_stream(customer_id=CUSTOMER_ID, query=query):
        rows.extend(batch.results)
    return rows


def main():
    p = argparse.ArgumentParser(description="Pull incumbent URLs/tracking + current campaign names.")
    p.add_argument("--pause-csv", required=True)
    p.add_argument("--targets-csv", required=True)
    args = p.parse_args()
    for f in (args.pause_csv, args.targets_csv):
        if not os.path.isfile(f):
            sys.exit("[ABORT] not found: %s" % f)
    os.makedirs(EXPORT_DIR, exist_ok=True)

    with open(args.pause_csv, newline="", encoding="utf-8") as f:
        pause = list(csv.DictReader(f))
    ad_ids = sorted({int(r["ad_id"]) for r in pause})
    with open(args.targets_csv, newline="", encoding="utf-8") as f:
        camp_ids = sorted({int(r["campaign_id"]) for r in csv.DictReader(f)})

    client = GoogleAdsClient.load_from_storage()
    ga = client.get_service("GoogleAdsService")
    stamp = datetime.date.today().isoformat()

    # ---- Q-URL ----
    rows = gaql(ga, f"""
        SELECT campaign.id, ad_group.id, ad_group_ad.ad.id,
               ad_group_ad.ad.final_urls,
               ad_group_ad.ad.tracking_url_template,
               ad_group_ad.ad.responsive_search_ad.path1,
               ad_group_ad.ad.responsive_search_ad.path2
        FROM ad_group_ad
        WHERE ad_group_ad.ad.id IN ({",".join(str(i) for i in ad_ids)})
    """)
    url_path = os.path.join(EXPORT_DIR, "bcc01_incumbent_urls_%s.csv" % stamp)
    urls, tmpls = set(), set()
    with open(url_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ad_id", "ad_group_id", "campaign_id", "final_url",
                    "path1", "path2", "tracking_url_template"])
        for r in rows:
            url = r.ad_group_ad.ad.final_urls[0] if r.ad_group_ad.ad.final_urls else ""
            tmpl = r.ad_group_ad.ad.tracking_url_template or ""
            urls.add(url)
            tmpls.add((r.campaign.id, tmpl))
            w.writerow([r.ad_group_ad.ad.id, r.ad_group.id, r.campaign.id, url,
                        r.ad_group_ad.ad.responsive_search_ad.path1 or "",
                        r.ad_group_ad.ad.responsive_search_ad.path2 or "", tmpl])
    print("[Q-URL] %d ad(s) -> %s" % (len(rows), url_path))
    print("        distinct final URLs: %d %s"
          % (len(urls), "-- ONE shared URL, question 2 answered: uniform"
             if len(urls) == 1 else "-- URLs VARY, build must inherit per ad group"))
    blank_tmpl = sum(1 for _, t in tmpls if not t)
    print("        ad-level tracking templates: %d distinct (%d campaign/tmpl pairs "
          "blank = inherit upper level)" % (len({t for _, t in tmpls if t}), blank_tmpl))
    if len(rows) != len(ad_ids):
        print("        [CHECK] %d pause ids not returned by the API -- reconcile "
              "before building." % (len(ad_ids) - len(rows)))

    # ---- Q-NAME ----
    rows = gaql(ga, f"""
        SELECT campaign.id, campaign.name, campaign.status
        FROM campaign
        WHERE campaign.id IN ({",".join(str(i) for i in camp_ids)})
    """)
    name_path = os.path.join(EXPORT_DIR, "bcc01_campaign_names_%s.csv" % stamp)
    with open(name_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["campaign_id", "campaign_name_current", "status"])
        for r in rows:
            w.writerow([r.campaign.id, r.campaign.name, r.campaign.status.name])
            print("        %s = %s" % (r.campaign.id, r.campaign.name))
    print("[Q-NAME] %d campaign name(s), live as of %s -> %s"
          % (len(rows), stamp, name_path))
    print("\nDone. Both CSVs go to the bulk-import thread with the copy block.")


if __name__ == "__main__":
    main()
