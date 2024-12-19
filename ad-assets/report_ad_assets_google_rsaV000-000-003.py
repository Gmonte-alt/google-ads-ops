# file name: ad-assets/report_ad_assets_google_rsa.py
# version: V000-000-003
# output:
# Notes: V000-000-001 builds and removes the 2nd output from V000-000-002 to instead incorporate unique counting of asset_text across ad groups wihtin a campaign

import os
import csv
from collections import defaultdict
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

# Define your customer IDs
CUSTOMER_IDS = ['7554573980', '7731032510']

# Set the path to the Google Ads API configuration file
os.environ["GOOGLE_ADS_CONFIGURATION_FILE_PATH"] = "authentication/google-ads.yaml"

# Initialize the Google Ads client
client = GoogleAdsClient.load_from_storage()

# Specify the output directory
output_directory = "ad-assets/output/"

# Ensure the directory exists
os.makedirs(output_directory, exist_ok=True)

def fetch_responsive_search_ad_asset_performance(client, customer_id):
    ga_service = client.get_service("GoogleAdsService", version="v16")

    # Query to get asset-level performance
    asset_query = """
        SELECT
            campaign.id,
            campaign.name,
            ad_group.id,
            ad_group.name,
            ad_group_ad.ad.id,
            ad_group_ad_asset_view.asset,
            ad_group_ad_asset_view.field_type,
            asset.type,
            asset.text_asset.text,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros
        FROM
            ad_group_ad_asset_view
        WHERE
            ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD'
    """

    try:
        asset_response = ga_service.search_stream(customer_id=customer_id, query=asset_query)
        asset_results = []
        campaign_adgroup_asset_counts = defaultdict(lambda: defaultdict(set))
        campaign_adgroup_count = defaultdict(set)  # Track unique ad_group_id per campaign

        for batch in asset_response:
            for row in batch.results:
                campaign_id = row.campaign.id
                ad_group_id = row.ad_group.id
                asset_text = row.asset.text_asset.text if row.asset.text_asset else ''
                
                # Track the number of ad groups the asset_text exists within for a unique campaign
                campaign_adgroup_asset_counts[campaign_id][asset_text].add(ad_group_id)
                
                # Track the unique ad_group_id per campaign
                campaign_adgroup_count[campaign_id].add(ad_group_id)

                asset_results.append({
                    'campaign_id': campaign_id,
                    'campaign_name': row.campaign.name,
                    'ad_group_id': ad_group_id,
                    'ad_group_name': row.ad_group.name,
                    'ad_id': row.ad_group_ad.ad.id,
                    'asset_id': row.ad_group_ad_asset_view.asset,
                    'field_type': row.ad_group_ad_asset_view.field_type.name,  # e.g., HEADLINE, DESCRIPTION
                    'asset_type': row.asset.type.name if row.asset.type else 'UNKNOWN',  # e.g., TEXT, IMAGE, etc.
                    'asset_text': asset_text,  # The actual text of the asset
                    'impressions': row.metrics.impressions,
                    'clicks': row.metrics.clicks,
                    'cost_micros': row.metrics.cost_micros / 1_000_000,  # converting to currency unit
                })

        # Enhance asset_results with ad group counts and uniqueness labels
        for result in asset_results:
            campaign_id = result['campaign_id']
            asset_text = result['asset_text']
            ad_group_id = result['ad_group_id']

            # Count of ad groups containing the asset_text in the same campaign
            adgroup_count_for_asset = len(campaign_adgroup_asset_counts[campaign_id][asset_text])

            # Total number of unique ad groups in the campaign
            total_adgroups_in_campaign = len(campaign_adgroup_count[campaign_id])

            # Determine if the asset_text is unique to the ad group or not
            uniqueness_label = "unique" if adgroup_count_for_asset == 1 else "multiple"

            # Add the new fields to the result
            result['adgroup_count_for_asset'] = adgroup_count_for_asset
            result['total_adgroups_in_campaign'] = total_adgroups_in_campaign
            result['uniqueness_label'] = uniqueness_label

        return asset_results

    except GoogleAdsException as ex:
        print(f"Request failed with status {ex.error.code().name} and includes the following errors:")
        for error in ex.failure.errors:
            print(f"\tError: {error.error_code.message}")
            if error.location:
                for field_path_element in error.location.field_path_elements:
                    print(f"\t\tOn field: {field_path_element.field_name}")
        return None

def save_to_csv(data, filename):
    """Save the data to a CSV file."""
    if not data:
        print("No data available to save.")
        return

    filepath = os.path.join(output_directory, filename)
    with open(filepath, mode='w', newline='', encoding='utf-8') as file:
        writer = csv.DictWriter(file, fieldnames=data[0].keys())
        writer.writeheader()
        writer.writerows(data)

    print(f"Data successfully saved to {filepath}")

if __name__ == "__main__":
    all_ad_results = []
    all_asset_results = []

    for customer_id in CUSTOMER_IDS:
        # Fetch performance data
        ad_performance = fetch_responsive_search_ad_asset_performance(client, customer_id)
        
        if ad_performance:
            all_asset_results.extend(ad_performance)

    if all_asset_results:
        save_to_csv(all_asset_results, "asset_performance.csv")
