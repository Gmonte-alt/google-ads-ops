# file name:
# version:
# Notes: only run this script if you need to delete the existing table. Look into versioning instead

import os
import sqlite3
import pandas as pd
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
from datetime import datetime, timedelta

# Set the path to the Google Ads API configuration file
os.environ["GOOGLE_ADS_CONFIGURATION_FILE_PATH"] = "authentication/google-ads.yaml"

# Initialize the Google Ads client
client = GoogleAdsClient.load_from_storage()

# Function to get clicks, impressions, and spend data for a given customer ID and date range
def get_clicks_impressions_spend_data(client, customer_id, start_date, end_date):
    ga_service = client.get_service("GoogleAdsService", version="v16")
    
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
            metrics.search_impression_share,
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
                    "customer_id": customer_id,  # Add customer ID
                    "Date": row.segments.date,
                    "Campaign_ID": row.campaign.id,
                    "Campaign_Name": row.campaign.name,
                    "Device": row.segments.device.name,
                    "Network": row.segments.ad_network_type.name,
                    "Clicks": row.metrics.clicks,
                    "Impressions": row.metrics.impressions,
                    "Cost": row.metrics.cost_micros / 1e6,  # Convert micros to currency
                    "absolute_top_impression_share": row.metrics.search_absolute_top_impression_share,
                    "impression_share": row.metrics.search_impression_share,
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

    # Convert dataset to DataFrame
    clicks_impressions_spend_df = pd.DataFrame(clicks_impressions_spend_data)
    
    return clicks_impressions_spend_df

# Replace with your actual customer IDs
CUSTOMER_IDS = ['7554573980', '7731032510']

# Initialize an empty DataFrame to hold the data for all customer IDs
all_data = pd.DataFrame()

# Connect to SQLite database
conn = sqlite3.connect("campaigns.db")
cursor = conn.cursor()

# Check if the table exists
cursor.execute("""
SELECT name FROM sqlite_master WHERE type='table' AND name='google_ads_campaign_performance';
""")
table_exists = cursor.fetchone()

# If the table exists, drop it to handle column changes
if table_exists:
    cursor.execute("DROP TABLE google_ads_campaign_performance")

# Create table with the new schema
cursor.execute("""
CREATE TABLE IF NOT EXISTS google_ads_campaign_performance (
    customer_id TEXT,
    Date TEXT,
    Campaign_ID INTEGER,
    Campaign_Name TEXT,
    Device TEXT,
    Network TEXT,
    Clicks INTEGER,
    Impressions INTEGER,
    Cost REAL,
    absolute_top_impression_share REAL,
    impression_share REAL,
    search_click_share REAL
)
""")

# Get yesterday's date
end_date = (datetime.today() - timedelta(days=1)).strftime('%Y-%m-%d')

# Check if the table exists (you just recreated it, so this can be omitted, but for continuity)
cursor.execute("""
SELECT MAX(Date) FROM google_ads_campaign_performance;
""")
last_date = cursor.fetchone()[0]

# If the last date of data is not yesterday, back-fill the missing dates
if last_date is None or last_date < end_date:
    start_date = (datetime.strptime(last_date, "%Y-%m-%d") + timedelta(days=1)).strftime('%Y-%m-%d') if last_date else "2023-01-01"
else:
    start_date = None

if start_date:
    # Iterate through each customer ID and get the data for the specified date range
    for customer_id in CUSTOMER_IDS:
        data = get_clicks_impressions_spend_data(client, customer_id, start_date, end_date)
        if data is not None:
            all_data = pd.concat([all_data, data], ignore_index=True)

    # Insert data into SQLite database
    all_data.to_sql('google_ads_campaign_performance', conn, if_exists='append', index=False)

    # Commit and close the connection
    conn.commit()

print("Data has been written to google_ads_campaign_performance table in campaigns.db")

# Close the connection
conn.close()
