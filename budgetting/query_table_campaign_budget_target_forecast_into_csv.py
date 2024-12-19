# file name: budgetting/query_table_campaign_budget_target_forecast_into_csv.py
# version: V000-000-001
# output file:
# input file: reports/output/final_combined_data.csv
# notes:

import sqlite3
import pandas as pd
from datetime import datetime, timedelta

# Define the SQLite database file path
database_path = 'campaigns.db'
# Define the CSV file path
csv_file_path = 'budgetting/output/campaign_details.csv'
# Define the input CSV file path
input_csv_file_path = 'reports/output/final_combined_data.csv'

# Function to query campaign details from the SQLite database
def query_campaign_details():
    conn = sqlite3.connect(database_path)
    cursor = conn.cursor()

    # Query to select all data from the campaign_details table
    query = '''
    SELECT * FROM campaign_details
    '''

    cursor.execute(query)
    rows = cursor.fetchall()

    # Fetch the column names
    column_names = [description[0] for description in cursor.description]

    # Create a pandas DataFrame from the fetched data
    df = pd.DataFrame(rows, columns=column_names)
    conn.close()
    
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
        'Yesterday': 1,
        'Last 3 Days': 3,
        'Last 7 Days': 7,
        'Last 14 Days': 14,
        'Last 21 Days': 21,
        'Last 28 Days': 28,
    }
    
    # Define the metrics to aggregate
    metrics = ['Furnished Finder - GA4 (web) FF Lead', 'Furnished Finder - GA4 (web) FF Purchase', 'Clicks', 'Impressions', 'Cost']
    ratio_metrics = {
        'Leads/Clicks': lambda df: df['Furnished Finder - GA4 (web) FF Lead'] / df['Clicks'],
        'Purchase/Leads': lambda df: df['Furnished Finder - GA4 (web) FF Purchase'] / df['Furnished Finder - GA4 (web) FF Lead'],
        'Avg Cost': lambda df: df['Cost'] / len(df),
        'Avg Clicks': lambda df: df['Clicks'] / len(df),
        'Avg Impressions': lambda df: df['Impressions'] / len(df),
        'Avg Cost/Click': lambda df: df['Cost'] / df['Clicks'],
        'Cost/Purchase': lambda df: df['Cost'] / df['Furnished Finder - GA4 (web) FF Purchase']
    }
    
    # Initialize the result DataFrame
    df_result = df_campaign_details.copy()

    # Process each time frame
    for time_frame, days in time_frames.items():
        end_date = df_input[date_column].max()
        start_date = end_date - timedelta(days=days)
        
        # Filter the input DataFrame for the current time frame
        df_filtered = df_input[(df_input[date_column] >= start_date) & (df_input[date_column] <= end_date)]
        
        # Group by campaign name and aggregate the metrics
        df_agg = df_filtered.groupby('Campaign Name')[metrics].sum().reset_index()
        
        # Calculate the ratios
        for ratio_name, ratio_func in ratio_metrics.items():
            df_agg[ratio_name] = ratio_func(df_agg)
        
        # Prefix the column names with the time frame
        df_agg = df_agg.add_prefix(f'{time_frame}_')
        df_agg = df_agg.rename(columns={f'{time_frame}_Campaign Name': 'campaign_name'})
        
        # Merge the result with the existing result DataFrame
        df_result = df_result.merge(df_agg, on='campaign_name', how='left')
    
    return df_result

# Function to save the processed data to a CSV file
def save_to_csv(df, file_path):
    df.to_csv(file_path, index=False)
    print(f'Data successfully saved to {file_path}')

# Main execution
if __name__ == '__main__':
    # Query campaign details from the database
    df_campaign_details = query_campaign_details()
    
    # Process the input CSV and aggregate metrics
    df_processed = process_input_csv(df_campaign_details)
    
    # Save the processed data to a CSV file
    save_to_csv(df_processed, csv_file_path)
