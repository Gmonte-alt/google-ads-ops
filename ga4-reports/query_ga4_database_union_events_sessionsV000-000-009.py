# file name: ga4-reports/query_ga4_database_union_events_sessions.py
# version: V000-000-009
# output: table; csv
# Notes: Incorporates impression_population metric

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
conn_supermetrics = sqlite3.connect("supermetrics_data.db")
conn_ff_snowflake = sqlite3.connect("ff_snowflake.db")

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
    0 AS Impression_population,
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
    0 AS Impression_population,
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

# Calculate Impression_population before aggregation
def calculate_impression_population(row):
    impressions = row['Impressions']
    impression_share = row['impression_share']
    
    if impression_share == 0 and impressions > 0:
        # If impression_share is 0 but impressions are greater than 0, use the impressions value
        return impressions
    elif impression_share == 0:
        # If both impression_share and impressions are 0, return 0
        return 0
    else:
        # Calculate Impression_population as Impressions / impression_share
        return impressions / impression_share

# Apply the calculation to the dataframe
df_google_ads['Impression_population'] = df_google_ads.apply(calculate_impression_population, axis=1)

# Aggregate the data
df_google_ads_agg = df_google_ads.groupby(['Date', 'Source', 'Medium', 'Campaign_Name', 'Device']).agg({
    'Impressions': 'sum',
    'Clicks': 'sum',
    'Cost': 'sum',
    'Impression_population': 'sum'  # Now also aggregating the Impression_population
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

# Query and process the bing_ads_data table from supermetrics_data.db
query_bing_ads = """
SELECT
    Date,
    Campaign,
    Impressions,
    Impression_population,
    Clicks,
    Cost
FROM
    bing_ads_data
"""
df_bing_ads = pd.read_sql_query(query_bing_ads, conn_ff_snowflake) # conn_supermetrics

# Transform the Bing Ads data
df_bing_ads['Date'] = df_bing_ads['Date'].apply(convert_date_format)
df_bing_ads['Source'] = 'bing'
df_bing_ads['Medium'] = 'cpc'
df_bing_ads['Device'] = 'desktop'

# Align columns with the GA4 combined data
df_bing_ads['Sessions'] = 0
df_bing_ads['TotalUsers'] = 0
df_bing_ads['NewUsers'] = 0
df_bing_ads['FF_Purchase_Event_Count'] = 0
df_bing_ads['FF_Lead_Event_Count'] = 0
df_bing_ads['FF_BRSubmit_Event_Count'] = 0
df_bing_ads['FF_DMSubmit_Event_Count'] = 0
df_bing_ads['FF_PhoneGet_Event_Count'] = 0
df_bing_ads['FF_HRSubmit_Event_Count'] = 0
df_bing_ads['HR_Submit_Event_New_Traveler_Lead_Count'] = 0
df_bing_ads['Total Traveler Actions'] = 0
df_bing_ads['Traveler Value'] = 0
df_bing_ads['Landlord Value'] = 0

# Ensure column order matches
df_bing_ads = df_bing_ads[columns_order]

# Query and process the facebook_ads_data table from supermetrics_data.db
query_facebook_ads = """
SELECT
    Date,
    Campaign,
    Impressions,
    0 AS Impression_population,
    Clicks,
    Cost
FROM
    facebook_ads_data
"""
df_facebook_ads = pd.read_sql_query(query_facebook_ads, conn_ff_snowflake) #conn_supermetrics

# Transform the Facebook Ads data
df_facebook_ads['Date'] = df_facebook_ads['Date'].apply(convert_date_format)
df_facebook_ads['Source'] = 'facebook'
df_facebook_ads['Medium'] = 'cpc'
df_facebook_ads['Device'] = 'desktop'  # Modify if needed

# Align columns with the GA4 combined data
df_facebook_ads['Sessions'] = 0
df_facebook_ads['TotalUsers'] = 0
df_facebook_ads['NewUsers'] = 0
df_facebook_ads['FF_Purchase_Event_Count'] = 0
df_facebook_ads['FF_Lead_Event_Count'] = 0
df_facebook_ads['FF_BRSubmit_Event_Count'] = 0
df_facebook_ads['FF_DMSubmit_Event_Count'] = 0
df_facebook_ads['FF_PhoneGet_Event_Count'] = 0
df_facebook_ads['FF_HRSubmit_Event_Count'] = 0
df_facebook_ads['HR_Submit_Event_New_Traveler_Lead_Count'] = 0
df_facebook_ads['Total Traveler Actions'] = 0
df_facebook_ads['Traveler Value'] = 0
df_facebook_ads['Landlord Value'] = 0

# Ensure column order matches
df_facebook_ads = df_facebook_ads[columns_order]

# Union the Google Ads, Bing Ads, and Facebook Ads data with the GA4 combined data
df_final_combined = pd.concat([df_combined, df_google_ads_agg, df_bing_ads, df_facebook_ads])

# Query the ga4_channel_groups_cleaned table for channel group assignment
query_channel_groups = """
SELECT Source, Medium, Campaign, new_channel_group AS Channel_Group
FROM ga4_channel_groups_cleaned
"""
df_channel_groups = pd.read_sql_query(query_channel_groups, conn_ga4)

# Merge the final combined DataFrame with the channel groups DataFrame
df_final_combined = df_final_combined.merge(
    df_channel_groups,
    how='left',
    on=['Source', 'Medium', 'Campaign']
)

# Assign "Undefined/Other" to rows with no channel group match
df_final_combined['Channel_Group'] = df_final_combined['Channel_Group'].fillna('Undefined/Other')

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
                return 'Undefined/Other' # Changing from 'Google Search - Healthcare' to 'Undefined/Other'
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
        if source == 'google' and campaign == 'search - competitors - new':
            return 'Google Search - Tenants Competitor'
        if source == 'google' and campaign != 'search - competitors - new':
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

# Apply the function to assign the Campaign Group
df_final_combined['Campaign_Group'] = df_final_combined.apply(assign_campaign_group, axis=1)

# Function to handle remaining Undefined/Other cases
def handle_remaining_campaign_groups(row):
    if row['Channel_Group'] == 'SEM Non-Brand' and row['Campaign_Group'] == 'Undefined/Other':
        if row['Source'].lower() == 'google':
            return 'Google Search - Non-Brand (Other)'
        elif row['Source'].lower() == 'travelnursehousing.com':
            return 'Google Search - TNH Non-Brand'
        elif row['Source'].lower() == 'bing':
            return 'Bing Search - Non-Brand (All Campaigns)'
    elif row['Channel_Group'] == 'Paid Social + Display' and row['Campaign_Group'] == 'Undefined/Other':
        return 'Facebook - Other'
    return row['Campaign_Group']

# Apply the function to handle remaining cases
df_final_combined['Campaign_Group'] = df_final_combined.apply(handle_remaining_campaign_groups, axis=1)

# Write the final combined DataFrame to a temporary table in the SQLite database
df_final_combined.to_sql("temp_ga4_combined", conn_ga4, if_exists="replace", index=False)

# Identify rows from Google Ads data source that are labeled as "Undefined/Other" on both the Channel Group & Campaign Group
unmatched_campaigns_query = """
SELECT DISTINCT Campaign
FROM temp_ga4_combined
WHERE Source = 'google' AND Medium = 'cpc' AND Channel_Group = 'Undefined/Other' AND Campaign_Group = 'Undefined/Other'
"""
df_unmatched_campaigns = pd.read_sql_query(unmatched_campaigns_query, conn_ga4)

# Query the google_ads_campaign_performance table for campaigns related to travelnursehousing.com
google_ads_query = """
SELECT DISTINCT Campaign_Name
FROM google_ads_campaign_performance
WHERE customer_id = '7731032510'
"""
df_google_ads_campaigns = pd.read_sql_query(google_ads_query, conn_campaigns)

# Create a temporary DataFrame for unmatched campaigns
df_temp_campaigns = df_unmatched_campaigns.merge(df_google_ads_campaigns, left_on='Campaign', right_on='Campaign_Name', how='inner')

# Assign Channel Group and Campaign Group based on the temporary table
def assign_temp_groups(row):
    if row['Channel_Group'] == 'Undefined/Other' and row['Campaign_Group'] == 'Undefined/Other':
        if row['Campaign'] in df_temp_campaigns['Campaign_Name'].values:
            if 'TNH - Search - Brand' in row['Campaign']:
                row['Channel_Group'] = 'SEM Brand'
                return 'Google Search - TNH Non-Brand' # Changing from 'Google Search - TNH Brand' to 'Google Search - TNH Non-Brand'
            else:
                row['Channel_Group'] = 'SEM Non-Brand'
                return 'Google Search - TNH Non-Brand'
    return row['Campaign_Group']

df_final_combined['Campaign_Group'] = df_final_combined.apply(assign_temp_groups, axis=1)

# Create a function to update the Channel Group based on the Campaign Group
def update_channel_group(row):
    if row['Channel_Group'] == 'Undefined/Other' and row['Campaign_Group'] in ['Google Search - TNH Brand', 'Google Search - TNH Non-Brand']:
        if 'TNH Brand' in row['Campaign_Group']:
            return 'SEM Non-Brand' # Changing from 'SEM Brand' to 'SEM Non-Brand'
        else:
            return 'SEM Non-Brand'
    return row['Channel_Group']

# Apply the function to update the Channel Group
df_final_combined['Channel_Group'] = df_final_combined.apply(update_channel_group, axis=1)

# Drop the temporary table
cursor = conn_ga4.cursor()
cursor.execute("DROP TABLE IF EXISTS temp_ga4_combined")

# Write the final combined DataFrame to the actual table in the SQLite database
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
conn_ff_snowflake.close() # conn_supermetrics.close()
