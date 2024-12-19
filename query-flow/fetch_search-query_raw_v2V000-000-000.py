# file name:
# version: V000-000-000

import pandas as pd
import sqlite3
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
from datetime import datetime, timedelta
import os

# Set the path to the Google Ads API configuration file
os.environ["GOOGLE_ADS_CONFIGURATION_FILE_PATH"] = "authentication/google-ads.yaml"

# Initialize the Google Ads client
client = GoogleAdsClient.load_from_storage()

# SQLite database and table names
DB_FILE = "search_query_database.db"
TABLE_NAME = "google_ads_search_term_raw"


def get_max_date_from_table(db_file, table_name):
    """
    Check if a table exists in the SQLite database and find the max date in the table.
    """
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    
    try:
        # Check if the table exists
        cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table_name}'")
        if cursor.fetchone() is None:
            print(f"Table '{table_name}' does not exist. Returning default start date.")
            return None  # Table does not exist
        else:
            # Get the max date from the table
            cursor.execute(f"SELECT MAX(Date) FROM {table_name}")
            max_date = cursor.fetchone()[0]
            return datetime.strptime(max_date, '%Y-%m-%d') if max_date else None
    finally:
        conn.close()


def fetch_search_term_performance(client, customer_id, start_date, end_date):
    """
    Fetch search term performance data from the Google Ads API.
    """
    ga_service = client.get_service("GoogleAdsService", version="v16")

    query = f"""
        SELECT
            search_term_view.resource_name,
            search_term_view.search_term,
            search_term_view.status,
            search_term_view.ad_group,
            segments.ad_network_type,
            segments.device,
            segments.keyword.info.text,
            segments.keyword.info.match_type,
            segments.search_term_match_type,
            segments.date,
            metrics.clicks,
            metrics.impressions,
            metrics.absolute_top_impression_percentage,
            metrics.cost_micros,
            metrics.top_impression_percentage
        FROM
            search_term_view
        WHERE
            segments.date BETWEEN '{start_date}' AND '{end_date}'
    """

    performance_data = []
    try:
        response = ga_service.search_stream(customer_id=customer_id, query=query)
        for batch in response:
            for row in batch.results:
                performance_data.append({
                    "Resource Name": row.search_term_view.resource_name,
                    "Search Term": row.search_term_view.search_term,
                    "Status": row.search_term_view.status.name,
                    "Ad Group": row.search_term_view.ad_group,
                    "Ad Network Type": row.segments.ad_network_type.name,
                    "Date": row.segments.date,
                    "Device": "MOBILE" if row.segments.device.name == "MOBILE" else "NON-MOBILE",
                    "Keyword Text": row.segments.keyword.info.text,
                    "Keyword Match Type": row.segments.keyword.info.match_type.name,
                    "Search Term Match Type": row.segments.search_term_match_type.name,
                    "Clicks": row.metrics.clicks,
                    "Impressions": row.metrics.impressions,
                    "Absolute Top Impression %": row.metrics.absolute_top_impression_percentage,
                    "Top Impression %": row.metrics.top_impression_percentage,
                    "Cost": row.metrics.cost_micros / 1e6  # Convert micros to currency
                })
    except GoogleAdsException as ex:
        handle_google_ads_exception(ex)

    return pd.DataFrame(performance_data)


def save_to_database(data, db_file, table_name):
    """
    Save a DataFrame to an SQLite database table.
    """
    conn = sqlite3.connect(db_file)
    try:
        data.to_sql(table_name, conn, if_exists="append", index=False)
        print(f"Data successfully saved to the '{table_name}' table in {db_file}.")
    finally:
        conn.close()


def handle_google_ads_exception(ex):
    """
    Handle Google Ads API exceptions.
    """
    print(f"Request failed with status {ex.error.code().name}.")
    for error in ex.failure.errors:
        print(f"Error with message: {error.message}")
        if error.location:
            for field_path_element in error.location.field_path_elements:
                print(f"On field: {field_path_element.field_name}")
    exit(1)

CUSTOMER_IDS = ['7554573980']

if __name__ == "__main__":
    # Replace with your actual customer ID
    for CUSTOMER_ID in CUSTOMER_IDS:
        # Default start date (if the table doesn't exist or is empty)
        default_start_date = (datetime.today() - timedelta(weeks=71)).strftime('%Y-%m-%d')

        # Check the max date in the table
        max_date = get_max_date_from_table(DB_FILE, TABLE_NAME)

        # Determine the start date
        if max_date:
            start_date = (max_date + timedelta(days=1)).strftime('%Y-%m-%d')
        else:
            start_date = default_start_date

        # Define the end date
        end_date = datetime.today().strftime('%Y-%m-%d')

        # Fetch search term performance data
        search_term_performance = fetch_search_term_performance(client, CUSTOMER_ID, start_date, end_date)
        print(search_term_performance.head())

        # Save the data to the SQLite database
        if not search_term_performance.empty:
            save_to_database(search_term_performance, DB_FILE, TABLE_NAME)
        else:
            print("No new data to save.")
