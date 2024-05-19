# python script to report on last 7 days hourly performance
# Python file: reporting/googleads_hourly_report_last7days.py
# Version: V000-000-001
# Note: This next version integrates the script to publish into Google Sheets via API
#       This version kicks an error with the Google Sheets. Saving this version and moving onto the next version.
 
# import argparse
from google.ads.googleads.client import GoogleAdsClient
from datetime import datetime, timedelta
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import os

os.environ["GOOGLE_ADS_CONFIGURATION_FILE_PATH"] = "authenticate/google-ads.yaml" # path/to/google-ads.yaml
# client = GoogleAdsClient.load_from_storage() # this script is called within the main() function

# Set the path to the service account JSON key file
SERVICE_ACCOUNT_KEY_FILE = 'authenticate/client_secret_839120695294-73s56ighm372uc9igcue8l00q3qlh3ge.apps.googleusercontent.com.json'

# Set the aname of the Google Sheet and worksheet
SHEET_NAME = 'Google Ads Hourly Pacer' # 'Your Google Sheet Name'
WORKSHEET_NAME = 'raw_data' # 'Your Worksheet Name'

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
        customer_id='5428037747', query=query)  # Replace INSERT_CUSTOMER_ID_HERE with your actual customer ID

    for row in response:
        hour = f"{row.segments.date} {row.segments.hour}:00"
        impressions = row.metrics.impressions
        clicks = row.metrics.clicks
        cost = row.metrics.cost_micros / 1e6  # Convert micros to currency
        conversions = row.metrics.conversions
        print(f"Hour: {hour}, Impressions: {impressions}, Clicks: {clicks}, Cost: {cost}, Conversions: {conversions}")
        
    # Authenticate with Google Sheets using the service account JSON key file
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_KEY_FILE, scope)
    client = gspread.authorize(creds)
    # Open the Google Sheet and worksheet
    sheet = client.open(SHEET_NAME).worksheet(WORKSHEET_NAME)
    
    # Clear the existing data in the worksheet
    sheet.clear()
    
    # WRite the data to the worksheet
    sheet.update('A1', data)

def main():
    # Initialize Google Ads API client.
    google_ads_client = GoogleAdsClient.load_from_storage()

    # Fetch hourly report data.
    get_hourly_report(google_ads_client)

if __name__ == '__main__':
    main()
