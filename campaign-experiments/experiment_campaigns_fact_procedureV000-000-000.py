# file name: campaign-experiments/experiment_campaigns_fact_procedure.py
# version number: V000-000-000
# Note: This virst version of the experiment_campaigns_fact_procedure creates grabs all campaign data and inserts the raw values into a "campaigns_basic_fact" table
#       and filters the results on a console print

import pandas as pd
import os
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

#---------------------------------------------------------------------------------------#
#---------------------------------------------------------------------------------------#
#-----------------------GOOGLE ADS API QUERY CAMPAIGN DATA---------------------#
#---------------------------------------------------------------------------------------#
#---------------------------------------------------------------------------------------#

# Set the path to the Google Ads API configuration file
os.environ["GOOGLE_ADS_CONFIGURATION_FILE_PATH"] = "authentication/google-ads.yaml"
# Replace with your actual customer ID
CUSTOMER_ID = '7554573980'

# Initialize the Google Ads client
client = GoogleAdsClient.load_from_storage() 

# Query to get the required campaign details
query = """
    SELECT
        customer.id,
        campaign.id,
        campaign.name,
        campaign.base_campaign,
        campaign.start_date,
        campaign.end_date,
        campaign.status,
        campaign.experiment_type
    FROM
        campaign
    WHERE
        campaign.status != 'REMOVED'
"""

# Fetching data from Google Ads API
def fetch_campaign_data(client, customer_id):
    ga_service = client.get_service("GoogleAdsService", version="v16")
    response = ga_service.search(customer_id=customer_id, query=query)

    campaigns = []
    for row in response:
        campaign = {
            "account_client_id": row.customer.id,
            "campaign_id": row.campaign.id,
            "campaign_name": row.campaign.name,
            "campaign_base_campaign_id": row.campaign.base_campaign,
            "campaign_start_date": row.campaign.start_date,
            "campaign_end_date": row.campaign.end_date,
            "status": row.campaign.status.name,
            "experiment_type": row.campaign.experiment_type.name if row.campaign.experiment_type else None
        }
        campaigns.append(campaign)

    return campaigns




#---------------------------------------------------------------------------------------#
#---------------------------------------------------------------------------------------#
#-------------------------------SQLite Database Integration-----------------------------#
#---------------------------------------------------------------------------------------#
#---------------------------------------------------------------------------------------#


import sqlite3

# Define the schema for the campaigns table
create_table_query = """
DROP TABLE IF EXISTS campaigns;

CREATE TABLE IF NOT EXISTS campaigns_basic_facts (
    account_client_id TEXT,
    campaign_id TEXT PRIMARY KEY,
    campaign_name TEXT,
    campaign_base_campaign_id TEXT,
    campaign_start_date TEXT,
    campaign_end_date TEXT,
    status TEXT,
    experiment_type TEXT
);
"""

# Function to insert campaign data into the table
def insert_campaign_data(campaigns):
    conn = sqlite3.connect('campaigns.db')
    cursor = conn.cursor()
    #cursor.execute(create_table_query)
    
    # Execute the table creation script
    cursor.executescript(create_table_query)
    
    insert_query = """
    INSERT OR REPLACE INTO campaigns_basic_facts (
        account_client_id,
        campaign_id,
        campaign_name,
        campaign_base_campaign_id,
        campaign_start_date,
        campaign_end_date,
        status,
        experiment_type
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """
    # Insert each campaign into the table
    for campaign in campaigns:
        cursor.execute(insert_query, (
            campaign["account_client_id"],
            campaign["campaign_id"],
            campaign["campaign_name"],
            campaign["campaign_base_campaign_id"],
            campaign["campaign_start_date"],
            campaign["campaign_end_date"],
            campaign["status"],
            campaign["experiment_type"]
        ))

    conn.commit()
    conn.close()

# Function to print the campaigns table
def print_campaigns_table():
    conn = sqlite3.connect('campaigns.db')
    cursor = conn.cursor()
    
    # Query to select all data from the campaigns table
    select_query = "SELECT * FROM campaigns_basic_facts WHERE status = 'ENABLED' AND experiment_type = 'EXPERIMENT';"
    
    cursor.execute(select_query)
    rows = cursor.fetchall()
    
    # Fetch the column names
    column_names = [description[0] for description in cursor.description]
    
    # Print the column names
    print(" | ".join(column_names))
    print("-" * 100)
    
    # Print all rows
    for row in rows:
        print(" | ".join(map(str, row)))
    
    conn.close()

# Main execution
if __name__ == "__main__":
    campaigns = fetch_campaign_data(client, CUSTOMER_ID)
    insert_campaign_data(campaigns)
    
    # Call the function to print the campaigns table
    print_campaigns_table()
