# python script to report on last 7 days hourly performance
# Python file: reporting/googleads_hourly_report_last7days.py
# Version: V000-000-000
 
# import argparse
from google.ads.googleads.client import GoogleAdsClient
from datetime import datetime, timedelta
import os

os.environ["GOOGLE_ADS_CONFIGURATION_FILE_PATH"] = "authentication/google-ads.yaml" # path/to/google-ads.yaml
# client = GoogleAdsClient.load_from_storage() # this script is called within the main() function

def get_hourly_report(client):
    # Get the GoogleAdsService client.
    google_ads_service = client.get_service('GoogleAdsService')

    # Set the date range for the report (last 7 days).
    end_date = datetime.now()
    start_date = end_date - timedelta(days=7)

    # Define the query to retrieve hourly performance metrics.
    query = f'''
        SELECT
          segments.date,
          segments.hour,
          metrics.impressions,
          metrics.clicks,
          metrics.cost_micros,
          metrics.conversions
        FROM
          ad_group
        WHERE
          segments.date >= '{start_date.strftime("%Y-%m-%d")}'
          AND segments.date <= '{end_date.strftime("%Y-%m-%d")}' '''

    # Issue a search request to retrieve the hourly report data.
    response = google_ads_service.search(
        customer_id='7554573980', query=query)  # Replace INSERT_CUSTOMER_ID_HERE with your actual customer ID

    for row in response:
        hour = f"{row.segments.date} {row.segments.hour}:00"
        impressions = row.metrics.impressions
        clicks = row.metrics.clicks
        cost = row.metrics.cost_micros / 1e6  # Convert micros to currency
        conversions = row.metrics.conversions
        print(f"Hour: {hour}, Impressions: {impressions}, Clicks: {clicks}, Cost: {cost}, Conversions: {conversions}")

def main():
    # Initialize Google Ads API client.
    google_ads_client = GoogleAdsClient.load_from_storage()

    # Fetch hourly report data.
    get_hourly_report(google_ads_client)

if __name__ == '__main__':
    main()
