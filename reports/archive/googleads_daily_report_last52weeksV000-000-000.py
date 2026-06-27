# file name: reports/googleads_daily_report_last52weeks.py
# version: V000-000-000
# execution method: python googleads_daily_report_last52weeks.py
# Note: this first version of the script successfully grabs conversion data from the campaign table
#       Next version needs to tie the conversion action id from the campaign table to the conversion_action table

import pandas as pd
import os
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
from datetime import datetime, timedelta

# Set the path to the Google Ads API configuration file
os.environ["GOOGLE_ADS_CONFIGURATION_FILE_PATH"] = "authentication/google-ads.yaml"

# Initialize the Google Ads client
client = GoogleAdsClient.load_from_storage()

def get_conversion_data(client, customer_id):
    ga_service = client.get_service("GoogleAdsService", version="v16")
    
    # Calculate date range for the past 52 weeks
    end_date = datetime.today().strftime('%Y-%m-%d')
    start_date = (datetime.today() - timedelta(weeks=52)).strftime('%Y-%m-%d')
    
    # Single query to get the required data
    query = f"""
        SELECT
            segments.date,
            campaign.id,
            campaign.name,
            segments.device,
            segments.ad_network_type,
            segments.conversion_action,
            metrics.all_conversions
        FROM
            campaign
        WHERE
            segments.date BETWEEN '{start_date}' AND '{end_date}'
    """
    
    conversion_data = []
    try:
        response = ga_service.search_stream(customer_id=customer_id, query=query)
        for batch in response:
            for row in batch.results:
                conversion_data.append({
                    "Date": row.segments.date,
                    "Campaign ID": row.campaign.id,
                    "Campaign Name": row.campaign.name,
                    "Device": row.segments.device,
                    "Network": row.segments.ad_network_type,
                    "Conversion Action": row.segments.conversion_action,
                    "Conversions": row.metrics.all_conversions
                })
    except GoogleAdsException as ex:
        print(f"Request failed with status {ex.error.code().name} and includes the following errors:")
        for error in ex.failure.errors:
            print(f"\tError with message {error.message}.")
            if error.location:
                for field_path_element in error.location.field_path_elements:
                    print(f"\t\tOn field: {field_path_element.field_name}")
        return None

    # Convert dataset to DataFrame
    conversion_df = pd.DataFrame(conversion_data)
    
    return conversion_df

def save_to_excel(dataframe, filename):
    dataframe.to_excel(filename, index=False)

# Replace with your actual customer ID
CUSTOMER_ID = '7554573980'

# Get conversion data
data = get_conversion_data(client, CUSTOMER_ID)

if data is not None:
    # Save data to Excel
    save_to_excel(data, 'reports/output/conversion_data.xlsx')
    print("Data successfully saved to 'conversion_data.xlsx'")
else:
    print("Failed to retrieve data.")
