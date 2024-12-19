# file name: ad-assets/report_ad_assets_google_rsa.py
# version: V000-000-001
# output:
# Notes: V000-000-001 builds on V000-000-000 and now incorporates a 2nd output file to show the ad group ad id performance

import os
import csv
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

def fetch_responsive_search_ad_performance(client, customer_id):
    ga_service = client.get_service("GoogleAdsService", version="v16")

    # Query to get overall ad performance (clicks, cost, impressions)
    ad_query = """
        SELECT
            campaign.id,
            campaign.name,
            ad_group.id,
            ad_group.name,
            ad_group_ad.ad.id,
            metrics.clicks,
            metrics.impressions,
            metrics.cost_micros
        FROM
            ad_group_ad
        WHERE
            ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD'
        """

    ad_response = ga_service.search_stream(customer_id=customer_id, query=ad_query)

    ad_results = []
    try:
        for batch in ad_response:
            for row in batch.results:
                ad_results.append({
                    'campaign_id': row.campaign.id,
                    'campaign_name': row.campaign.name,
                    'ad_group_id': row.ad_group.id,
                    'ad_group_name': row.ad_group.name,
                    'ad_id': row.ad_group_ad.ad.id,
                    'clicks': row.metrics.clicks,
                    'impressions': row.metrics.impressions,
                    'cost_micros': row.metrics.cost_micros / 1_000_000,  # converting to currency unit
                })

    except GoogleAdsException as ex:
        print(f"Request failed with status {ex.error.code().name} and includes the following errors:")
        for error in ex.failure.errors:
            print(f"\tError: {error.error_code.message}")
            if error.location:
                for field_path_element in error.location.field_path_elements:
                    print(f"\t\tOn field: {field_path_element.field_name}")
        return None

    return ad_results

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
            ad_group_ad.ad.responsive_search_ad.headlines,
            ad_group_ad.ad.responsive_search_ad.descriptions,
            ad_group_ad.ad.responsive_search_ad.path1,
            ad_group_ad.ad.responsive_search_ad.path2,
            ad_group_ad_asset_view.asset,
            metrics.impressions
        FROM
            ad_group_ad_asset_view
        WHERE
            ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD'
        """

    asset_response = ga_service.search_stream(customer_id=customer_id, query=asset_query)

    asset_results = []
    try:
        for batch in asset_response:
            for row in batch.results:
                asset_results.append({
                    'campaign_id': row.campaign.id,
                    'campaign_name': row.campaign.name,
                    'ad_group_id': row.ad_group.id,
                    'ad_group_name': row.ad_group.name,
                    'ad_id': row.ad_group_ad.ad.id,
                    'headlines': ' | '.join([headline.text for headline in row.ad_group_ad.ad.responsive_search_ad.headlines]),
                    'descriptions': ' | '.join([description.text for description in row.ad_group_ad.ad.responsive_search_ad.descriptions]),
                    'asset': row.ad_group_ad_asset_view.asset,
                    'impressions': row.metrics.impressions,
                })

    except GoogleAdsException as ex:
        print(f"Request failed with status {ex.error.code().name} and includes the following errors:")
        for error in ex.failure.errors:
            print(f"\tError: {error.error_code.message}")
            if error.location:
                for field_path_element in error.location.field_path_elements:
                    print(f"\t\tOn field: {field_path_element.field_name}")
        return None

    return asset_results

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
        ad_performance = fetch_responsive_search_ad_performance(client, customer_id)
        asset_performance = fetch_responsive_search_ad_asset_performance(client, customer_id)
        
        if ad_performance:
            all_ad_results.extend(ad_performance)
        
        if asset_performance:
            all_asset_results.extend(asset_performance)

    if all_ad_results:
        save_to_csv(all_ad_results, "ad_performance.csv")

    if all_asset_results:
        save_to_csv(all_asset_results, "asset_performance.csv")
