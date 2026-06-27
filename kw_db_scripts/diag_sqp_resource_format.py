"""
diag_sqp_resource_format.py
-----------------------------
Prints the raw segments.keyword.ad_group_criterion resource name string
returned by the Google Ads API for search_term_view, so we can confirm
the exact format and fix the parser accordingly.

Run with any enabled campaign that has recent traffic.

Usage:
    python kw_db_scripts/diag_sqp_resource_format.py --campaign-id 23385188622
"""

import os
import argparse
from dotenv import load_dotenv
from google.ads.googleads.client import GoogleAdsClient

load_dotenv()

MCC_ID = os.getenv("GOOGLE_ADS_MCC_ID")

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--campaign-id", required=True)
    p.add_argument("--customer-id", default=None,
                   help="Override customer ID (default: HOME account from .env)")
    return p.parse_args()

def main():
    args = parse_args()
    customer_id = args.customer_id or os.getenv("GOOGLE_ADS_CUSTOMER_ID_HOME")

    client = GoogleAdsClient.load_from_storage()
    client.login_customer_id = MCC_ID
    ga_service = client.get_service("GoogleAdsService")

    query = f"""
        SELECT
            search_term_view.search_term,
            segments.keyword.ad_group_criterion,
            campaign.id,
            segments.date,
            metrics.clicks
        FROM search_term_view
        WHERE
            campaign.id = {args.campaign_id}
            AND segments.date BETWEEN '2026-04-01' AND '2026-04-12'
            AND campaign.advertising_channel_type = 'SEARCH'
            AND metrics.clicks > 0
        LIMIT 10
    """

    print(f"Querying search_term_view for campaign {args.campaign_id}...")
    print(f"Customer ID: {customer_id}\n")

    count = 0
    response = ga_service.search_stream(customer_id=customer_id, query=query)
    for batch in response:
        for row in batch.results:
            resource = row.segments.keyword.ad_group_criterion
            print(f"Raw resource string : {resource!r}")

            # Try parsing both ways
            parts = resource.split("/")
            print(f"  split('/') parts  : {parts}")
            print(f"  len(parts)        : {len(parts)}")
            if len(parts) >= 6:
                print(f"  parts[3] (ag_id?) : {parts[3]!r}")
                print(f"  parts[5] (crit?)  : {parts[5]!r}")
                criterion_from_index = parts[5].split("~")[-1]
                ag_from_index        = parts[3]
                print(f"  → ag_id (index)   : {ag_from_index!r}")
                print(f"  → crit (index)    : {criterion_from_index!r}")
            if "~" in resource:
                tilde_parts = resource.split("~")
                print(f"  tilde parts       : {tilde_parts}")
                print(f"  → crit (tilde)    : {tilde_parts[-1]!r}")
            print()
            count += 1

    if count == 0:
        print("No rows returned — campaign may have no recent traffic or wrong date range.")

if __name__ == "__main__":
    main()
