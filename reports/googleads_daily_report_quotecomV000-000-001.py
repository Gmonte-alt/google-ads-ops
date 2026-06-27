# file name: reports/googleads_daily_report_quotecom.py
# version: V000-000-001
# execution method: python reports/googleads_daily_report_quotecom.py
# Changes from V000-000-016:
#   - Removed Furnished Finder-specific conversion logic and GA4 section
#   - Customer ID and credential path now loaded from .env file
#   - API version updated from v16 to v18
#   - Removed version parameter from get_service() calls (not supported in google-ads >= 21.0.0)
#   - Added TODO section for Quote.com conversion actions and summary metrics

import pandas as pd
import os
from dotenv import load_dotenv
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
from datetime import datetime, timedelta
import re
from openpyxl import Workbook
from openpyxl.utils.dataframe import dataframe_to_rows
from openpyxl.styles import Font
import numpy as np

# -------------------------------------------------------------------------
# Load environment variables from .env file
# Your .env file should live in the root of your project:
#   C:\Projects\google-ads-ops\.env
#
# It should contain:
#   GOOGLE_ADS_CONFIGURATION_FILE_PATH=C:\ProgramData\google-ads-credentials\google-ads.yaml
#   GOOGLE_ADS_CUSTOMER_ID=YOUR_10_DIGIT_CUSTOMER_ID   (no dashes)
# -------------------------------------------------------------------------
load_dotenv()

# Set the path to the Google Ads API configuration file from .env
os.environ["GOOGLE_ADS_CONFIGURATION_FILE_PATH"] = os.getenv("GOOGLE_ADS_CONFIGURATION_FILE_PATH")

# Initialize the Google Ads client
client = GoogleAdsClient.load_from_storage()

# Load customer ID from .env — Google Ads expects no dashes, just digits e.g. "1234567890"
# CUSTOMER_ID = os.getenv("GOOGLE_ADS_CUSTOMER_ID")

CUSTOMER_IDS = {
    "auto":   os.getenv("GOOGLE_ADS_CUSTOMER_ID_AUTO"),
    "home":   os.getenv("GOOGLE_ADS_CUSTOMER_ID_HOME"),
    "medicare": os.getenv("GOOGLE_ADS_CUSTOMER_ID_MEDICARE"),
    "term_life": os.getenv("GOOGLE_ADS_CUSTOMER_ID_TERM_LIFE"),
}

OUTPUT_DIR = os.getenv("OUTPUT_DIR", "reports/output")

if not CUSTOMER_IDS:
    raise ValueError("GOOGLE_ADS_CUSTOMER_ID is not set in your .env file.")


# -------------------------------------------------------------------------
# API DATA RETRIEVAL FUNCTIONS
# -------------------------------------------------------------------------

def get_campaign_data(client, customer_id):
    """
    Pulls conversion data segmented by date, campaign, device, network,
    and conversion action for the past 60 weeks.
    Uses all_conversions_by_conversion_date so conversions are counted
    on the date they occurred, not the date the click happened.
    """
    ga_service = client.get_service("GoogleAdsService")

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
            metrics.all_conversions_by_conversion_date,
            metrics.all_conversions_value_by_conversion_date
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
                    "Conversions": row.metrics.all_conversions_by_conversion_date,
                    "Conversion Value": row.metrics.all_conversions_value_by_conversion_date
                })
    except GoogleAdsException as ex:
        print(f"Request failed with status {ex.error.code().name} and includes the following errors:")
        for error in ex.failure.errors:
            print(f"\tError with message {error.message}.")
            if error.location:
                for field_path_element in error.location.field_path_elements:
                    print(f"\t\tOn field: {field_path_element.field_name}")
        return None

    return pd.DataFrame(campaign_data)


def get_conversion_action_data(client, customer_id):
    """
    Pulls the full list of conversion actions defined in the account.
    Used to map conversion action IDs (from campaign data) to human-readable names.
    """
    ga_service = client.get_service("GoogleAdsService")

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

    return pd.DataFrame(conversion_action_data)


def get_clicks_impressions_spend_data(client, customer_id):
    """
    Pulls clicks, impressions, cost, and impression share metrics
    segmented by date, campaign, device, and network for the past 60 weeks.
    Cost is converted from micros (Google's internal unit) to dollars.
    """
    ga_service = client.get_service("GoogleAdsService")

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
            metrics.cost_micros,
            metrics.search_click_share,
            metrics.search_budget_lost_top_impression_share,
            metrics.search_budget_lost_impression_share,
            metrics.search_budget_lost_absolute_top_impression_share,
            metrics.search_absolute_top_impression_share,
            metrics.top_impression_percentage
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
                    "Cost": row.metrics.cost_micros / 1e6,  # Convert micros to dollars
                    "absolute_top_impression_share": row.metrics.search_absolute_top_impression_share,
                    "search_click_share": row.metrics.search_click_share
                })
    except GoogleAdsException as ex:
        print(f"Request failed with status {ex.error.code().name} and includes the following errors:")
        for error in ex.failure.errors:
            print(f"\tError with message {error.message}.")
            if error.location:
                for field_path_element in error.location.field_path_elements:
                    print(f"\t\tOn field: {field_path_element.field_name}")
        return None

    return pd.DataFrame(clicks_impressions_spend_data)


# -------------------------------------------------------------------------
# HELPER FUNCTIONS
# -------------------------------------------------------------------------

def extract_conversion_action_id(conversion_action_str):
    """
    The API returns conversion actions as a resource name string like:
    'customers/1234567890/conversionActions/987654321'
    This function extracts just the numeric ID at the end.
    """
    match = re.search(r'conversionActions/(\d+)', conversion_action_str)
    return match.group(1) if match else None


def safe_divide(numerator, denominator):
    """Divides two values, returning 0 instead of NaN or infinity."""
    with np.errstate(divide='ignore', invalid='ignore'):
        result = np.where(denominator != 0, numerator / denominator, 0)
    return result


def save_to_excel(dataframe, filename, sheet_name='Sheet1'):
    with pd.ExcelWriter(filename, engine='openpyxl') as writer:
        dataframe.to_excel(writer, sheet_name=sheet_name, index=False)


# -------------------------------------------------------------------------
# MAIN EXECUTION
# -------------------------------------------------------------------------

# Testing against Auto Insurance account first
CUSTOMER_ID = CUSTOMER_IDS["auto"]

# Pull all three datasets from the API
campaign_data = get_campaign_data(client, CUSTOMER_ID)
conversion_action_data = get_conversion_action_data(client, CUSTOMER_ID)
clicks_impressions_spend_data = get_clicks_impressions_spend_data(client, CUSTOMER_ID)

if campaign_data is not None and conversion_action_data is not None:
    # Extract the numeric conversion action ID from the resource name string
    campaign_data["Conversion Action ID"] = campaign_data["Conversion Action ID"].apply(extract_conversion_action_id)

    # Ensure both ID columns are the same type before merging
    campaign_data["Conversion Action ID"] = campaign_data["Conversion Action ID"].astype(str)
    conversion_action_data["Conversion Action ID"] = conversion_action_data["Conversion Action ID"].astype(str)

    # Join conversion names onto the campaign data
    merged_data = pd.merge(campaign_data, conversion_action_data, on="Conversion Action ID", how="left")

    # Pivot so each conversion action becomes its own column
    pivot_data = merged_data.pivot_table(
        index=["Date", "Campaign ID", "Device", "Network"],
        columns="Conversion Action Name",
        values=["Conversions", "Conversion Value"],
        aggfunc="sum",
        fill_value=0
    ).reset_index()

    # Flatten the two-level column headers into readable names
    # This turns ('Conversions', 'QW - Submit Lead') into 'Conversions - QW - Submit Lead'
    pivot_data.columns = [
        ' - '.join(col).strip(' - ') if isinstance(col, tuple) else col
        for col in pivot_data.columns
    ]

    pivot_data.columns.name = None  # Remove the multi-index column label

else:
    print("Failed to retrieve campaign or conversion action data. Exiting.")
    exit()

if clicks_impressions_spend_data is not None:
    # Join clicks/impressions/spend onto the conversion pivot table
    final_data = pd.merge(pivot_data, clicks_impressions_spend_data,
                          on=["Date", "Campaign ID", "Device", "Network"], how="right")

    # Exclude Display campaigns — keep Search only
    final_data = final_data[~final_data['Campaign Name'].str.contains('Display', case=False, na=False)]

    # Keep only Search and Search Partners traffic
    final_data = final_data[final_data['Network'].str.contains('SEARCH|SEARCH_PARTNERS', case=False, na=False)]

    # Save raw output for inspection
    save_to_excel(final_data, 'reports/output/final_data.xlsx')
    print("Data successfully saved to 'reports/output/final_data.xlsx'")
    print(f"Shape: {final_data.shape}")
    print(final_data.head())

else:
    print("Failed to retrieve clicks/impressions/spend data.")
    exit()


# -------------------------------------------------------------------------
# TODO: Quote.com Summary Metrics
# -------------------------------------------------------------------------
# The section below is where the performance summary logic will go.
# To rebuild it for Quote.com, you'll need to provide:
#
#   1. Your conversion action names exactly as they appear in Google Ads
#      (run this script first — they'll show up as column headers in final_data)
#
#   2. Whether you have a separate GA4 data export to join in, and what
#      the column names look like in that CSV
#
#   3. Any campaign naming conventions (e.g. how Brand vs Non-Brand
#      campaigns are named, so we can split summaries correctly)
#
# Once you have those, paste them here and we'll rebuild calculate_summary_metrics
# for Quote.com.
# -------------------------------------------------------------------------