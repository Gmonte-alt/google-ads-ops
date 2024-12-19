# file name: query-flow/fetch_search-query_raw_v2.py
# version: V000-000-004
# Notes: This is the next iteration where we include the procedure to build and update Linnking Table

import pandas as pd
import sqlite3
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
from datetime import datetime, timedelta
import os
import logging

# Set up logging
logging.basicConfig(filename="search_term_log.log", level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Set the path to the Google Ads API configuration file
os.environ["GOOGLE_ADS_CONFIGURATION_FILE_PATH"] = "authentication/google-ads.yaml"

# Initialize the Google Ads client
client = GoogleAdsClient.load_from_storage()

# SQLite database and table names
DB_FILE = "search_query_database.db"
SEARCH_TERM_TABLE = "google_ads_search_term_raw"
CONVERSION_TABLE = "google_ads_conversion_data"
NEW_QUERIES_TABLE = "New_Search_Queries"

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
            logging.info(f"Table '{table_name}' does not exist.")
            return None
        # Get the max date
        cursor.execute(f"SELECT MAX(Date) FROM {table_name}")
        max_date = cursor.fetchone()[0]
        print(f"Table max date: '{max_date}'.")
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


def fetch_conversion_data(client, customer_id, start_date, end_date):
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
            metrics.all_conversions,
            segments.conversion_action_name
        FROM
            search_term_view
        WHERE
            segments.date BETWEEN '{start_date}' AND '{end_date}'
    """

    conversion_data = []
    try:
        response = ga_service.search_stream(customer_id=customer_id, query=query)
        for batch in response:
            for row in batch.results:
                conversion_data.append({
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
                    "Conversion Action Name": row.segments.conversion_action_name,
                    "All Conversions": row.metrics.all_conversions,
                })
    except GoogleAdsException as ex:
        handle_google_ads_exception(ex)

    return pd.DataFrame(conversion_data)

def handle_google_ads_exception(ex):
    """
    Handle Google Ads API exceptions.
    """
    logging.error(f"Request failed with status {ex.error.code().name}.")
    for error in ex.failure.errors:
        logging.error(f"Error: {error.message}")
        if error.location:
            for field_path_element in error.location.field_path_elements:
                logging.error(f"Field: {field_path_element.field_name}")
    exit(1)

def create_new_search_queries_table():
    """
    Create the New_Search_Queries table if it doesn't exist.
    """
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS New_Search_Queries (
        query_id INTEGER PRIMARY KEY AUTOINCREMENT,
        search_query TEXT NOT NULL UNIQUE,
        date_added_to_table DATETIME DEFAULT CURRENT_TIMESTAMP, -- When the query was added
        first_date_search_term_raw TEXT, -- First occurrence in google_ads_search_term_raw
        first_date_conversion_data TEXT, -- First occurrence in google_ads_conversion_data
        exists_in_search_term_raw INTEGER DEFAULT 0, -- Flag for existence in google_ads_search_term_raw
        exists_in_conversion_data INTEGER DEFAULT 0 -- Flag for existence in google_ads_conversion_data
    );
    ''')
    conn.commit()
    conn.close()


def export_new_search_queries_to_csv(new_queries):
    """
    Export new search queries to a CSV file with a timestamped name.
    """
    if not new_queries.empty:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        csv_file = f"query-flow/output/new_search_queries_{timestamp}.csv"
        new_queries.to_csv(csv_file, index=False)
        logging.info(f"Exported new search queries to {csv_file}")
    else:
        logging.info("No new search queries to export.")



def save_to_database_with_new_queries_tracking(data, db_file, table_name):
    """
    Save data to SQLite using batch inserts and update the New_Search_Queries table.
    """
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    new_queries = []  # Collect new queries for CSV export

    try:
        # Batch insert data into the raw table
        data.to_sql(table_name, conn, if_exists='append', index=False)
        logging.info(f"Batch data saved to table '{table_name}' in database '{db_file}'.")

        # Process each row to update New_Search_Queries
        for _, row in data.iterrows():
            search_query = row['Search Term']
            query_date = row['Date']

            # Check if the search query already exists in New_Search_Queries
            cursor.execute("SELECT * FROM New_Search_Queries WHERE search_query = ?", (search_query,))
            result = cursor.fetchone()

            if result:
                # Update the appropriate flag and first date
                if table_name == SEARCH_TERM_TABLE and not result[4]:  # exists_in_search_term_raw
                    cursor.execute('''
                    UPDATE New_Search_Queries
                    SET exists_in_search_term_raw = 1,
                        first_date_search_term_raw = CASE
                            WHEN first_date_search_term_raw IS NULL THEN ?
                            ELSE first_date_search_term_raw
                        END
                    WHERE search_query = ?
                    ''', (query_date, search_query))
                elif table_name == CONVERSION_TABLE and not result[5]:  # exists_in_conversion_data
                    cursor.execute('''
                    UPDATE New_Search_Queries
                    SET exists_in_conversion_data = 1,
                        first_date_conversion_data = CASE
                            WHEN first_date_conversion_data IS NULL THEN ?
                            ELSE first_date_conversion_data
                        END
                    WHERE search_query = ?
                    ''', (query_date, search_query))
            else:
                # Insert a new search query
                first_date_column = 'first_date_search_term_raw' if table_name == SEARCH_TERM_TABLE else 'first_date_conversion_data'
                exists_column = 'exists_in_search_term_raw' if table_name == SEARCH_TERM_TABLE else 'exists_in_conversion_data'
                cursor.execute(f'''
                INSERT INTO New_Search_Queries (search_query, date_added_to_table, {exists_column}, {first_date_column})
                VALUES (?, CURRENT_TIMESTAMP, 1, ?)
                ''', (search_query, query_date))
                new_queries.append({"Search Query": search_query, "First Date": query_date, "Table": table_name})

        conn.commit()

    except Exception as e:
        logging.error(f"Error while saving data to table '{table_name}': {e}")
        conn.rollback()
    finally:
        conn.close()

    return pd.DataFrame(new_queries)  # Return new queries for export

def create_search_queries_fact_table():
    """
    Create the Search_Queries_Fact table with separate columns for key conversion actions.
    """
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS Search_Queries_Fact (
        query_id INTEGER PRIMARY KEY AUTOINCREMENT,
        search_query TEXT NOT NULL UNIQUE,
        total_impressions INTEGER DEFAULT 0,
        total_clicks INTEGER DEFAULT 0,
        total_cost REAL DEFAULT 0,
        FF_Lead INTEGER DEFAULT 0,
        FF_Purchase INTEGER DEFAULT 0,
        FF_BRSubmit INTEGER DEFAULT 0,
        FF_DMSubmit INTEGER DEFAULT 0,
        FF_PhoneGet INTEGER DEFAULT 0,
        date_added DATETIME DEFAULT CURRENT_TIMESTAMP,
        date_updated DATETIME DEFAULT CURRENT_TIMESTAMP
    );
    ''')
    conn.commit()
    conn.close()

def update_search_queries_fact_table():
    """
    Aggregate metrics from raw tables and update the Search_Queries_Fact table.
    """
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    try:
        # Aggregate metrics from raw tables
        search_term_query = '''
        SELECT
            LOWER("Search Term") AS search_query,
            SUM(impressions) AS total_impressions,
            SUM(clicks) AS total_clicks,
            SUM(cost) AS total_cost
        FROM google_ads_search_term_raw
        GROUP BY LOWER("Search Term")
        '''
        cursor.execute(search_term_query)
        search_term_aggregates = cursor.fetchall()

        conversion_query = '''
        SELECT
            LOWER("Search Term") AS search_query,
            SUM(CASE WHEN "Conversion Action Name" = "Furnished Finder - GA4 (web) FF Lead" THEN "All Conversions" ELSE 0 END) AS FF_Lead,
            SUM(CASE WHEN "Conversion Action Name" = "Furnished Finder - GA4 (web) FF Purchase" THEN "All Conversions" ELSE 0 END) AS FF_Purchase,
            SUM(CASE WHEN "Conversion Action Name" = "Furnished Finder - GA4 (web) FF-BRSubmit" THEN "All Conversions" ELSE 0 END) AS FF_BRSubmit,
            SUM(CASE WHEN "Conversion Action Name" = "Furnished Finder - GA4 (web) FF-DMSubmit" THEN "All Conversions" ELSE 0 END) AS FF_DMSubmit,
            SUM(CASE WHEN "Conversion Action Name" = "Furnished Finder - GA4 (web) FF-PhoneGet" THEN "All Conversions" ELSE 0 END) AS FF_PhoneGet
        FROM google_ads_conversion_data
        GROUP BY LOWER("Search Term")
        '''
        cursor.execute(conversion_query)
        conversion_aggregates = {row[0]: row[1:] for row in cursor.fetchall()}

        # Update fact table
        for search_query, impressions, clicks, cost in search_term_aggregates:
            conversions = conversion_aggregates.get(search_query, (0, 0, 0, 0, 0))

            cursor.execute('''
            INSERT INTO Search_Queries_Fact (
                search_query, total_impressions, total_clicks, total_cost,
                FF_Lead, FF_Purchase, FF_BRSubmit, FF_DMSubmit, FF_PhoneGet
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(search_query) DO UPDATE SET
                total_impressions = total_impressions + excluded.total_impressions,
                total_clicks = total_clicks + excluded.total_clicks,
                total_cost = total_cost + excluded.total_cost,
                FF_Lead = FF_Lead + excluded.FF_Lead,
                FF_Purchase = FF_Purchase + excluded.FF_Purchase,
                FF_BRSubmit = FF_BRSubmit + excluded.FF_BRSubmit,
                FF_DMSubmit = FF_DMSubmit + excluded.FF_DMSubmit,
                FF_PhoneGet = FF_PhoneGet + excluded.FF_PhoneGet,
                date_updated = CURRENT_TIMESTAMP
            ''', (search_query, impressions, clicks, cost, *conversions))

        conn.commit()
        logging.info("Search_Queries_Fact table updated successfully.")

    except Exception as e:
        logging.error(f"Error updating Search_Queries_Fact table: {e}")
        conn.rollback()
    finally:
        conn.close()

def create_search_query_links_table():
    """
    Create the Search_Query_Links table if it doesn't exist.
    """
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS Search_Query_Links (
        link_id INTEGER PRIMARY KEY AUTOINCREMENT,
        query_id INTEGER NOT NULL,
        raw_table_name TEXT NOT NULL,
        raw_table_id TEXT NOT NULL,
        FOREIGN KEY (query_id) REFERENCES Search_Queries_Fact(query_id)
    );
    ''')
    conn.commit()
    conn.close()

def update_search_query_links():
    """
    Populate the Search_Query_Links table with relationships between fact table queries
    and entries in the raw tables.
    """
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    try:
        # Get all search queries from the fact table
        cursor.execute("SELECT query_id, search_query FROM Search_Queries_Fact")
        search_queries = cursor.fetchall()

        for query_id, search_query in search_queries:
            # Link entries from google_ads_search_term_raw
            cursor.execute('''
            SELECT DISTINCT resource_name
            FROM google_ads_search_term_raw
            WHERE search_query = ?
            ''', (search_query,))
            term_ids = cursor.fetchall()

            for term_id in term_ids:
                cursor.execute('''
                INSERT INTO Search_Query_Links (query_id, raw_table_name, raw_table_id)
                SELECT ?, 'google_ads_search_term_raw', ?
                WHERE NOT EXISTS (
                    SELECT 1 FROM Search_Query_Links
                    WHERE query_id = ? AND raw_table_name = 'google_ads_search_term_raw' AND raw_table_id = ?
                )
                ''', (query_id, term_id[0], query_id, term_id[0]))

            # Link entries from google_ads_conversion_data
            cursor.execute('''
            SELECT DISTINCT resource_name
            FROM google_ads_conversion_data
            WHERE search_query = ?
            ''', (search_query,))
            conversion_ids = cursor.fetchall()

            for conversion_id in conversion_ids:
                cursor.execute('''
                INSERT INTO Search_Query_Links (query_id, raw_table_name, raw_table_id)
                SELECT ?, 'google_ads_conversion_data', ?
                WHERE NOT EXISTS (
                    SELECT 1 FROM Search_Query_Links
                    WHERE query_id = ? AND raw_table_name = 'google_ads_conversion_data' AND raw_table_id = ?
                )
                ''', (query_id, conversion_id[0], query_id, conversion_id[0]))

        conn.commit()
        logging.info("Search_Query_Links table updated successfully.")

    except Exception as e:
        logging.error(f"Error updating Search_Query_Links table: {e}")
        conn.rollback()
    finally:
        conn.close()



CUSTOMER_IDS = ['7554573980']

if __name__ == "__main__":
    # Create the New_Search_Queries table if it doesn't exist
    create_new_search_queries_table()
    # Create the search_queries_fact table if it doesn't exist
    create_search_queries_fact_table()
    # Create the search_query_links_table
    create_search_query_links_table()
    
    all_new_queries = pd.DataFrame()  # Collect all new queries for the day

    for CUSTOMER_ID in CUSTOMER_IDS:
        # Default start date (if the table doesn't exist or is empty)
        default_start_date = (datetime.today() - timedelta(weeks=71)).strftime('%Y-%m-%d')

        # Fetch and save search term performance data
        max_date = get_max_date_from_table(DB_FILE, SEARCH_TERM_TABLE)
        
        # Determine the start date
        start_date = (max_date + timedelta(days=1)).strftime('%Y-%m-%d') if max_date else default_start_date
        
        # Define the end date
        end_date = datetime.today().strftime('%Y-%m-%d')
        
        # Fetch search term performance data
        search_term_performance = fetch_search_term_performance(client, CUSTOMER_ID, start_date, end_date)
        print(search_term_performance.head())
        
        # Save the data to the SQLite database
        if not search_term_performance.empty:
            new_queries = save_to_database_with_new_queries_tracking(search_term_performance, DB_FILE, SEARCH_TERM_TABLE)
            all_new_queries = pd.concat([all_new_queries, new_queries], ignore_index=True)
        else:
            logging.info(f"No new search term performance data for customer ID {CUSTOMER_ID}.")

        # Fetch and save conversion data
        max_date = get_max_date_from_table(DB_FILE, CONVERSION_TABLE)
        start_date = (max_date + timedelta(days=1)).strftime('%Y-%m-%d') if max_date else default_start_date
        
        conversion_data = fetch_conversion_data(client, CUSTOMER_ID, start_date, end_date)
        if not conversion_data.empty:
            new_queries = save_to_database_with_new_queries_tracking(conversion_data, DB_FILE, CONVERSION_TABLE)
            all_new_queries = pd.concat([all_new_queries, new_queries], ignore_index=True)
        else:
            logging.info(f"No new conversion data for customer ID {CUSTOMER_ID}.")
        
    # Update the fact table
    update_search_queries_fact_table()
    
    # Update the linking table
    update_search_query_links()

    # Export the updated New_Search_Queries table to a CSV file
    export_new_search_queries_to_csv(all_new_queries)
