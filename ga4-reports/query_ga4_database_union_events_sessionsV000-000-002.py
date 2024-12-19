# file name: ga4-reports/query_ga4_database_union_events_sessions.py
# version: V000-000-002
# output: table; csv
# Notes: Integrates data from google ads campaigns.db - second the third data source

import sqlite3
import pandas as pd
from datetime import datetime

# Function to convert date format to match GA4 date format
def convert_date_format(date_str):
    date_obj = datetime.strptime(date_str, '%Y-%m-%d')
    return date_obj.strftime('%Y%m%d')

# Connect to the SQLite databases
conn_ga4 = sqlite3.connect("ga4_data.db")
conn_campaigns = sqlite3.connect("campaigns.db")

# Create DataFrames from both tables in ga4_data.db
query_events = """
SELECT
    Date,
    Source,
    Medium,
    Campaign,
    Device,
    0 AS Sessions,
    0 AS TotalUsers,
    0 AS NewUsers,
    FF_Purchase_Event_Count,
    FF_Lead_Event_Count,
    FF_BRSubmit_Event_Count,
    FF_DMSubmit_Event_Count,
    FF_PhoneGet_Event_Count,
    FF_HRSubmit_Event_Count,
    HR_Submit_Event_New_Traveler_Lead_Count,
    0 AS Impressions,
    0 AS Clicks,
    0 AS Cost
FROM
    ga4_events
"""

query_sessions = """
SELECT
    Date,
    Source,
    Medium,
    Campaign,
    Device,
    Sessions,
    TotalUsers,
    NewUsers,
    0 AS FF_Purchase_Event_Count,
    0 AS FF_Lead_Event_Count,
    0 AS FF_BRSubmit_Event_Count,
    0 AS FF_DMSubmit_Event_Count,
    0 AS FF_PhoneGet_Event_Count,
    0 AS FF_HRSubmit_Event_Count,
    0 AS HR_Submit_Event_New_Traveler_Lead_Count,
    0 AS Impressions,
    0 AS Clicks,
    0 AS Cost
FROM
    ga4_sessions
"""

df_events = pd.read_sql_query(query_events, conn_ga4)
df_sessions = pd.read_sql_query(query_sessions, conn_ga4)

# Combine the two DataFrames using union all
df_combined = pd.concat([df_events, df_sessions])

# Replace NaNs with zeros
df_combined.fillna(0, inplace=True)

# Aggregate the columns for "FF_HRSubmit_Event_Count" and "HR_Submit_Event_New_Traveler_Lead_Count"
df_combined["Total Traveler Actions"] = (
    df_combined["FF_BRSubmit_Event_Count"] +
    df_combined["FF_DMSubmit_Event_Count"] +
    df_combined["FF_PhoneGet_Event_Count"] +
    df_combined["FF_HRSubmit_Event_Count"] +
    df_combined["HR_Submit_Event_New_Traveler_Lead_Count"]
)

# Calculate "Traveler Value" and "Landlord Value"
df_combined["Traveler Value"] = df_combined["Total Traveler Actions"] * 2
df_combined["Landlord Value"] = df_combined["FF_Purchase_Event_Count"] * 240

# Load the experiment_campaigns_fact table for campaign name normalization
query_experiment_campaigns = """
SELECT campaign_name, campaign_base_campaign_name
FROM experiment_campaigns_fact
"""
df_experiment_campaigns = pd.read_sql_query(query_experiment_campaigns, conn_campaigns)

# Create a dictionary for campaign name lookup
campaign_name_lookup = df_experiment_campaigns.set_index('campaign_name')['campaign_base_campaign_name'].to_dict()

# Normalize the "Campaign" column
df_combined['Campaign'] = df_combined['Campaign'].map(campaign_name_lookup).fillna(df_combined['Campaign'])

# Query and process the google_ads_campaign_performance table
query_google_ads = """
SELECT p.*, f.campaign_base_campaign_name
FROM google_ads_campaign_performance p
LEFT JOIN experiment_campaigns_fact f ON p.Campaign_ID = f.campaign_id
"""
df_google_ads = pd.read_sql_query(query_google_ads, conn_campaigns)

# Transform the Google Ads data
df_google_ads['Date'] = df_google_ads['Date'].apply(convert_date_format)
df_google_ads['Campaign_Name'] = df_google_ads.apply(
    lambda row: row['campaign_base_campaign_name'] if pd.notnull(row['campaign_base_campaign_name']) else row['Campaign_Name'],
    axis=1
)
df_google_ads['Source'] = 'google'
df_google_ads['Medium'] = 'cpc'

# Aggregate the data
df_google_ads_agg = df_google_ads.groupby(['Date', 'Source', 'Medium', 'Campaign_Name', 'Device']).agg({
    'Impressions': 'sum',
    'Clicks': 'sum',
    'Cost': 'sum'
}).reset_index()

# Align columns with the GA4 combined data
df_google_ads_agg.rename(columns={'Campaign_Name': 'Campaign'}, inplace=True)
df_google_ads_agg['Sessions'] = 0
df_google_ads_agg['TotalUsers'] = 0
df_google_ads_agg['NewUsers'] = 0
df_google_ads_agg['FF_Purchase_Event_Count'] = 0
df_google_ads_agg['FF_Lead_Event_Count'] = 0
df_google_ads_agg['FF_BRSubmit_Event_Count'] = 0
df_google_ads_agg['FF_DMSubmit_Event_Count'] = 0
df_google_ads_agg['FF_PhoneGet_Event_Count'] = 0
df_google_ads_agg['FF_HRSubmit_Event_Count'] = 0
df_google_ads_agg['HR_Submit_Event_New_Traveler_Lead_Count'] = 0
df_google_ads_agg['Total Traveler Actions'] = 0
df_google_ads_agg['Traveler Value'] = 0
df_google_ads_agg['Landlord Value'] = 0

# Ensure column order matches
columns_order = df_combined.columns
df_google_ads_agg = df_google_ads_agg[columns_order]

# Union the Google Ads data with the GA4 combined data
df_final_combined = pd.concat([df_combined, df_google_ads_agg])

# Write the final combined DataFrame to a new table in the SQLite database
df_final_combined.to_sql("ga4_combined", conn_ga4, if_exists="replace", index=False)

# Define the CSV file path
csv_file_path = "ga4-reports/output/ga4_combined_data.csv"

# Write the final combined DataFrame to a CSV file
df_final_combined.to_csv(csv_file_path, index=False)

# Display a message indicating that the data has been written to the CSV file
print(f"Data has been written to {csv_file_path}")

# Close the database connections
conn_ga4.close()
conn_campaigns.close()
