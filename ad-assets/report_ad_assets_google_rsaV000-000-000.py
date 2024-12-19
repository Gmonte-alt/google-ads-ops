# file name: ad-assets/report_ad_assets_google_rsa.py
# version: V000-000-000
# output:
# Notes: V000-000-000 creates asset level preview of Impressions for each ad group and RSA. One RSA ad id will contain an asset id for each headline & description line.

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

def fetch_responsive_search_ad_asset_performance(client, customer_id):
    ga_service = client.get_service("GoogleAdsService", version="v16")

    query = """
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
            metrics.clicks,
            metrics.impressions,
            metrics.cost_micros
        FROM
            ad_group_ad_asset_view
        WHERE
            ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD'
        """

    response = ga_service.search_stream(customer_id=customer_id, query=query)

    results = []
    try:
        for batch in response:
            for row in batch.results:
                campaign = row.campaign
                ad_group = row.ad_group
                ad = row.ad_group_ad.ad
                asset_view = row.ad_group_ad_asset_view
                metrics = row.metrics
                
                result = {
                    'campaign_id': campaign.id,
                    'campaign_name': campaign.name,
                    'ad_group_id': ad_group.id,
                    'ad_group_name': ad_group.name,
                    'ad_id': ad.id,
                    'headlines': ' | '.join([headline.text for headline in ad.responsive_search_ad.headlines]),
                    'descriptions': ' | '.join([description.text for description in ad.responsive_search_ad.descriptions]),
                    'asset': asset_view.asset,
                    'clicks': metrics.clicks,
                    'impressions': metrics.impressions,
                    'cost_micros': metrics.cost_micros / 1_000_000,  # converting to currency unit
                }
                
                results.append(result)

        return results

    except GoogleAdsException as ex:
        print(f"Request failed with status {ex.error.code().name} and includes the following errors:")
        for error in ex.failure.errors:
            print(f"\tError: {error.error_code.message}")
            if error.location:
                for field_path_element in error.location.field_path_elements:
                    print(f"\t\tOn field: {field_path_element.field_name}")
        return None

def save_to_csv(data, filename="ad-assets/output/asset_performance.csv"):
    """Save the data to a CSV file."""
    if not data:
        print("No data available to save.")
        return

    with open(filename, mode='w', newline='', encoding='utf-8') as file:
        writer = csv.DictWriter(file, fieldnames=data[0].keys())
        writer.writeheader()
        writer.writerows(data)

    print(f"Data successfully saved to {filename}")

if __name__ == "__main__":
    all_results = []
    for customer_id in CUSTOMER_IDS:
        asset_performance = fetch_responsive_search_ad_asset_performance(client, customer_id)
        if asset_performance:
            all_results.extend(asset_performance)
    
    if all_results:
        save_to_csv(all_results, "ad-assets/output/asset_performance.csv")
