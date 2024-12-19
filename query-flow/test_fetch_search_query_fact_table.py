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
        
        #print(search_term_aggregates)
        
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
        
        print(conversion_aggregates)

        # Update fact table
        for search_query, impressions, clicks, cost in search_term_aggregates:
            conversions = conversion_aggregates.get(search_query, (0, 0, 0, 0, 0))
            print(search_query)
            print(conversions)

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


if __name__ == "__main__":
    print("beginning update on search_queries_fact_table")
    # Update the fact table
    update_search_queries_fact_table()
    print("Update completed...")
