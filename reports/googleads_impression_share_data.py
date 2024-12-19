# reports/googleads_impression_share_data.py

import os 
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

# Set the path to the Google Ads API configuration file
os.environ["GOOGLE_ADS_CONFIGURATION_FILE_PATH"] = "authentication/google-ads.yaml"
# Replace with your actual customer ID
CUSTOMER_ID = '7554573980'

# Initialize the Google Ads client
client = GoogleAdsClient.load_from_storage() 


def main(client):
    # Replace with your Google Ads customer ID
    customer_id = 'CUSTOMER_ID'

    query = """
        SELECT
            campaign.id,
            campaign.name,
            metrics.auction_insight_search_impression_share,
            metrics.auction_insight_search_outranking_share,
            metrics.auction_insight_search_overlap_rate,
            metrics.auction_insight_search_position_above_rate,
            metrics.auction_insight_search_top_impression_percentage,
            metrics.search_click_share,
            metrics.search_budget_lost_top_impression_share,
            metrics.search_budget_lost_impression_share,
            metrics.search_budget_lost_absolute_top_impression_share,
            metrics.search_absolute_top_impression_share,
            metrics.top_impression_percentage
        FROM
            campaign
        WHERE
            campaign.status = 'ENABLED'
    """

    # Issue a search request
    google_ads_service = client.get_service('GoogleAdsService', version='v16')
    try:
        response = google_ads_service.search(customer_id=customer_id, query=query)
        
        for row in response:
            campaign = row.campaign
            metrics = row.metrics
            print(f"Campaign ID: {campaign.id}")
            print(f"Campaign Name: {campaign.name}")
            print(f"Search Impression Share: {metrics.auction_insight_search_impression_share}")
            print(f"Search Outranking Share: {metrics.auction_insight_search_outranking_share}")
            print(f"Search Overlap Rate: {metrics.auction_insight_search_overlap_rate}")
            print(f"Search Position Above Rate: {metrics.auction_insight_search_position_above_rate}")
            print(f"Search Top Impression Percentage: {metrics.auction_insight_search_top_impression_percentage}")
            print(f"Search Click Share: {metrics.search_click_share}")
            print(f"Search Budget Lost Top Impression Share: {metrics.search_budget_lost_top_impression_share}")
            print(f"Search Budget Lost Impression Share: {metrics.search_budget_lost_impression_share}")
            print(f"Search Budget Lost Absolute Top Impression Share: {metrics.search_budget_lost_absolute_top_impression_share}")
            print(f"Search Absolute Top Impression Share: {metrics.search_absolute_top_impression_share}")
            print(f"Top Impression Percentage: {metrics.top_impression_percentage}")
            print("\n")

    except GoogleAdsException as ex:
        print(f"Request failed with status {ex.error.code().name} and includes the following errors:")
        for error in ex.failure.errors:
            print(f"\tError with message: {error.message}")
            if error.location:
                for field_path_element in error.location.field_path_elements:
                    print(f"\t\tOn field: {field_path_element.field_name}")

if __name__ == "__main__":
    main(client)
