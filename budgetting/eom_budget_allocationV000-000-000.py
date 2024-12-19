# file name: 
# version: V000-000-000
# notes: first working version of the eom_budget_allocation python script - does not include TNH campaign group names or bing

import sqlite3
import pandas as pd
from datetime import datetime, timedelta

# Define the SQLite database file path
database_path = 'campaigns.db'
# Define the input CSV file path
input_csv_file_path = 'ga4-reports/output/ga4_combined_data.csv' # 'reports/output/final_combined_data.csv'
csv_file_path = 'budgetting/output/EOM_Budget_Output.csv'

# Connect to the SQLite databases
conn_ga4 = sqlite3.connect("ga4_data.db")
conn_campaigns = sqlite3.connect("campaigns.db")

# Load the experiment_campaigns_fact table for campaign name normalization
query_experiment_campaigns = """
SELECT campaign_id FROM experiment_campaigns_fact
"""

df_experiment_campaigns = pd.read_sql_query(query_experiment_campaigns, conn_campaigns)

# Load the ga4_channel_groups_cleaned table for channel grouping
query_ga4_channel_groups = """
SELECT * FROM ga4_channel_groups_cleaned
"""

df_ga4_channel_groups_cleaned = pd.read_sql_query(query_ga4_channel_groups, conn_ga4)

# Query the ga4_channel_groups_cleaned table for channel group assignment
query_channel_groups = """
SELECT Source, Medium, Campaign, new_channel_group AS Channel_Group
FROM ga4_channel_groups_cleaned
"""
df_channel_groups = pd.read_sql_query(query_channel_groups, conn_ga4)

# Function to query campaign details from the SQLite database
def query_campaign_details():
    try:
        with sqlite3.connect(database_path) as conn:
            # Query to select all data from the campaign_details table
            query = '''SELECT * FROM campaign_details'''
            # Create a pandas DataFrame from the fetched data and assign source & medium
            df = pd.read_sql_query(query, conn)
            df['Source'] = 'google'
            df['Medium'] = 'cpc'
            df['Campaign'] = df['campaign_name'] # change campaign name colum from 'campaign_name' to 'Campaign'
            
            df['campaign_id'] = df['campaign_id'].astype(str)
            df_experiment_campaigns['campaign_id'] = df_experiment_campaigns['campaign_id'].astype(str)

            # Filter out campaign_ids that are experiment campaigns from the campaign_details df
            df = df[~df['campaign_id'].isin(df_experiment_campaigns['campaign_id'])]
            
            # Merge with chanel group data
            # Create Channel_Group column based on source, medium, and campaign values matched and returned from df_ga4_channel_groups_cleaned
            df = df.merge(df_channel_groups, on=['Source', 'Medium', 'Campaign'], how='left')
    except sqlite3.Error as e:
        print(f"Error while connecting to database: {e}")
    
    return df

# Function to process the input CSV and aggregate metrics
def process_input_csv(df_campaign_details):
    # Load the input CSV file into a pandas DataFrame
    df_input = pd.read_csv(input_csv_file_path)
    
    # Ensure the date column is in datetime format (replace 'Date' with the actual date column name if different)
    date_column = 'Date'
    df_input[date_column] = pd.to_datetime(df_input[date_column])
    
    # Define the time frames for aggregation
    time_frames = {
        # 'Yesterday': 1,
        # 'Last 3 Days': 3,
        # 'Last 7 Days': 7,
        # 'Last 14 Days': 14,
        # 'Last 21 Days': 21,
        'Last 30 Days': 30
    }
    
    # Define the metrics to aggregate
    metrics = ['FF_Lead_Event_Count', 'FF_Purchase_Event_Count', 'FF_BRSubmit_Event_Count', 'FF_DMSubmit_Event_Count', 'FF_PhoneGet_Event_Count', 'Clicks', 'Impressions', 'Cost']
    # ratio_metrics = {
    #     'Leads/Clicks': lambda df: df['FF_Lead_Event_Count'] / df['Clicks'],
    #     'Purchase/Leads': lambda df: df['FF_Purchase_Event_Count'] / df['FF_Lead_Event_Count'],
    #     'Avg Cost': lambda df: df['Cost'] / len(df),
    #     'Avg Clicks': lambda df: df['Clicks'] / len(df),
    #     'Avg Impressions': lambda df: df['Impressions'] / len(df),
    #     'Avg Cost/Click': lambda df: df['Cost'] / df['Clicks'],
    #     'Cost/Purchase': lambda df: df['Cost'] / df['FF_Purchase_Event_Count']
    # }
    
    # Initialize the result DataFrame
    df_result = df_campaign_details.copy()

    # Process each time frame
    df_time_frames = []
    for time_frame, days in time_frames.items():
        end_date = df_input[date_column].max()
        start_date = end_date - timedelta(days=days)
        df_filtered = df_input[(df_input[date_column] >= start_date) & (df_input[date_column] <= end_date)]
        
        # Group by 'Campaign' and aggregate the metrics
        df_agg = df_filtered.groupby('Campaign')[metrics].sum().reset_index()

        # Prefix columns and append
        # Apply the prefix only to the metric columns, not the 'Campaign' column
        metric_columns = [f'{time_frame}_{col}' for col in metrics]
        df_agg.columns = ['Campaign'] + metric_columns
        
        df_time_frames.append(df_agg)
        
        # Calculate the ratios
        # for ratio_name, ratio_func in ratio_metrics.items():
        #     df_agg[ratio_name] = ratio_func(df_agg)
        
    # Concatenate all time frames and merge with df_campaign_details
    df_all_time_frames = pd.concat(df_time_frames, axis=1)
    # print(df_all_time_frames.columns)
    df_result = df_campaign_details.merge(df_all_time_frames, on='Campaign', how='left')    
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
            else:
                return 'Google Search - Brand'
        elif source == 'bing':
            if 'broad' in campaign:
                return 'Bing Search - Brand Broad'
            else:
                return 'Bing Search - Brand'
    elif channel_group == 'SEM Non-Brand':
        if source == 'google':
            if campaign == 'search - housing':
                return 'Google Search - Housing'
            elif campaign == 'search - healthcare':
                return 'Google Search - Healthcare'
            elif campaign == 'search - travel nurse' or campaign == 'search - landlord - travel nurse':
                return 'Google Search - Travel Nurse'
            elif campaign == 'search - travel nurse housing':
                return 'Google Search - Travel Nurse Housing'
            elif campaign == 'search - corporate' or campaign ==  'search - landlord - corporate':
                return 'Google Search - Corporate'
            elif campaign == 'search - landlord' or campaign == 'search - landlord - generic':
                return 'Google Search - Landlord'
            elif campaign == 'search - competitor':
                return 'Google Search - Competitor'
            elif campaign == 'search - generics':
                return 'Google Search - Generics'
        elif source == 'travelnursehousing.com':
            return 'Google Search - TNH Non-Brand'
        elif source == 'bing':
            return 'Bing Search - Non-Brand (All Campaigns)'
    elif channel_group == 'SEM Non-Brand - Tenant':
        if source == 'google':
            return 'Google Search - Tenants'
        if source == 'bing':
            return 'Bing Search - Tenants'
    elif channel_group == 'Paid Social + Display':
        if source == 'google':
            if 'prospecting' in campaign and 'traveler' in campaign:
                return 'Google Display Prospecting - Traveler'
            elif 'prospecting' in campaign and 'traveler' not in campaign:
                return 'Google Display Prospecting'
            elif 'retargeting' in campaign:
                return 'Google Display Retargeting'
        elif source == 'facebook':
            if 'prospecting' in campaign and 'traveler' in campaign:
                return 'Facebook Display Prospecting - Traveler'
            elif 'prospecting' in campaign and 'traveler' not in campaign:
                return 'Facebook Display Prospecting'
            elif 'retargeting' in campaign:
                return 'Facebook Display Retargeting'
    
    return 'Undefined/Other'

# Function to save the processed data to a CSV file
def save_to_csv(df, file_path):
    df.to_csv(file_path, index=False)
    print(f'Data successfully saved to {file_path}')

# Main execution
if __name__ == '__main__':
    # Query campaign details from the database
    df_campaign_details = query_campaign_details()
    
    # Apply the function to assign the Campaign Group
    df_campaign_details['Campaign_Group'] = df_campaign_details.apply(assign_campaign_group, axis=1)
    
    # Process the input CSV and aggregate metrics
    df_processed = process_input_csv(df_campaign_details)
    
    # Save the processed data to a CSV file
    save_to_csv(df_processed, csv_file_path)