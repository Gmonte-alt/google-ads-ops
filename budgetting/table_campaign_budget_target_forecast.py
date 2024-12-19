# File name:budgetting/table_campaign_budget_target_forecast.py
# Version: V000-000-000
# Note: 
#


#---------------------------------------------------------------------------------------#
#---------------------------------------------------------------------------------------#
#-----------------------GOOGLE ADS API QUERY CAMPAIGN DATA---------------------#
#---------------------------------------------------------------------------------------#
#---------------------------------------------------------------------------------------#

import sqlite3
import os
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

# Set the path to the Google Ads API configuration file
os.environ["GOOGLE_ADS_CONFIGURATION_FILE_PATH"] = "authentication/google-ads.yaml"
# Replace with your actual customer IDs
CUSTOMER_IDS = ['7554573980', '7731032510']

# Configure your Google Ads API credentials
client = GoogleAdsClient.load_from_storage()

# Define the SQLite database file path
database_path = 'campaigns.db'

# Function to fetch campaign data from Google Ads API
def fetch_campaign_data(client, customer_id):
    ga_service = client.get_service('GoogleAdsService', version='v16')

    query = '''
    SELECT
      campaign.id,
      campaign.name,
      campaign_budget.amount_micros,
      campaign.bidding_strategy_type,
      campaign.target_spend.target_spend_micros,
      campaign.maximize_conversions.target_cpa_micros,
      campaign.bidding_strategy_system_status,
      campaign.bidding_strategy,
      campaign.primary_status,
      campaign_budget.status,
      campaign_budget.has_recommended_budget,
      campaign_budget.recommended_budget_amount_micros,
      campaign_budget.recommended_budget_estimated_change_weekly_clicks,
      campaign_budget.recommended_budget_estimated_change_weekly_cost_micros,
      metrics.search_budget_lost_impression_share
    FROM
      campaign
    WHERE
      campaign.status = 'ENABLED'
    '''

    response = ga_service.search(customer_id=customer_id, query=query)

    campaigns = []
    for row in response:
        campaign = row.campaign
        campaign_budget = row.campaign_budget
        metrics = row.metrics

        campaigns.append({
            "customer_id": customer_id,
            "campaign_id": campaign.id,
            "campaign_name": campaign.name,
            "budget": campaign_budget.amount_micros / 1e6,
            "bid_strategy": campaign.bidding_strategy_type.name,
            "target_spend": campaign.target_spend.target_spend_micros / 1e6 if campaign.target_spend.target_spend_micros else None,
            "target_cpa": campaign.maximize_conversions.target_cpa_micros / 1e6 if campaign.maximize_conversions.target_cpa_micros else None,
            "bidding_strategy_system_status": campaign.bidding_strategy_system_status.name,
            "bidding_strategy": campaign.bidding_strategy,
            "primary_status": campaign.primary_status.name,
            "campaign_budget_status": campaign_budget.status,
            "has_recommended_budget": campaign_budget.has_recommended_budget,
            "recommended_budget_amount": campaign_budget.recommended_budget_amount_micros / 1e6 if campaign_budget.recommended_budget_amount_micros else None,
            "recommended_budget_estimated_change_weekly_clicks": campaign_budget.recommended_budget_estimated_change_weekly_clicks if campaign_budget.recommended_budget_estimated_change_weekly_clicks else None,
            "recommended_budget_estimated_change_weekly_cost": campaign_budget.recommended_budget_estimated_change_weekly_cost_micros / 1e6 if campaign_budget.recommended_budget_estimated_change_weekly_cost_micros else None,
            "search_budget_lost_impression_share": metrics.search_budget_lost_impression_share if metrics.search_budget_lost_impression_share else None
        })

    return campaigns

# Function to insert campaign data into the SQLite database
def insert_campaign_data(campaigns):
    conn = sqlite3.connect(database_path)
    cursor = conn.cursor()

    # Create table if it does not exist
    cursor.executescript('''
    DROP TABLE IF EXISTS campaign_details;
    
    CREATE TABLE IF NOT EXISTS campaign_details (
        customer_id TEXT,
        campaign_id INTEGER,
        campaign_name TEXT,
        budget REAL,
        bid_strategy TEXT,
        target_spend REAL,
        target_cpa REAL,
        bidding_strategy_system_status TEXT,
        bidding_strategy TEXT,
        primary_status TEXT,
        campaign_budget_status INTEGER,
        has_recommended_budget BOOLEAN,
        recommended_budget_amount REAL,
        recommended_budget_estimated_change_weekly_clicks INTEGER,
        recommended_budget_estimated_change_weekly_cost REAL,
        search_budget_lost_impression_share REAL,
        PRIMARY KEY (customer_id, campaign_id)
    )
    ''')

    # Insert data into the table
    for campaign in campaigns:
        cursor.execute('''
        INSERT OR REPLACE INTO campaign_details (
            customer_id, campaign_id, campaign_name, budget, bid_strategy, target_spend, target_cpa, bidding_strategy_system_status, bidding_strategy, primary_status, campaign_budget_status, has_recommended_budget, recommended_budget_amount, recommended_budget_estimated_change_weekly_clicks, recommended_budget_estimated_change_weekly_cost, search_budget_lost_impression_share
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (campaign['customer_id'], campaign['campaign_id'], campaign['campaign_name'], campaign['budget'],
              campaign['bid_strategy'], campaign['target_spend'], campaign['target_cpa'],
              campaign['bidding_strategy_system_status'], campaign['bidding_strategy'],
              campaign['primary_status'], campaign['campaign_budget_status'],
              campaign['has_recommended_budget'], campaign['recommended_budget_amount'],
              campaign['recommended_budget_estimated_change_weekly_clicks'], campaign['recommended_budget_estimated_change_weekly_cost'],
              campaign['search_budget_lost_impression_share']))

    conn.commit()
    conn.close()

# Main execution
if __name__ == '__main__':
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
