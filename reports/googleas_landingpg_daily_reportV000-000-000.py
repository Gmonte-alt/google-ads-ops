# File name:
# Version:
# Notes:

import pandas as pd
import os
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
from datetime import datetime
from openpyxl import Workbook
from openpyxl.utils.dataframe import dataframe_to_rows
from openpyxl.styles import Font

# Set the path to the Google Ads API configuration file
os.environ["GOOGLE_ADS_CONFIGURATION_FILE_PATH"] = "authentication/google-ads.yaml"

# Initialize the Google Ads client
client = GoogleAdsClient.load_from_storage()

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
            metrics.clicks,
            metrics.impressions,
            metrics.cost_micros,
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
                    "Clicks": row.metrics.clicks,
                    "Impressions": row.metrics.impressions,
                    "Cost": row.metrics.cost_micros / 1e6,  # Convert micros to currency
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

# Get landing page data
landing_page_data = get_landing_page_data(client, CUSTOMER_ID)

if landing_page_data is not None:
    # Save final data to Excel
    save_to_excel(landing_page_data, 'reports/output/landing_page_data.xlsx')
    print("Data successfully saved to 'landing_page_data.xlsx'")
else:
    print("Failed to retrieve data.")
