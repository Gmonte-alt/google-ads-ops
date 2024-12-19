# file name:
# version
# Notes: This is the first version of query flow - goal is to create an MVP of documenting all search queries in the FF Google Ads account

import os
import sqlite3
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException


# Set the path to the Google Ads API configuration file
os.environ["GOOGLE_ADS_CONFIGURATION_FILE_PATH"] = "authentication/google-ads.yaml"

# Initialize the Google Ads client
client = GoogleAdsClient.load_from_storage()

# Define Google Ads Query to pull search queries and metrics
query = """
    SELECT
        search_term_view.search_term,
        metrics.impressions,
        metrics.clicks,
        metrics.cost_micros
    FROM
        search_term_view
    WHERE
        segments.date = '2023-07-01'
"""
# Function to connect to SQLite and create the Search Queries table
def create_database():
    conn = sqlite3.connect('search_query_database.db')
    cursor = conn.cursor()

    # Create table for storing search queries and metrics
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS Search_Queries (
        query_id INTEGER PRIMARY KEY AUTOINCREMENT,
        search_query TEXT NOT NULL,
        impressions INTEGER,
        clicks INTEGER,
        cost REAL,
        conversion_action TEXT,
        conversions INTEGER
       
    );
    ''')

    conn.commit()
    return conn

# Function to check if a search query already exists in the database
def query_exists(cursor, search_query):
    cursor.execute('''
    SELECT query_id FROM Search_Queries WHERE search_query = ? 
    ''', (search_query))
    return cursor.fetchone()

# Function to update metrics of an existing search query
def update_existing_query(cursor, search_query, impressions, clicks, cost, conversions, conversion_action, query_date):
    cursor.execute('''
    UPDATE Search_Queries
    SET impressions = ?, clicks = ?, cost = ?, conversions = ?, conversion_action = ?
    WHERE search_query = ?
    ''', (impressions, clicks, cost, conversions, conversion_action, search_query, query_date))


# Insert new search query with a return of query_id
def insert_new_query(cursor, search_query, impressions, clicks, cost, conversions, conversion_action, query_date):
    cursor.execute('''
    INSERT INTO Search_Queries (search_query, impressions, clicks, cost, conversion_action, conversions)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (search_query, impressions, clicks, cost, conversion_action, conversions, query_date))

    return cursor.lastrowid  # Return the query_id of the newly inserted query

# Insert the new query into the New_Search_Queries table
def log_new_query(cursor, query_id):
    cursor.execute('''
    INSERT INTO New_Search_Queries (query_id)
    VALUES (?)
    ''', (query_id,))

# Function to fetch data from Google Ads API
def fetch_search_query_data(client, customer_id, query, conn):
    ga_service = client.get_service("GoogleAdsService", version="v16")
    cursor = conn.cursor()

    # Issue search query
    search_request = client.get_type("SearchGoogleAdsStreamRequest")
    search_request.customer_id = customer_id  # Replace with your Google Ads customer ID
    search_request.query = query

    stream = ga_service.search_stream(search_request)

    try:
        # Iterate over results and insert or update data in the SQLite database
        for batch in stream:
            for row in batch.results:
                search_query = row.search_term_view.search_term
                impressions = row.metrics.impressions
                clicks = row.metrics.clicks
                cost = row.metrics.cost_micros / 1_000_000  # Convert micros to regular currency
            #     conversions = row.metrics.conversions
            #     conversion_action = row.segments.conversion_action
            #     query_date = row.segments.date

                # Check if the search query already exists for the same date
                if query_exists(cursor, search_query):
                    # Update existing search query metrics
                    update_existing_query(cursor, search_query, impressions, clicks, cost) # , conversions, conversion_action, query_date
                else:
                    # Insert new search query and get its query_id
                    query_id = insert_new_query(cursor, search_query, impressions, clicks, cost) # , conversions, conversion_action, query_date

                    # Log the new query in the New_Search_Queries table
                    # log_new_query(cursor, query_id)
                
                # cursor.execute('''
                # INSERT INTO Search_Queries (search_query, impressions, clicks, cost) 
                # VALUES (?, ?, ?, ?)
                # ''', (search_query, impressions, clicks, cost)) # conversion_action, conversions, query_date

        conn.commit()

    except GoogleAdsException as ex:
        print(f"Request failed with status '{ex.error.code().name}' and includes the following errors:")
        for error in ex.failure.errors:
            print(f"Error: {error.error_code}, Message: {error.message}")
        raise


# Replace with your actual customer IDs
CUSTOMER_IDS = ['7554573980'] # TNH , '7731032510'

if __name__ == "__main__":
    conn = create_database()

    for customer_id in CUSTOMER_IDS:
        # Fetch performance data
        fetch_search_query_data(client, customer_id, query, conn)
        #ad_performance = fetch_ad_performance(client, customer_id)
    
    conn.close()
    print("Data fetched and stored in SQLite database successfully.")

        #if asset_performance:
        #    all_asset_results.extend(asset_performance)
        
        #if ad_performance:
        #    all_ad_results.extend(ad_performance)

    #if all_asset_results:
     #   save_to_csv(all_asset_results, "asset_performance.csv")

    #if all_ad_results:
    #    save_to_csv(all_ad_results, "ad_performance.csv")