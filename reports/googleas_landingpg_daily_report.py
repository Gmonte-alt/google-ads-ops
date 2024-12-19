# File name: reports/googleas_landingpg_daily_report.py
# Version: V000-000-002
# Notes: output file: reports/output/landing_page_data.xlsx

import pandas as pd
import os
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
from datetime import datetime, timedelta
from openpyxl import Workbook
from openpyxl.utils.dataframe import dataframe_to_rows
from openpyxl.styles import Font
import re

# Set the path to the Google Ads API configuration file
os.environ["GOOGLE_ADS_CONFIGURATION_FILE_PATH"] = "authentication/google-ads.yaml"

# Initialize the Google Ads client
client = GoogleAdsClient.load_from_storage()

# Extract conversion action ID function
def extract_conversion_action_id(conversion_action_str):
    match = re.search(r'conversionActions/(\d+)', conversion_action_str)
    return match.group(1) if match else None

# Get all conversion actions
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

# Get landing page data
def get_landing_page_data(client, customer_id):
    ga_service = client.get_service("GoogleAdsService", version="v16")
    
    # Set date range from January 1, 2023 to today
    start_date = '2023-01-01'
    end_date = datetime.today().strftime('%Y-%m-%d')
    
    query = f"""
        SELECT
            segments.date,
            campaign.id,
            campaign.name,
            segments.device,
            segments.ad_network_type,
            segments.conversion_action,
            metrics.all_conversions,
            landing_page_view.resource_name,
            landing_page_view.unexpanded_final_url
        FROM
            landing_page_view
        WHERE
            segments.date BETWEEN '{start_date}' AND '{end_date}'
    """
    
    landing_page_data = []
    try:
        response = ga_service.search_stream(customer_id=customer_id, query=query)
        for batch in response:
            for row in batch.results:
                landing_page_data.append({
                    "Date": row.segments.date,
                    "Campaign ID": row.campaign.id,
                    "Campaign Name": row.campaign.name,
                    "Device": row.segments.device.name,
                    "Network": row.segments.ad_network_type.name,
                    "Conversion Action ID": row.segments.conversion_action,
                    "Conversions": row.metrics.all_conversions,
                    "Landing Page Resource Name": row.landing_page_view.resource_name,
                    "Landing Page URL": row.landing_page_view.unexpanded_final_url
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
    landing_page_df = pd.DataFrame(landing_page_data)
    
    return landing_page_df

# Get campaign performance data
def get_campaign_performance_data(client, customer_id):
    ga_service = client.get_service("GoogleAdsService", version="v16")
    
    # Set date range from January 1, 2023 to today
    start_date = '2023-01-01'
    end_date = datetime.today().strftime('%Y-%m-%d')
    
    query = f"""
        SELECT
            segments.date,
            campaign.id,
            campaign.name,
            segments.device,
            segments.ad_network_type,
            metrics.clicks,
            metrics.impressions,
            metrics.cost_micros,
            landing_page_view.resource_name,
            landing_page_view.unexpanded_final_url
        FROM
            landing_page_view
        WHERE
            segments.date BETWEEN '{start_date}' AND '{end_date}'
    """
    
    campaign_performance_data = []
    try:
        response = ga_service.search_stream(customer_id=customer_id, query=query)
        for batch in response:
            for row in batch.results:
                campaign_performance_data.append({
                    "Date": row.segments.date,
                    "Campaign ID": row.campaign.id,
                    "Campaign Name": row.campaign.name,
                    "Device": row.segments.device.name,
                    "Network": row.segments.ad_network_type.name,
                    "Clicks": row.metrics.clicks,
                    "Impressions": row.metrics.impressions,
                    "Cost": row.metrics.cost_micros / 1e6,  # Convert micros to currency
                    "Landing Page Resource Name": row.landing_page_view.resource_name,
                    "Landing Page URL": row.landing_page_view.unexpanded_final_url
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
    campaign_performance_df = pd.DataFrame(campaign_performance_data)
    
    return campaign_performance_df

def save_to_excel(df, file_path):
    wb = Workbook()
    ws = wb.active

    # Write the DataFrame to the worksheet
    for row in dataframe_to_rows(df, index=False, header=True):
        ws.append(row)

    # Apply some basic styling
    for cell in ws["1:1"]:
        cell.font = Font(bold=True)

    # Save the workbook
    wb.save(file_path)

# Replace with your actual customer ID
CUSTOMER_ID = '7554573980'

# Get all conversion actions
conversion_action_data = get_conversion_action_data(client, CUSTOMER_ID)
print("Conversion Action Data:")
print(conversion_action_data.head())

# Get landing page data
landing_page_data = get_landing_page_data(client, CUSTOMER_ID)
print("Landing Page Data:")
print(landing_page_data.head())

# Get campaign performance data
campaign_performance_data = get_campaign_performance_data(client, CUSTOMER_ID)
print("Campaign Performance Data:")
print(campaign_performance_data.head())

if landing_page_data is not None and conversion_action_data is not None and campaign_performance_data is not None:
    # Ensure both columns are of the same type (string in this case)
    landing_page_data["Conversion Action ID"] = landing_page_data["Conversion Action ID"].apply(extract_conversion_action_id)
    conversion_action_data["Conversion Action ID"] = conversion_action_data["Conversion Action ID"].astype(str)
    
    # Merge the two dataframes on Conversion Action ID
    merged_data = pd.merge(landing_page_data, conversion_action_data, on="Conversion Action ID", how="left")

    # Pivot the data to have individual columns for each conversion action name
    pivot_data = merged_data.pivot_table(index=["Date", "Campaign ID", "Device", "Network", "Landing Page URL"],
                                         columns="Conversion Action Name",
                                         values="Conversions",
                                         aggfunc="sum",
                                         fill_value=0).reset_index()
    
    # Flatten the column headers
    pivot_data.columns.name = None

    print("Pivot Data:")
    print(pivot_data.head())

    # Merge campaign performance data with pivot data
    landingpage_final_df = pd.merge(pivot_data, campaign_performance_data, on=["Date", "Campaign ID", "Device", "Network", "Landing Page URL"], how="left")

    print("Final Merged Data:")
    print(landingpage_final_df.head())

    # Save final data to Excel
    save_to_excel(landingpage_final_df, 'reports/output/landingpage_final_data.xlsx')
    print("Data successfully saved to 'landingpage_final_data.xlsx'")
else:
    print("Failed to retrieve data.")
