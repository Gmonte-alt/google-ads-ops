# file name:ga4-reports/query_ga4_database_union_events_sessions_group.py
# version: V000-000-008
# output:
# Notes: Updating the Total section and the campaign group order

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
    FF_BRSubmit_Event_Count + FF_DMSubmit_Event_Count + FF_PhoneGet_Event_Count AS "Total Traveler Actions",
    (FF_BRSubmit_Event_Count + FF_DMSubmit_Event_Count + FF_PhoneGet_Event_Count) * 2 AS "Traveler Value",
    FF_Purchase_Event_Count * 240 AS "Landlord Value",
    Impressions,
    Clicks,
    Cost,
    Channel_Group,
    Campaign_Group
FROM
    ga4_combined
"""

# Execute the query and fetch the results into a Pandas DataFrame
df = pd.read_sql_query(query, conn)

# Define the logic to create the "Total" groupings
total_groups = [
    ('Non-Paid Total', lambda df: df[~df['Channel_Group'].str.contains('SEM|Paid')]),
    ('Paid Total', lambda df: df[df['Channel_Group'].str.contains('SEM|Paid')]),
    ('SEM Brand Total', lambda df: df[df['Channel_Group'] == 'SEM Brand']),
    ('SEM Non-Brand Total', lambda df: df[df['Channel_Group'].str.contains('SEM Non-Brand')]),
    ('Google SEM Total', lambda df: df[(df['Campaign_Group'].str.contains('Google') & (df['Channel_Group'].str.contains('SEM')))]),
    ('Bing SEM Total', lambda df: df[(df['Campaign_Group'].str.contains('Bing') & (df['Channel_Group'].str.contains('SEM')))]),
    ('Paid Social + Display Total', lambda df: df[df['Channel_Group'] == 'Paid Social + Display'])
]

# Aggregate the total groups and append them to the DataFrame
total_rows = []
for name, func in total_groups:
    group_df = func(df).groupby(['Date']).sum().reset_index()
    group_df['Campaign_Group'] = name
    group_df['Channel_Group'] = name
    group_df['Source'] = ''
    group_df['Medium'] = ''
    group_df['Campaign'] = ''
    group_df['Device'] = ''
    total_rows.append(group_df)

totals_df = pd.concat(total_rows, ignore_index=True)
df = pd.concat([df, totals_df], ignore_index=True)

# Correct the order DataFrame to ensure all lists have the same length
order_df = pd.DataFrame({
    'Channel_Group': [
        'SEM Brand', 'SEM Brand', 'SEM Non-Brand', 'SEM Non-Brand', 'SEM Non-Brand', 'SEM Non-Brand', 
        'SEM Non-Brand', 'SEM Non-Brand', 'SEM Non-Brand', 'SEM Non-Brand - Tenant', 'SEM Non-Brand', 
        'SEM Non-Brand', 'SEM Brand', 'SEM Non-Brand', 'SEM Brand', 'SEM Non-Brand', 
        'Paid Social + Display', 'Paid Social + Display', 'Paid Social + Display', 
        'Paid Social + Display', 'Paid Social + Display', 'Paid Social + Display', 
        'Paid Social + Display',  # Added "Paid Social + Display"
        'SEM Non-Brand',  # Added "SEM Non-Brand"
        'Paid Total', 'SEM Brand Total', 'SEM Non-Brand Total', 'Google SEM Total', 
        'Bing SEM Total', 'Paid Social + Display Total', 'Non-Paid Total', 'Direct', 
        'Organic Search (SEO)', 'Email', 'Referral', 'Organic Social', 'Organic Video', 'Undefined/Other'
    ],
    'Campaign_Group': [
        'Google Search - Brand', 'Google Search - Brand Broad', 'Google Search - Housing', 
        'Google Search - Healthcare', 'Google Search - Travel Nurse', 'Google Search - Travel Nurse Housing', 
        'Google Search - Corporate', 'Google Search - Landlord', 'Google Search - Competitor', 
        'Google Search - Tenants', 'Google Search - Generics', 'Google Search - Non-Brand (Other)', 
        'Google Search - TNH Brand', 'Google Search - TNH Non-Brand', 'Bing Search - Brand', 
        'Bing Search - Non-Brand (All Campaigns)', 'Facebook Display Prospecting', 'Facebook Display Retargeting', 
        'Facebook Display Prospecting - Traveler', 'Facebook - Other',  # Added "Facebook - Other"
        'Google Display Prospecting', 'Google Display Retargeting', 
        'Google Display Prospecting - Traveler', 
        'Google Search - Non-Brand (Other)',  # Added "Google Search - Non-Brand (Other)"
        'Paid Total', 'SEM Brand Total', 'SEM Non-Brand Total', 
        'Google SEM Total', 'Bing SEM Total', 'Paid Social + Display Total', 'Non-Paid Total', 'Direct', 
        'Organic Search (SEO)', 'Email', 'Referral', 'Organic Social', 'Organic Video', 'Undefined/Other'
    ],
    'Order_number': [
        1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20,  # Continued numbering for added entries
        21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38
    ]
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
    previous_year = target_year - 1

    # Filter for the actual, last week, and YoY week data for the target year
    df_actual = df[(df['ISO_Year'] == target_year) & (df['ISO_Week'] == actual_week)]
    df_lw = df[(df['ISO_Year'] == target_year) & (df['ISO_Week'] == lw_week)]
    df_yoy = df[(df['ISO_Year'] == previous_year) & (df['ISO_Week'] == actual_week)]

    # Define the metrics to aggregate
    metrics = [
        'FF_Lead_Event_Count', 'FF_Purchase_Event_Count', 'FF_BRSubmit_Event_Count',
        'FF_DMSubmit_Event_Count', 'FF_PhoneGet_Event_Count', 'Housing Requests',
        'Total Traveler Actions', 'Traveler Value', 'Landlord Value', 'Sessions',
        'Impressions', 'Clicks', 'Cost'
    ]

    # Aggregate the metrics for "Actual" (current week)
    actual_agg = df_actual.groupby(['Channel_Group', 'Campaign_Group'])[metrics].sum().reset_index()
    actual_agg.columns = ['Channel_Group', 'Campaign_Group'] + [f'{metric}_Actual' for metric in metrics]

    # Aggregate the metrics for "LW" (last week)
    lw_agg = df_lw.groupby(['Channel_Group', 'Campaign_Group'])[metrics].sum().reset_index()
    lw_agg.columns = ['Channel_Group', 'Campaign_Group'] + [f'{metric}_LW' for metric in metrics]

    # Aggregate the metrics for "YoY" (same week last year)
    yoy_agg = df_yoy.groupby(['Channel_Group', 'Campaign_Group'])[metrics].sum().reset_index()
    yoy_agg.columns = ['Channel_Group', 'Campaign_Group'] + [f'{metric}_YoY' for metric in metrics]

    # Merge the actual, LW, and YoY dataframes
    summary_df = actual_agg.merge(lw_agg, on=['Channel_Group', 'Campaign_Group'], how='outer').fillna(0)
    summary_df = summary_df.merge(yoy_agg, on=['Channel_Group', 'Campaign_Group'], how='outer').fillna(0)

    # Calculate Lead Conversion ratios
    summary_df['Lead_Conversion_Actual'] = summary_df['FF_Purchase_Event_Count_Actual'] / summary_df['FF_Lead_Event_Count_Actual'].replace(0, 1)
    summary_df['Lead_Conversion_LW'] = summary_df['FF_Purchase_Event_Count_LW'] / summary_df['FF_Lead_Event_Count_LW'].replace(0, 1)
    summary_df['Lead_Conversion_YoY'] = summary_df['FF_Purchase_Event_Count_YoY'] / summary_df['FF_Lead_Event_Count_YoY'].replace(0, 1)

    # Calculate additional ratios
    summary_df['CPL_Actual'] = summary_df['Cost_Actual'] / summary_df['FF_Lead_Event_Count_Actual'].replace(0, 1)
    summary_df['CPL_LW'] = summary_df['Cost_LW'] / summary_df['FF_Lead_Event_Count_LW'].replace(0, 1)
    summary_df['CPL_YoY'] = summary_df['Cost_YoY'] / summary_df['FF_Lead_Event_Count_YoY'].replace(0, 1)

    summary_df['CAC_Actual'] = summary_df['Cost_Actual'] / summary_df['FF_Purchase_Event_Count_Actual'].replace(0, 1)
    summary_df['CAC_LW'] = summary_df['Cost_LW'] / summary_df['FF_Purchase_Event_Count_LW'].replace(0, 1)
    summary_df['CAC_YoY'] = summary_df['Cost_YoY'] / summary_df['FF_Purchase_Event_Count_YoY'].replace(0, 1)

    summary_df['Traveler_Conversion_Actual'] = summary_df['Total Traveler Actions_Actual'] / summary_df['Sessions_Actual'].replace(0, 1)
    summary_df['Traveler_Conversion_LW'] = summary_df['Total Traveler Actions_LW'] / summary_df['Sessions_LW'].replace(0, 1)
    summary_df['Traveler_Conversion_YoY'] = summary_df['Total Traveler Actions_YoY'] / summary_df['Sessions_YoY'].replace(0, 1)

    summary_df['CPTA_Actual'] = summary_df['Cost_Actual'] / summary_df['Total Traveler Actions_Actual'].replace(0, 1)
    summary_df['CPTA_LW'] = summary_df['Cost_LW'] / summary_df['Total Traveler Actions_LW'].replace(0, 1)
    summary_df['CPTA_YoY'] = summary_df['Cost_YoY'] / summary_df['Total Traveler Actions_YoY'].replace(0, 1)

    summary_df['ROAS_Actual'] = (summary_df['Traveler Value_Actual'] + summary_df['Landlord Value_Actual']) / summary_df['Cost_Actual'].replace(0, 1)
    summary_df['ROAS_LW'] = (summary_df['Traveler Value_LW'] + summary_df['Landlord Value_LW']) / summary_df['Cost_LW'].replace(0, 1)
    summary_df['ROAS_YoY'] = (summary_df['Traveler Value_YoY'] + summary_df['Landlord Value_YoY']) / summary_df['Cost_YoY'].replace(0, 1)

    # Calculate WoW and YoY percentage differences
    for metric in metrics + ['Lead_Conversion', 'CPL', 'CAC', 'Traveler_Conversion', 'CPTA', 'ROAS']:
        summary_df[f'{metric}_WoW'] = (summary_df[f'{metric}_Actual'] / summary_df[f'{metric}_LW'].replace(0, 1)) - 1
        summary_df[f'{metric}_YoY'] = (summary_df[f'{metric}_Actual'] / summary_df[f'{metric}_YoY'].replace(0, 1)) - 1

        # Apply the check to zero out YoY if it's 1 or greater or -1
        summary_df.loc[summary_df[f'{metric}_YoY'].abs() >= 1, f'{metric}_YoY'] = 0

    # Zero out the ratio metrics if their associated volume metrics are zeroed out
    ratio_metric_pairs = {
        'Lead_Conversion_YoY': 'FF_Lead_Event_Count_YoY',
        'CPL_YoY': 'FF_Lead_Event_Count_YoY',
        'CAC_YoY': 'FF_Purchase_Event_Count_YoY',
        'Traveler_Conversion_YoY': 'Total Traveler Actions_YoY',
        'CPTA_YoY': 'Total Traveler Actions_YoY',
        'ROAS_YoY': 'Cost_YoY'
    }
    
    for ratio_metric, volume_metric in ratio_metric_pairs.items():
        if ratio_metric in summary_df.columns and volume_metric in summary_df.columns:
            summary_df.loc[summary_df[volume_metric] == 0, ratio_metric] = 0

    # Rearrange the columns
    columns_order = []
    for metric in metrics + ['Lead_Conversion', 'CPL', 'CAC', 'Traveler_Conversion', 'CPTA', 'ROAS']:
        columns_order.append(f'{metric}_Actual')
        columns_order.append(f'{metric}_LW')
        columns_order.append(f'{metric}_YoY')
        columns_order.append(f'{metric}_WoW')

    summary_df = summary_df[['Channel_Group', 'Campaign_Group'] + columns_order]

    # Merge the order DataFrame with the summary DataFrame
    summary_df = summary_df.merge(order_df, on=['Channel_Group', 'Campaign_Group'], how='left')

    # Sort the summary_df DataFrame based on the Order_number
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
