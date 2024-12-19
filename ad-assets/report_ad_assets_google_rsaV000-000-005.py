# file name: ad-assets/report_ad_assets_google_rsa.py
# version: V000-000-005
# output:
# Notes: based on V000-000-004 creates two new columns "messaging_category" and "keyword_based"

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
input_file_path = "ad-assets/input/ad_asset_messaging_category_label.csv"

# Ensure the directory exists
os.makedirs(output_directory, exist_ok=True)

def load_messaging_category(input_file):
    """Load messaging categories from input CSV."""
    messaging_category_map = {}
    with open(input_file, mode='r', encoding='utf-8') as file:
        reader = csv.DictReader(file)
        for row in reader:
            key = (row['asset_text'], row['field_type'])
            messaging_category_map[key] = row['messaging_category']
    return messaging_category_map

def correct_typos(field_type, asset_text):
    """Correct known typos in asset text."""
    if field_type == "HEADLINE" and "Short Time" in asset_text:
        return asset_text.replace("Short Time", "Short Term")
    if field_type == "DESCRIPTION" and asset_text == "We connect Tenants & Landlords to each other directly. No booking fees getting in the way.":
        return "We connect Tenants & Landlords to each other directly. No booking fees getting in the way"
    return asset_text

def fetch_responsive_search_ad_asset_performance(client, customer_id, messaging_category_map):
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
                ad_group_name = row.ad_group.name
                field_type = row.ad_group_ad_asset_view.field_type.name if row.ad_group_ad_asset_view.field_type else 'UNKNOWN'
                asset_text = row.asset.text_asset.text if row.asset.text_asset else ''

                # Correct known typos in the asset_text
                corrected_text = correct_typos(field_type, asset_text)
                
                # Lookup messaging category
                messaging_category = messaging_category_map.get((corrected_text, field_type), "")

                # Track the number of ad groups the asset_text exists within for a unique campaign
                campaign_adgroup_asset_counts[campaign_id][corrected_text].add(ad_group_id)
                
                # Track the unique ad_group_id per campaign
                campaign_adgroup_count[campaign_id].add(ad_group_id)

                # Determine if the keyword-based label is true or false
                keyword_based = "FALSE"
                if "-" in ad_group_name:
                    keyword = ad_group_name.split("-")[-1].strip()
                    if keyword.lower() in corrected_text.lower():
                        keyword_based = "TRUE"

                asset_results.append({
                    'campaign_id': campaign_id,
                    'campaign_name': row.campaign.name,
                    'ad_group_id': ad_group_id,
                    'ad_group_name': ad_group_name,
                    'ad_id': row.ad_group_ad.ad.id,
                    'asset_id': row.ad_group_ad_asset_view.asset,
                    'field_type': field_type,  # e.g., HEADLINE, DESCRIPTION
                    'asset_type': field_type,  # e.g., TEXT, IMAGE, etc.
                    'asset_text': corrected_text,  # The actual text of the asset with typos corrected
                    'messaging_category': messaging_category,  # Retrieved from input file
                    'keyword_based': keyword_based,  # True if ad_group_name keyword exists in asset_text
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
    all_asset_results = []

    # Load the messaging category map from the input file
    messaging_category_map = load_messaging_category(input_file_path)

    for customer_id in CUSTOMER_IDS:
        # Fetch performance data
        asset_performance = fetch_responsive_search_ad_asset_performance(client, customer_id, messaging_category_map)
        
        if asset_performance:
            all_asset_results.extend(asset_performance)

    if all_asset_results:
        save_to_csv(all_asset_results, "asset_performance.csv")