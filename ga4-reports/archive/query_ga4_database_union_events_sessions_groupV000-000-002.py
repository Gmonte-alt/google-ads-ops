# file name:ga4-reports/query_ga4_database_union_events_sessions_group.py
# version: V000-000-002
# output:
# Notes: builds on V000-000-001 to aggregate based on the date dimension and all ratios & % diff

import sqlite3
import pandas as pd
from datetime import datetime, timedelta


# Connect to the SQLite database
conn = sqlite3.connect("ga4_data.db")

# Create a cursor object
cursor = conn.cursor()

# Define a query to select data from the ga4_combined table
query = """
SELECT
    Date,
    Source,
    Medium,
    Campaign,
    Device,
    Sessions,
    TotalUsers,
    NewUsers,
    FF_Purchase_Event_Count,
    FF_Lead_Event_Count,
    FF_BRSubmit_Event_Count,
    FF_DMSubmit_Event_Count,
    FF_PhoneGet_Event_Count,
    FF_HRSubmit_Event_Count,
    HR_Submit_Event_New_Traveler_Lead_Count,
    FF_HRSubmit_Event_Count + HR_Submit_Event_New_Traveler_Lead_Count AS "Housing Requests",
    "Total Traveler Actions",
    "Traveler Value",
    "Landlord Value"
FROM
    ga4_combined
"""

# Execute the query and fetch the results into a Pandas DataFrame
df = pd.read_sql_query(query, conn)

# Define the logic to create the "Campaign Group" column
def get_campaign_group(row):
    medium = row['Medium']
    source = row['Source']
    campaign = row['Campaign']

    if medium == "(none)":
        return "Direct"
    elif medium == "organic":
        return "Organic Search (SEO)"
    elif "email" in medium:
        return "Email"
    elif medium == "referral":
        return "Referral"
    elif medium == "social":
        return "Organic Social"
    elif medium == "youtube":
        return "Organic Video"
    elif medium == "cpc":
        if source == "facebook":
            if "Traveler" in campaign:
                return "Facebook Prospecting - Traveler"
            elif "Prospecting" in campaign:
                return "Facebook Prospecting"
            elif "Retargeting" in campaign:
                return "Facebook Retargeting"
        elif source == "google" and "Display" in campaign:
            if "Traveler" in campaign:
                return "Google Display Prospecting - Traveler"
            elif "Prospecting" in campaign:
                return "Google Display Prospecting"
            elif "Retargeting" in campaign:
                return "Google Display Retargeting"
        elif source == "google" and "Search" in campaign:
            if campaign == "Search -  Brand":
                return "Google Search -  Brand"
            elif campaign == "Search - Brand Broad":
                return "Google Search - Brand Broad"
            elif campaign == "Search - Housing":
                return "Google Search - Housing"
            elif campaign == "Search - Healthcare":
                return "Google Search - Healthcare"
            elif campaign == "Search - Travel Nurse":
                return "Google Search - Travel Nurse"
            elif campaign == "Search - Travel Nurse Housing":
                return "Google Search - Travel Nurse Housing"
            elif campaign == "Search - Corporate":
                return "Google Search - Corporate"
            elif campaign == "Search - Landlord":
                return "Google Search - Landlord"
            elif campaign == "Search - Competitor":
                return "Google Search - Competitor"
            elif campaign == "Search - Tenants":
                return "Google Search - Tenants"
            elif campaign == "Search - Generics":
                return "Google Search - Generics"
            else:
                return "Google Search - Other"
        elif source == "bing" and "Search" in campaign:
            if "Brand" in campaign:
                return "Bing Search - Brand"
            else:
                return "Bing Search - Non Brand (All Campaigns)"
    return "Other"

# Apply the logic to create the "Campaign Group" column
df['Campaign Group'] = df.apply(get_campaign_group, axis=1)

# Define the logic to create the "Channel Group" column
def get_channel_group(row):
    medium = row['Medium']
    campaign = row['Campaign']
    campaign_group = row['Campaign Group']
    
    if campaign_group in [
        "Paid Total", "SEM Brand Total", "SEM Non Brand Total", "Google SEM Total", "Bing SEM Total",
        "Paid Social + Display Total", "Non-Paid Total", "Direct", "Organic Search (SEO)", "Email", 
        "Referral", "Organic Social", "Organic Video", "Other"
    ]:
        return campaign_group
    elif medium == 'cpc':
        if 'Search' in campaign:
            if 'Brand' in campaign:
                return 'SEM Brand'
            elif 'Tenant' in campaign:
                return 'SEM NonBrand - Tenant'
            else:
                return 'SEM NonBrand'
        elif 'Facebook' in campaign_group or 'Display' in campaign_group:
            return 'Paid Social + Display'
    return 'Other'

# Apply the logic to create the "Channel Group" column
df['Channel Group'] = df.apply(get_channel_group, axis=1)

# Define the logic to create the "Total" groupings
total_groups = [
    ('Non-Paid Total', lambda df: df[df['Medium'] != 'cpc']),
    ('Paid Total', lambda df: df[df['Medium'] == 'cpc']),
    ('SEM Brand Total', lambda df: df[(df['Medium'] == 'cpc') & (df['Campaign'].str.contains('Search')) & (df['Campaign'].str.contains('Brand'))]),
    ('SEM Non Brand Total', lambda df: df[(df['Medium'] == 'cpc') & (df['Campaign'].str.contains('Search')) & (~df['Campaign'].str.contains('Brand'))]),
    ('Google SEM Total', lambda df: df[(df['Medium'] == 'cpc') & (df['Source'] == 'google') & (df['Campaign'].str.contains('Search'))]),
    ('Bing SEM Total', lambda df: df[(df['Medium'] == 'cpc') & (df['Source'] == 'bing') & (df['Campaign'].str.contains('Search'))]),
    ('Paid Social + Display Total', lambda df: df[(df['Medium'] == 'cpc') & ((df['Source'] == 'google') & (df['Campaign'].str.contains('Display')) | (df['Source'] == 'facebook'))])
]

# Aggregate the total groups and append them to the DataFrame
total_rows = []
for name, func in total_groups:
    group_df = func(df).groupby(['Date']).sum().reset_index()
    group_df['Campaign Group'] = name
    group_df['Channel Group'] = name
    group_df['Source'] = ''
    group_df['Medium'] = ''
    group_df['Campaign'] = ''
    group_df['Device'] = ''
    total_rows.append(group_df)

totals_df = pd.concat(total_rows, ignore_index=True)
df = pd.concat([df, totals_df], ignore_index=True)

# Define the CSV file path
csv_file_path = "ga4-reports/output/ga4_combined_data_with_totals_and_channel_group.csv"

# Write the DataFrame to a CSV file
df.to_csv(csv_file_path, index=False)

# Display a message indicating that the data has been written to the CSV file
print(f"Data has been written to {csv_file_path}")

# Close the database connection
conn.close()


# -------------------------------------------- #
# -------- Calculate Summary Metrics --------- #
# -------------------------------------------- #

# Sample DataFrame `df` creation for demonstration (replace this with your actual DataFrame loading method)
# df = pd.read_csv('path_to_your_combined_csv_file.csv')

def calculate_iso_week_metrics(df):
    # Convert Date to datetime format
    df['Date'] = pd.to_datetime(df['Date'], format='%Y%m%d')

    # Add ISO year and week columns
    df['ISO_Year'] = df['Date'].dt.isocalendar().year
    df['ISO_Week'] = df['Date'].dt.isocalendar().week

    # Define the target weeks and year
    target_year = 2024
    actual_week = 31
    lw_week = 30

    # Filter for the actual and last week data for the target year
    df_actual = df[(df['ISO_Year'] == target_year) & (df['ISO_Week'] == actual_week)]
    df_lw = df[(df['ISO_Year'] == target_year) & (df['ISO_Week'] == lw_week)]

    # Define the metrics to aggregate
    metrics = [
        'FF_Lead_Event_Count', 'FF_Purchase_Event_Count', 'FF_BRSubmit_Event_Count',
        'FF_DMSubmit_Event_Count', 'FF_PhoneGet_Event_Count', 'Housing Requests',
        'Total Traveler Actions', 'Traveler Value', 'Landlord Value', 'Sessions'
    ]

    # Aggregate the metrics for "Actual" (current week)
    actual_agg = df_actual.groupby(['Channel Group', 'Campaign Group'])[metrics].sum().reset_index()
    actual_agg.columns = ['Channel Group', 'Campaign Group'] + [f'{metric}_Actual' for metric in metrics]

    # Aggregate the metrics for "LW" (last week)
    lw_agg = df_lw.groupby(['Channel Group', 'Campaign Group'])[metrics].sum().reset_index()
    lw_agg.columns = ['Channel Group', 'Campaign Group'] + [f'{metric}_LW' for metric in metrics]

    # Merge the actual and LW dataframes
    summary_df = pd.merge(actual_agg, lw_agg, on=['Channel Group', 'Campaign Group'], how='outer').fillna(0)

    # Calculate Lead Conversion ratios
    summary_df['Lead_Conversion_Actual'] = summary_df['FF_Purchase_Event_Count_Actual'] / summary_df['FF_Lead_Event_Count_Actual'].replace(0, 1)
    summary_df['Lead_Conversion_LW'] = summary_df['FF_Purchase_Event_Count_LW'] / summary_df['FF_Lead_Event_Count_LW'].replace(0, 1)

    # Calculate WoW percentage differences
    for metric in metrics + ['Lead_Conversion']:
        summary_df[f'{metric}_WoW'] = (summary_df[f'{metric}_Actual'] / summary_df[f'{metric}_LW'].replace(0, 1)) - 1

    # Rearrange the columns
    columns_order = []
    for metric in metrics + ['Lead_Conversion']:
        columns_order.append(f'{metric}_Actual')
        columns_order.append(f'{metric}_LW')
        columns_order.append(f'{metric}_WoW')

    summary_df = summary_df[['Channel Group', 'Campaign Group'] + columns_order]

    return summary_df

# Call the function and print the summary metrics
summary_df = calculate_iso_week_metrics(df)
print(summary_df)

# Optionally, save the summary metrics to a CSV file
summary_csv_path = 'ga4-reports/output/ga4_summary_metrics_iso_weeks.csv'
summary_df.to_csv(summary_csv_path, index=False)
print(f"Summary metrics have been written to {summary_csv_path}")

# Close the database connection
conn.close()