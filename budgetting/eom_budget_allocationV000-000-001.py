# file name: budgetting/eom_budget_allocation.py
# version: V000-000-001
# notes: builds on V000-000-000 and properly groups channel & campaign groups for TNH

import sqlite3
import pandas as pd
from datetime import datetime, timedelta

# Define the SQLite database file path
database_path = 'campaigns.db'
input_csv_file_path = 'ga4-reports/output/ga4_combined_data.csv'
csv_file_path = 'budgetting/output/EOM_Budget_Output.csv'

# Connect to the SQLite databases
conn_ga4 = sqlite3.connect("ga4_data.db")
conn_campaigns = sqlite3.connect("campaigns.db")

# Load the experiment_campaigns_fact table
query_experiment_campaigns = "SELECT campaign_id FROM experiment_campaigns_fact"
df_experiment_campaigns = pd.read_sql_query(query_experiment_campaigns, conn_campaigns)

# Load the ga4_channel_groups_cleaned table for channel grouping
query_ga4_channel_groups = "SELECT * FROM ga4_channel_groups_cleaned"
df_ga4_channel_groups_cleaned = pd.read_sql_query(query_ga4_channel_groups, conn_ga4)

# Query for channel group assignment
query_channel_groups = """
SELECT Source, Medium, Campaign, new_channel_group AS Channel_Group
FROM ga4_channel_groups_cleaned
"""
df_channel_groups = pd.read_sql_query(query_channel_groups, conn_ga4)

# Function to query campaign details from the SQLite database
def query_campaign_details():
    try:
        with sqlite3.connect(database_path) as conn:
            query = "SELECT * FROM campaign_details"
            df = pd.read_sql_query(query, conn)
            df['Source'] = 'google'
            df['Medium'] = 'cpc'
            df['Campaign'] = df['campaign_name']
            df['campaign_id'] = df['campaign_id'].astype(str)
            df_experiment_campaigns['campaign_id'] = df_experiment_campaigns['campaign_id'].astype(str)

            # Filter out experiment campaigns
            df = df[~df['campaign_id'].isin(df_experiment_campaigns['campaign_id'])]

            # Merge with channel group data
            df = df.merge(df_channel_groups, on=['Source', 'Medium', 'Campaign'], how='left')

            # Initialize missing Channel_Group values
            df['Channel_Group'].fillna('Undefined/Other', inplace=True)

    except sqlite3.Error as e:
        print(f"Error while connecting to database: {e}")
    
    return df

# Assign temporary campaign groups based on unmatched campaigns
def assign_temp_groups(row):
    if row['Channel_Group'] == 'Undefined/Other' and row['Campaign_Group'] == 'Undefined/Other':
        if row['Campaign'] in df_temp_campaigns['Campaign'].values:
            if 'tnh - search - brand' in row['Campaign'].lower():
                row['Channel_Group'] = 'SEM Brand'
                return 'Google Search - TNH Brand'
            else:
                row['Channel_Group'] = 'SEM Non-Brand'
                return 'Google Search - TNH Non-Brand'
    return row['Campaign_Group']

# Update the Channel Group based on the Campaign Group
def update_channel_group(row):
    if row['Channel_Group'] == 'Undefined/Other' and row['Campaign_Group'] in ['Google Search - TNH Brand', 'Google Search - TNH Non-Brand']:
        if 'tnh brand' in row['Campaign_Group'].lower():
            return 'SEM Brand'
        else:
            return 'SEM Non-Brand'
    return row['Channel_Group']

# Function to process the input CSV and aggregate metrics based on 'Campaign_Group'
def process_input_csv(df_campaign_details):
    # Read the input CSV file
    df_input = pd.read_csv(input_csv_file_path)
    
    # Convert the 'Date' column from the string format 'YYYYMMDD' to a proper datetime format
    df_input['Date'] = pd.to_datetime(df_input['Date'], format='%Y%m%d')

    # Define the time frames for aggregation
    time_frames = {'Last 30 Days': 30}
    
    # Define the metrics to aggregate
    metrics = ['FF_Lead_Event_Count', 'FF_Purchase_Event_Count', 'FF_BRSubmit_Event_Count', 
               'FF_DMSubmit_Event_Count', 'FF_PhoneGet_Event_Count', 'Clicks', 'Impressions', 'Cost']

    # Initialize the result DataFrame
    df_result = df_campaign_details.copy()
    df_time_frames = []

    for time_frame, days in time_frames.items():
        end_date = df_input['Date'].max()
        start_date = end_date - timedelta(days=days)
        
        # Filter the input DataFrame for the current time frame
        df_filtered = df_input[(df_input['Date'] >= start_date) & (df_input['Date'] <= end_date)]
        
        # Group by 'Campaign_Group' and aggregate the metrics
        df_agg = df_filtered.groupby('Campaign_Group')[metrics].sum().reset_index()

        # Rename columns for the time frame
        metric_columns = [f'{time_frame}_{col}' for col in metrics]
        df_agg.columns = ['Campaign_Group'] + metric_columns
        
        df_time_frames.append(df_agg)

    # Concatenate all time frames and merge with df_campaign_details based on 'Campaign_Group'
    df_all_time_frames = pd.concat(df_time_frames, axis=1)
    df_result = df_campaign_details.merge(df_all_time_frames, on='Campaign_Group', how='left')    

    return df_result

# Function to assign Campaign Group based on Channel Group and other conditions
def assign_campaign_group(row):
    channel_group = row['Channel_Group']
    source = row['Source'].lower()
    campaign = row['Campaign'].lower()

    # Non-Paid Groups
    if channel_group == 'Direct':
        return 'Direct'
    elif channel_group == 'Organic Search (SEO)':
        return 'Organic Search (SEO)'
    elif channel_group == 'Referral':
        return 'Referral'
    elif channel_group == 'Organic Social':
        return 'Organic Social'
    elif channel_group == 'Organic Video':
        return 'Organic Video'
    elif channel_group == 'Email':
        return 'Email'

    # Paid Groups
    if channel_group == 'SEM Brand':
        if source == 'google':
            if 'broad' in campaign:
                return 'Google Search - Brand Broad'
            elif 'tnh - search - brand' in campaign:
                return 'Google Search - TNH Brand'
            else:
                return 'Google Search - Brand'
        elif source == 'bing':
            if 'broad' in campaign:
                return 'Bing Search - Brand Broad'
            else:
                return 'Bing Search - Brand'
    
    elif channel_group == 'SEM Non-Brand':
        if source == 'google':
            # Handle "TNH - Search - Travel Nurse Housing" first
            if 'tnh - search - travel nurse housing' in campaign:
                return 'Google Search - TNH Non-Brand'
            # Handle other "Search - Travel Nurse Housing" campaigns
            elif 'search - travel nurse housing' in campaign:
                return 'Google Search - Travel Nurse Housing'
            elif 'tnh' in campaign and 'brand' not in campaign:
                return 'Google Search - TNH Non-Brand'
            elif 'housing' in campaign:
                return 'Google Search - Housing'
            elif 'healthcare' in campaign:
                return 'Google Search - Healthcare'
            elif 'travel nurse' in campaign:
                return 'Google Search - Travel Nurse'
            elif 'corporate' in campaign:
                return 'Google Search - Corporate'
            elif 'landlord' in campaign:
                return 'Google Search - Landlord'
        elif source == 'bing':
            return 'Bing Search - Non-Brand (All Campaigns)'
    elif channel_group == 'SEM Non-Brand - Tenant':
        if source == 'google':
            return 'Google Search - Tenants'
        if source == 'bing':
            return 'Bing Search - Tenants'

    return 'Undefined/Other'

# Function to save the processed data to a CSV file
def save_to_csv(df, file_path):
    df.to_csv(file_path, index=False)
    print(f'Data successfully saved to {file_path}')

# Main execution
if __name__ == '__main__':
    # Query campaign details from the database
    df_campaign_details = query_campaign_details()

    # Initialize the 'Campaign_Group' column as 'Undefined/Other'
    df_campaign_details['Campaign_Group'] = 'Undefined/Other'

    # Create a temporary DataFrame for unmatched campaigns
    df_unmatched_campaigns = df_campaign_details[df_campaign_details['Channel_Group'] == 'Undefined/Other']
    df_google_ads_campaigns = pd.DataFrame(df_campaign_details)
    df_temp_campaigns = df_unmatched_campaigns.merge(df_google_ads_campaigns, left_on='Campaign', right_on='Campaign', how='inner')

    # Apply temporary group assignment
    df_campaign_details['Campaign_Group'] = df_campaign_details.apply(assign_temp_groups, axis=1)

    # Update the Channel Group based on the Campaign Group
    df_campaign_details['Channel_Group'] = df_campaign_details.apply(update_channel_group, axis=1)

    # Apply the function to assign the Campaign Group
    df_campaign_details['Campaign_Group'] = df_campaign_details.apply(assign_campaign_group, axis=1)

    # Process the input CSV and aggregate metrics
    df_processed = process_input_csv(df_campaign_details)

    # Save the processed data to a CSV file
    save_to_csv(df_processed, csv_file_path)
