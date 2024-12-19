# file name: campaign-experiments/experiment_campaigns_fact_procedure.py
# version number: V000-000-003
# Note: This builds on V000-000-002 to include the TNH account
#  reports/output/final_data.xlsx     

import pandas as pd
import os
import sqlite3
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

# Define your customer IDs
CUSTOMER_IDS = ['7554573980', '7731032510']

# Set the path to the Google Ads API configuration file
os.environ["GOOGLE_ADS_CONFIGURATION_FILE_PATH"] = "authentication/google-ads.yaml"

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

# Define the schema for the campaigns_basic_facts table
create_basic_facts_table_query = """
    DROP TABLE IF EXISTS campaigns_basic_facts;

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

# Define the schema for the temporary table and experiment_campaigns_fact table
create_temporary_table_query = """
    DROP TABLE IF EXISTS temp_experiment_campaigns;

    CREATE TEMPORARY TABLE IF NOT EXISTS temp_experiment_campaigns (
        account_client_id TEXT,
        campaign_id TEXT PRIMARY KEY,
        campaign_name TEXT,
        campaign_base_campaign_id TEXT,
        campaign_start_date TEXT,
        campaign_end_date TEXT,
        status TEXT,
        experiment_type TEXT,
        test_name TEXT,
        campaign_base_campaign_name TEXT
    );
    """

create_experiment_facts_table_query = """
    CREATE TABLE IF NOT EXISTS experiment_campaigns_fact (
        account_client_id TEXT,
        campaign_id TEXT PRIMARY KEY,
        campaign_name TEXT,
        campaign_base_campaign_id TEXT,
        campaign_start_date TEXT,
        campaign_end_date TEXT,
        status TEXT,
        experiment_type TEXT,
        test_name TEXT,
        campaign_base_campaign_name TEXT
    );
    """

# Function to insert campaign data into the campaigns_basic_facts table
def insert_campaign_data(campaigns):
    conn = sqlite3.connect('campaigns.db')
    cursor = conn.cursor()
    
    # Execute the table creation script for campaigns_basic_facts
    cursor.executescript(create_basic_facts_table_query)
    
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

    # Create the temporary table for further processing
    cursor.executescript(create_temporary_table_query)
    
    # Insert campaigns containing "test" in their names directly into the temporary table
    insert_into_temp_query = """
    INSERT INTO temp_experiment_campaigns (
        account_client_id,
        campaign_id,
        campaign_name,
        campaign_base_campaign_id,
        campaign_start_date,
        campaign_end_date,
        status,
        experiment_type,
        test_name,
        campaign_base_campaign_name
    )
    SELECT
        cbf.account_client_id,
        cbf.campaign_id,
        cbf.campaign_name,
        cbf.campaign_base_campaign_id,
        cbf.campaign_start_date,
        cbf.campaign_end_date,
        cbf.status,
        cbf.experiment_type,
        CASE
            WHEN LOWER(cbf.campaign_name) LIKE '%test%' THEN
                SUBSTR(cbf.campaign_name, INSTR(LOWER(cbf.campaign_name), 'test'))
            ELSE NULL
        END AS test_name,
        (SELECT cbf2.campaign_name
         FROM campaigns_basic_facts cbf2
         WHERE cbf2.campaign_id = SUBSTR(cbf.campaign_base_campaign_id, INSTR(cbf.campaign_base_campaign_id, '/campaigns/') + LENGTH('/campaigns/'))
        ) AS campaign_base_campaign_name
    FROM campaigns_basic_facts cbf
    WHERE (cbf.status = 'ENABLED' OR cbf.status = 'PAUSED') AND cbf.experiment_type = 'EXPERIMENT'
    """
    cursor.executescript(insert_into_temp_query)

    # Print rows that will be updated (where test_name is NULL and campaign_base_campaign_name is not NULL)
    print("Rows to be updated with new test_name values:")
    pre_update_query = """
    SELECT * FROM temp_experiment_campaigns
    WHERE test_name IS NULL /*AND campaign_base_campaign_name IS NOT NULL*/
    """
    rows_to_update = cursor.execute(pre_update_query).fetchall()
    for row in rows_to_update:
        print(row)

    # Now handle the experiments whose names do not contain "test"
    update_non_test_campaigns_query = """
    UPDATE temp_experiment_campaigns
    SET test_name = 'test ' || TRIM(REPLACE(REPLACE(campaign_name, campaign_base_campaign_name, ''), '-', ' ')) || ' ' || strftime('%Y%m%d', campaign_start_date)
    WHERE test_name IS NULL AND campaign_base_campaign_name IS NOT NULL
    """

    cursor.executescript(update_non_test_campaigns_query)

    # Print rows that were updated
    print("\nRows after the update with new test_name values:")
    post_update_query = """
    SELECT * FROM temp_experiment_campaigns
    WHERE test_name IS NOT NULL AND campaign_base_campaign_name IS NOT NULL
    """
    updated_rows = cursor.execute(post_update_query).fetchall()
    for row in updated_rows:
        print(row)

    # Insert the final data into the experiment_campaigns_fact table
    cursor.executescript(create_experiment_facts_table_query)
    
    insert_final_query = """
    INSERT OR REPLACE INTO experiment_campaigns_fact (
        account_client_id,
        campaign_id,
        campaign_name,
        campaign_base_campaign_id,
        campaign_start_date,
        campaign_end_date,
        status,
        experiment_type,
        test_name,
        campaign_base_campaign_name
    )
    SELECT
        account_client_id,
        campaign_id,
        campaign_name,
        campaign_base_campaign_id,
        campaign_start_date,
        campaign_end_date,
        status,
        experiment_type,
        test_name,
        campaign_base_campaign_name
    FROM temp_experiment_campaigns
    """
    cursor.executescript(insert_final_query)

    conn.commit()
    conn.close()

# Function to print the experiment_campaigns_fact table
def print_experiment_campaigns_table():
    conn = sqlite3.connect('campaigns.db')
    cursor = conn.cursor()
    
    # Query to select all data from the experiment_campaigns_fact table
    select_query = "SELECT * FROM experiment_campaigns_fact"
    
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
    all_campaigns = []
    for customer_id in CUSTOMER_IDS:
        try:
            campaigns = fetch_campaign_data(client, customer_id)
            all_campaigns.extend(campaigns)
        except GoogleAdsException as ex:
            print(f'Request with ID "{ex.request_id}" failed with status "{ex.error.code().name}" and includes the following errors:')
            for error in ex.failure.errors:
                print(f'\tError with message "{error.message}".')
                if error.location:
                    for field_path_element in error.location.field_path_elements:
                        print(f'\t\tOn field: {field_path_element.field_name}')
        except Exception as e:
            print(f'An error occurred: {e}')
    
    insert_campaign_data(all_campaigns)
    print('Campaign data successfully inserted into the database.')
    
    # Call the function to print the experiment_campaigns_fact table
    print_experiment_campaigns_table()
