# file name:ga4-reports/query_ga4_database_union_events_sessions_group.py
# version: V000-000-005
# output:
# Notes: builds on V000-000-004 and to calculate all ratio metrics

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
    "Landlord Value",
    Impressions,
    Clicks,
    Cost
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
            if "Brand" in campaign and "Broad" not in campaign:
                return "Google Search - Brand"
            elif "Brand" in campaign and "Broad" in campaign:
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

# Define the order DataFrame
order_df = pd.DataFrame({
    'Channel Group': ['SEM Brand', 'SEM Brand', 'SEM NonBrand', 'SEM NonBrand', 'SEM NonBrand', 'SEM NonBrand', 
                      'SEM NonBrand', 'SEM NonBrand', 'SEM NonBrand', 'SEM NonBrand - Tenant', 'SEM NonBrand', 
                      'SEM NonBrand', 'SEM Brand', 'SEM NonBrand', 'SEM Brand', 'SEM NonBrand', 
                      'Paid Social + Display', 'Paid Social + Display', 'Paid Social + Display', 
                      'Paid Social + Display', 'Paid Social + Display', 'Paid Social + Display', 
                      'Paid Total', 'SEM Brand Total', 'SEM Non Brand Total', 'Google SEM Total', 
                      'Bing SEM Total', 'Non-Paid Total', 'Paid Social + Display Total', 'Direct', 
                      'Organic Search (SEO)', 'Email', 'Referral', 'Organic Social', 'Organic Video', 'Other'],
    'Campaign Group': ['Google Search - Brand', 'Google Search - Brand Broad', 'Google Search - Housing', 
                       'Google Search - Healthcare', 'Google Search - Travel Nurse', 'Google Search - Travel Nurse Housing', 
                       'Google Search - Corporate', 'Google Search - Landlord', 'Google Search - Competitor', 
                       'Google Search - Tenants', 'Google Search - Generics', 'Google Search - Other', 
                       'Google Search - TNH Brand', 'Google Search - TNH Non Brand', 'Bing Search - Brand', 
                       'Bing Search - Non Brand (All Campaigns)', 'Facebook Prospecting', 'Facebook Retargeting', 
                       'Facebook Prospecting - Traveler', 'Google Display Prospecting', 'Google Display Retargeting', 
                       'Google Display Prospecting - Traveler', 'Paid Total', 'SEM Brand Total', 'SEM Non Brand Total', 
                       'Google SEM Total', 'Bing SEM Total', 'Non-Paid Total', 'Paid Social + Display Total', 'Direct', 
                       'Organic Search (SEO)', 'Email', 'Referral', 'Organic Social', 'Organic Video', 'Other'],
    'Order_number': [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 28, 29, 30, 31, 32, 33, 34, 35]
})

# Define the CSV file path for the combined data
csv_file_path_combined = "ga4-reports/output/ga4_combined_data_with_totals_and_channel_group_sorted.csv"

# Write the combined DataFrame to a CSV file
df.to_csv(csv_file_path_combined, index=False)

# Display a message indicating that the combined data has been written to the CSV file
print(f"Combined data has been written to {csv_file_path_combined}")

# Close the database connection
conn.close()

# -------------------------------------------- #
# -------- Calculate Summary Metrics --------- #
# -------------------------------------------- #

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
        'Total Traveler Actions', 'Traveler Value', 'Landlord Value', 'Sessions',
        'Impressions', 'Clicks', 'Cost'
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

    # Calculate additional ratios
    summary_df['CPL_Actual'] = summary_df['Cost_Actual'] / summary_df['FF_Lead_Event_Count_Actual'].replace(0, 1)
    summary_df['CPL_LW'] = summary_df['Cost_LW'] / summary_df['FF_Lead_Event_Count_LW'].replace(0, 1)
    summary_df['CAC_Actual'] = summary_df['Cost_Actual'] / summary_df['FF_Purchase_Event_Count_Actual'].replace(0, 1)
    summary_df['CAC_LW'] = summary_df['Cost_LW'] / summary_df['FF_Purchase_Event_Count_LW'].replace(0, 1)
    summary_df['Traveler_Conversion_Actual'] = summary_df['Total Traveler Actions_Actual'] / summary_df['Sessions_Actual'].replace(0, 1)
    summary_df['Traveler_Conversion_LW'] = summary_df['Total Traveler Actions_LW'] / summary_df['Sessions_LW'].replace(0, 1)
    summary_df['CPTA_Actual'] = summary_df['Cost_Actual'] / summary_df['Total Traveler Actions_Actual'].replace(0, 1)
    summary_df['CPTA_LW'] = summary_df['Cost_LW'] / summary_df['Total Traveler Actions_LW'].replace(0, 1)
    summary_df['ROAS_Actual'] = (summary_df['Traveler Value_Actual'] + summary_df['Landlord Value_Actual']) / summary_df['Cost_Actual'].replace(0, 1)
    summary_df['ROAS_LW'] = (summary_df['Traveler Value_LW'] + summary_df['Landlord Value_LW']) / summary_df['Cost_LW'].replace(0, 1)

    # Calculate WoW percentage differences for ratios
    for ratio in ['CPL', 'CAC', 'Traveler_Conversion', 'CPTA', 'ROAS']:
        summary_df[f'{ratio}_WoW'] = (summary_df[f'{ratio}_Actual'] / summary_df[f'{ratio}_LW'].replace(0, 1)) - 1

    # Rearrange the columns
    columns_order = []
    for metric in metrics + ['Lead_Conversion', 'CPL', 'CAC', 'Traveler_Conversion', 'CPTA', 'ROAS']:
        columns_order.append(f'{metric}_Actual')
        columns_order.append(f'{metric}_LW')
        columns_order.append(f'{metric}_WoW')

    summary_df = summary_df[['Channel Group', 'Campaign Group'] + columns_order]

    # Merge the order DataFrame with the summary DataFrame
    summary_df = summary_df.merge(order_df, on=['Channel Group', 'Campaign Group'], how='left')

    # Sort the summary DataFrame based on the Order_number
    summary_df = summary_df.sort_values(by='Order_number').reset_index(drop=True)

    # Drop the Order_number column
    summary_df = summary_df.drop(columns=['Order_number'])

    return summary_df

# Call the function and print the summary metrics
summary_df = calculate_iso_week_metrics(df)
print(summary_df)

# Optionally, save the summary metrics to a CSV file
summary_csv_path = 'ga4-reports/output/ga4_summary_metrics_iso_weeks_sorted.csv'
summary_df.to_csv(summary_csv_path, index=False)
print(f"Summary metrics have been written to {summary_csv_path}")
