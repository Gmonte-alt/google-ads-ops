# file name: reports/googleads_daily_report_last52weeks.py
# version: V000-000-003
# execution method: python googleads_daily_report_last52weeks.py
# Note: V000-000-003 will build on the previous version to combine the outputs from the conversion action data pull & ad performance data
#       

import pandas as pd
import os
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
from datetime import datetime, timedelta
import re

# Set the path to the Google Ads API configuration file
os.environ["GOOGLE_ADS_CONFIGURATION_FILE_PATH"] = "authentication/google-ads.yaml"

# Initialize the Google Ads client
client = GoogleAdsClient.load_from_storage()

def get_campaign_data(client, customer_id):
    ga_service = client.get_service("GoogleAdsService", version="v16")
    
    # Calculate date range for the past 52 weeks
    end_date = datetime.today().strftime('%Y-%m-%d')
    start_date = (datetime.today() - timedelta(weeks=60)).strftime('%Y-%m-%d')
    
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
    
    campaign_data = []
    try:
        response = ga_service.search_stream(customer_id=customer_id, query=query)
        for batch in response:
            for row in batch.results:
                campaign_data.append({
                    "Date": row.segments.date,
                    "Campaign ID": row.campaign.id,
                    "Campaign Name": row.campaign.name,
                    "Device": row.segments.device.name,
                    "Network": row.segments.ad_network_type.name,
                    "Conversion Action ID": row.segments.conversion_action,
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
    campaign_df = pd.DataFrame(campaign_data)
    
    return campaign_df

def get_conversion_action_data(client, customer_id):
    ga_service = client.get_service("GoogleAdsService", version="v16")
    
    query = """
        SELECT
            conversion_action.id,
            conversion_action.name
        FROM
            conversion_action
    """
    
    conversion_action_data = []
    try:
        response = ga_service.search_stream(customer_id=customer_id, query=query)
        for batch in response:
            for row in batch.results:
                conversion_action_data.append({
                    "Conversion Action ID": row.conversion_action.id,
                    "Conversion Action Name": row.conversion_action.name
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
    conversion_action_df = pd.DataFrame(conversion_action_data)
    
    return conversion_action_df

def get_clicks_impressions_spend_data(client, customer_id):
    ga_service = client.get_service("GoogleAdsService", version="v16")
    
    # Calculate date range for the past 52 weeks
    end_date = datetime.today().strftime('%Y-%m-%d')
    start_date = (datetime.today() - timedelta(weeks=60)).strftime('%Y-%m-%d')
    
    query = f"""
        SELECT
            segments.date,
            campaign.id,
            campaign.name,
            segments.device,
            segments.ad_network_type,
            metrics.clicks,
            metrics.impressions,
            metrics.cost_micros
        FROM
            campaign
        WHERE
            segments.date BETWEEN '{start_date}' AND '{end_date}'
    """
    
    clicks_impressions_spend_data = []
    try:
        response = ga_service.search_stream(customer_id=customer_id, query=query)
        for batch in response:
            for row in batch.results:
                clicks_impressions_spend_data.append({
                    "Date": row.segments.date,
                    "Campaign ID": row.campaign.id,
                    "Campaign Name": row.campaign.name,
                    "Device": row.segments.device.name,
                    "Network": row.segments.ad_network_type.name,
                    "Clicks": row.metrics.clicks,
                    "Impressions": row.metrics.impressions,
                    "Cost": row.metrics.cost_micros / 1e6  # Convert micros to currency
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
    clicks_impressions_spend_df = pd.DataFrame(clicks_impressions_spend_data)
    
    return clicks_impressions_spend_df

def extract_conversion_action_id(conversion_action_str):
    match = re.search(r'conversionActions/(\d+)', conversion_action_str)
    return match.group(1) if match else None

def save_to_excel(dataframe, filename):
    dataframe.to_excel(filename, index=False)

# Replace with your actual customer ID
CUSTOMER_ID = '7554573980'

# Get campaign data
campaign_data = get_campaign_data(client, CUSTOMER_ID)

# Get conversion action data
conversion_action_data = get_conversion_action_data(client, CUSTOMER_ID)

# Get clicks, impressions, and spend data
clicks_impressions_spend_data = get_clicks_impressions_spend_data(client, CUSTOMER_ID)

if campaign_data is not None and conversion_action_data is not None:
    # Extract numerical Conversion Action ID from the string format in campaign data
    campaign_data["Conversion Action ID"] = campaign_data["Conversion Action ID"].apply(extract_conversion_action_id)
    
    # Ensure both columns are of the same type (string in this case)
    campaign_data["Conversion Action ID"] = campaign_data["Conversion Action ID"].astype(str)
    conversion_action_data["Conversion Action ID"] = conversion_action_data["Conversion Action ID"].astype(str)
    
    # Merge the two dataframes on Conversion Action ID
    merged_data = pd.merge(campaign_data, conversion_action_data, on="Conversion Action ID", how="left")

    # Pivot the data to have individual columns for each conversion action name
    pivot_data = merged_data.pivot_table(index=["Date", "Campaign ID", "Device", "Network"],
                                         columns="Conversion Action Name",
                                         values="Conversions",
                                         aggfunc="sum",
                                         fill_value=0).reset_index()
    
    # Flatten the column headers
    pivot_data.columns.name = None

else:
    print("Failed to retrieve data.")

if clicks_impressions_spend_data is not None:
    # Merge the pivoted conversion data with the clicks, impressions, and spend data
    final_data = pd.merge(pivot_data, clicks_impressions_spend_data, on=["Date", "Campaign ID", "Device", "Network"], how="right")
    
    # Save final data to Excel
    save_to_excel(final_data, 'reports/output/v000-000-003final_data.xlsx')
    print("Data successfully saved to 'final_data.xlsx'")
else:
    print("Failed to retrieve clicks, impressions, and spend data.")
