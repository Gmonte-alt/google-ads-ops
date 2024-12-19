# file name:ga4-reports/query_ga4_database_union_events_sessions_group.py
# version: V000-000-013
# output:
# Notes: Creates the output ga4_wtd_adjusted_data.csv to be ingested into the final excel workbook for trended data

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
    ('SEM Brand + Landlord Total', lambda df: df[(df['Channel_Group'] == 'SEM Brand') | (df['Channel_Group'] == 'SEM Non-Brand')]),
    ('Google SEM Total', lambda df: df[(df['Campaign_Group'].str.contains('Google') & (df['Channel_Group'].str.contains('SEM')))]),
    ('Google SEM Landlord Total', lambda df: df[(df['Campaign_Group'].str.contains('Google') & (df['Channel_Group'] == 'SEM Non-Brand'))]),
    ('Google SEM Tenant Total', lambda df: df[(df['Campaign_Group'].str.contains('Google') & (df['Channel_Group'] == 'SEM Non-Brand - Tenant'))]),
    ('Bing SEM Total', lambda df: df[(df['Campaign_Group'].str.contains('Bing') & (df['Channel_Group'].str.contains('SEM')))]),
    ('Paid Social + Display Total', lambda df: df[df['Channel_Group'] == 'Paid Social + Display']),
    ('Display Total', lambda df: df[(df['Campaign_Group'].str.contains('Google') & (df['Channel_Group'].str.contains('Display')))]),
    ('Paid Social Total', lambda df: df[(df['Campaign_Group'].str.contains('Facebook') & (df['Channel_Group'].str.contains('Paid Social')))])
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

# Add the Grand Total row, excluding any rows that are already subtotals
grand_total_df = df[~df['Channel_Group'].str.contains('Total')].groupby(['Date']).sum().reset_index()

# Set values for the Grand Total row
grand_total_df['Campaign_Group'] = 'Grand Total'
grand_total_df['Channel_Group'] = 'Grand Total'
grand_total_df['Source'] = ''
grand_total_df['Medium'] = ''
grand_total_df['Campaign'] = ''
grand_total_df['Device'] = ''

# Append the Grand Total row to the DataFrame
df = pd.concat([df, grand_total_df], ignore_index=True)

# Correct the order DataFrame to ensure all lists have the same length
order_df = pd.DataFrame({
    'Channel_Group': [
        'SEM Brand', 'SEM Brand', 'SEM Non-Brand', 'SEM Non-Brand', 'SEM Non-Brand', 'SEM Non-Brand', 
        'SEM Non-Brand', 'SEM Non-Brand - Tenant', 'SEM Non-Brand - Tenant', 'SEM Non-Brand', 
        'SEM Non-Brand', 'SEM Non-Brand', 'SEM Brand', 'SEM Non-Brand', 
        'Paid Social + Display', 'Paid Social + Display', 'Paid Social + Display', 
        'Paid Social + Display', 'Paid Social + Display', 'Paid Social + Display', 
        'Paid Social + Display',  # Added "Paid Social + Display"
     #   'SEM Non-Brand',  # Added "SEM Non-Brand"
        'Paid Total', 'SEM Brand Total', 'SEM Non-Brand Total', 'SEM Brand + Landlord Total', 
        'Google SEM Total', 'Google SEM Landlord Total', 'Google SEM Tenant Total', 
        'Bing SEM Total', 'Paid Social + Display Total', 'Display Total', 'Paid Social Total', 'Non-Paid Total', 'Direct', 
        'Organic Search (SEO)', 'Email', 'Referral', 'Organic Social', 'Organic Video', 'Undefined/Other',
        'Grand Total'
    ],
    'Campaign_Group': [
        'Google Search - Brand', 'Google Search - Brand Broad', 'Google Search - Housing', 
        'Google Search - Travel Nurse', 'Google Search - Travel Nurse Housing', 
        'Google Search - Corporate', 'Google Search - Landlord', 'Google Search - Tenants Competitor', 
        'Google Search - Tenants', 'Google Search - Generics', 'Google Search - Non-Brand (Other)', 
        'Google Search - TNH Non-Brand', 'Bing Search - Brand', # 'Google Search - TNH Brand', 
        'Bing Search - Non-Brand (All Campaigns)', 'Facebook Display Prospecting', 'Facebook Display Retargeting', 
        'Facebook Display Prospecting - Traveler', 'Facebook - Other',  # Added "Facebook - Other"
        'Google Display Prospecting', 'Google Display Retargeting', 
        'Google Display Prospecting - Traveler', 
        # 'Google Search - Non-Brand (Other)',  # Added "Google Search - Non-Brand (Other)"
        'Paid Total', 'SEM Brand Total', 'SEM Non-Brand Total', 'SEM Brand + Landlord Total', 
        'Google SEM Total', 'Google SEM Landlord Total', 'Google SEM Tenant Total', 
        'Bing SEM Total', 'Paid Social + Display Total', 'Display Total', 'Paid Social Total', 'Non-Paid Total', 'Direct', 
        'Organic Search (SEO)', 'Email', 'Referral', 'Organic Social', 'Organic Video', 'Undefined/Other',
        'Grand Total'
    ],
    'Order_number': [
        1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20,  # Continued numbering for added entries
        21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41
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

# Function to calculate derived metrics for a given period
def calculate_derived_metrics(df, period_name):
    df[f'Lead_Conversion'] = df[f'FF_Purchase_Event_Count'] / df[f'FF_Lead_Event_Count'].replace(0, 1)
    df[f'CPL'] = df[f'Cost'] / df[f'FF_Lead_Event_Count'].replace(0, 1)
    df[f'CAC'] = df[f'Cost'] / df[f'FF_Purchase_Event_Count'].replace(0, 1)
    df[f'Traveler_Conversion'] = df[f'Total Traveler Actions'] / df[f'Sessions'].replace(0, 1)
    df[f'CPTA'] = df[f'Cost'] / df[f'Total Traveler Actions'].replace(0, 1)
    df[f'ROAS'] = (df[f'Traveler Value'] + df[f'Landlord Value']) / df[f'Cost'].replace(0, 1)
    
    # New Derived Metrics
    df[f'CPC'] = df[f'Cost'] / df[f'Clicks'].replace(0, 1)  # Cost per Click
    df[f'CTR'] = df[f'Clicks'] / df[f'Impressions'].replace(0, 1)  # Click Through Rate
    df[f'RPC_Landlord'] = df[f'Landlord Value'] / df[f'Clicks'].replace(0, 1)  # Revenue per Click - Landlord
    df[f'RPC_Tenant'] = df[f'Traveler Value'] / df[f'Clicks'].replace(0, 1)  # Revenue per Click - Tenant
    
    return df

def calculate_percentage_changes(df, current_period_name, comparison_period_name, change_label):
    metrics = [
        'FF_Lead_Event_Count', 'FF_Purchase_Event_Count', 'FF_BRSubmit_Event_Count',
        'FF_DMSubmit_Event_Count', 'FF_PhoneGet_Event_Count', 'Housing Requests',
        'Total Traveler Actions', 'Traveler Value', 'Landlord Value', 'Sessions',
        'Impressions', 'Clicks', 'Cost', 'Lead_Conversion', 'CPL', 'CAC',
        'Traveler_Conversion', 'CPTA', 'ROAS', 'CPC', 'CTR', 'RPC_Landlord', 'RPC_Tenant'
    ]

    # Create a dictionary to store the original column names and their temporary replacements
    original_to_temp = {}
    temp_to_original = {}

    # Temporarily rename the columns
    for metric in metrics:
        original_col = metric
        temp_col = f'{metric}_{current_period_name}'
        original_to_temp[original_col] = temp_col
        temp_to_original[temp_col] = original_col
        df = df.rename(columns={original_col: temp_col})

    for metric in metrics:
        current_col = f'{metric}_{current_period_name}'
        comparison_col = f'{metric}_{comparison_period_name}'
        change_col = f'{metric}_{change_label}'
        
        if current_col in df.columns and comparison_col in df.columns:
            with pd.option_context('mode.use_inf_as_na', True):
                df[change_col] = ((df[current_col] / df[comparison_col].replace(0, pd.NA)) - 1)
            
            # Zero out extreme values or NaN
            df.loc[df[change_col].abs() >= 100, change_col] = 0
            # Apply .fillna(0) and then use .infer_objects(copy=False) to avoid FutureWarning
            df[change_col] = df[change_col].fillna(0).infer_objects(copy=False)
    
    # Rename the columns back to their original names
    df = df.rename(columns=temp_to_original)

    return df


def calculate_iso_week_metrics(df, start_date, end_date, period_name, previous_start_date=None, previous_end_date=None, previous_yoy_start_date=None, previous_yoy_end_date=None):
    # Convert Date to datetime format
    df['Date'] = pd.to_datetime(df['Date'], format='%Y%m%d')

    # Filter for the date range for the current period
    df_period = df[(df['Date'] >= start_date) & (df['Date'] <= end_date)]

    # Group by Channel and Campaign Group and sum metrics
    metrics = [
        'FF_Lead_Event_Count', 'FF_Purchase_Event_Count', 'FF_BRSubmit_Event_Count',
        'FF_DMSubmit_Event_Count', 'FF_PhoneGet_Event_Count', 'Housing Requests',
        'Total Traveler Actions', 'Traveler Value', 'Landlord Value', 'Sessions',
        'Impressions', 'Clicks', 'Cost'
    ]
    
    summary_df = df_period.groupby(['Channel_Group', 'Campaign_Group'])[metrics].sum().reset_index()
    summary_df = calculate_derived_metrics(summary_df, period_name)

    # Calculate WoW if we have a previous period
    if previous_start_date and previous_end_date:
        df_previous_period = df[(df['Date'] >= previous_start_date) & (df['Date'] <= previous_end_date)]
        previous_summary_df = df_previous_period.groupby(['Channel_Group', 'Campaign_Group'])[metrics].sum().reset_index()
        previous_summary_df = calculate_derived_metrics(previous_summary_df, 'Previous')

        summary_df = summary_df.merge(previous_summary_df, on=['Channel_Group', 'Campaign_Group'], how='outer', suffixes=('','_previous')).fillna(0)

        # Calculate WoW percentage changes
        summary_df = calculate_percentage_changes(summary_df, period_name, 'previous', 'WoW')

    # Calculate YoY if we have a YoY period
    if previous_yoy_start_date and previous_yoy_end_date:
        df_previous_yoy_period = df[(df['Date'] >= previous_yoy_start_date) & (df['Date'] <= previous_yoy_end_date)]
        previous_yoy_summary_df = df_previous_yoy_period.groupby(['Channel_Group', 'Campaign_Group'])[metrics].sum().reset_index()
        previous_yoy_summary_df = calculate_derived_metrics(previous_yoy_summary_df, 'YoY')

        summary_df = summary_df.merge(previous_yoy_summary_df, on=['Channel_Group', 'Campaign_Group'], how='outer', suffixes=('','_YoY')).fillna(0)

        # Calculate YoY percentage changes
        summary_df = calculate_percentage_changes(summary_df, period_name, 'YoY', 'YoY')

    # Format the start_date and end_date to short date format
    start_date_str = start_date.strftime('%m/%d/%Y')
    end_date_str = end_date.strftime('%m/%d/%Y')

    # Add formatted start_date and end_date columns
    summary_df['Start_Date_Period'] = start_date_str
    summary_df['End_Date_Period'] = end_date_str

    # Add the period name as a column to identify each summary
    summary_df['Period'] = period_name

    return summary_df

# --------------------------------------------------------------------------------------------- #
# ------------------ CALCULATE ADJUSTED WTD DATA FOR TRENDED VIEW DATA PREP ------------------- #
# --------------------------------------------------------------------------------------------- #

def calculate_adjusted_wtd_data(df, reference_date):
    """
    Calculate adjusted WTD data by normalizing each ISO week in the DataFrame
    to match the day span of the last report week's ISO week.
    """
    # Step 1: Convert the date column to datetime format, if not already
    df['Date'] = pd.to_datetime(df['Date'], format='%Y%m%d')

    # Step 2: Extract the ISO week, ISO year, and day of the week
    df['ISO_Year'] = df['Date'].dt.isocalendar().year
    df['ISO_Week'] = df['Date'].dt.isocalendar().week
    df['Day_of_Week'] = df['Date'].dt.dayofweek

    # Verify the column creation
    print("Step 2: Columns in the DataFrame after adding ISO Year and Week:")
    print(df.columns)

    # Step 3: Determine the last report week's ISO year and ISO week
    last_iso_year = reference_date.isocalendar().year
    last_iso_week = reference_date.isocalendar().week
    max_day_of_week = reference_date.weekday()

    # Step 4: Filter the DataFrame to only include days up to the same day of week as the last report date
    df_filtered = df[df['Day_of_Week'] <= max_day_of_week]

    # Verify columns in df_filtered and check if 'ISO_Year' is present
    print("Step 4: Columns in df_filtered before grouping:")
    print(df_filtered.columns)

    if 'ISO_Year' not in df_filtered.columns:
        raise KeyError("Column 'ISO_Year' is missing from df_filtered DataFrame.")

    # Step 5: Remove unnecessary columns before aggregation
    # Columns that are not needed for the aggregation and are not numeric will be removed.
    columns_to_keep = [
        'ISO_Year', 'ISO_Week', 'Channel_Group', 'Campaign_Group',
        'FF_Purchase_Event_Count', 'FF_Lead_Event_Count', 'FF_BRSubmit_Event_Count',
        'FF_DMSubmit_Event_Count', 'FF_PhoneGet_Event_Count', 'FF_HRSubmit_Event_Count',
        'HR_Submit_Event_New_Traveler_Lead_Count', 'Housing Requests', 'Total Traveler Actions',
        'Traveler Value', 'Landlord Value', 'Impressions', 'Clicks', 'Cost', 'Sessions'
    ]
    df_filtered = df_filtered[columns_to_keep]

    # Verify the columns after dropping unnecessary ones
    print("Step 5: Columns in df_filtered after removing unnecessary ones:")
    print(df_filtered.columns)

    # Step 6: Group the data by ISO Year, ISO Week, Channel Group, and Campaign Group
    week_summary = df_filtered.groupby(
        ['ISO_Year', 'ISO_Week', 'Channel_Group', 'Campaign_Group']
    ).sum().reset_index()

    # Verify the result of grouping
    print("Step 6: Week summary after grouping:")
    print(week_summary.head())

    # Return the adjusted WTD data
    return week_summary


# --------------------------------------------------------------------------------------------- #


# Step 2: Define the date ranges for EOW, WTD, MTD, and QTD
def get_date_ranges(reference_date):
    # Calculate dates
    eow_end_date = reference_date - timedelta(days=reference_date.weekday() + 1)
    eow_start_date = eow_end_date - timedelta(days=6)
    
    wtd_start_date = reference_date.replace(hour=0, minute=0, second=0, microsecond=0)
    wtd_start_date = wtd_start_date - timedelta(days=reference_date.weekday())

    mtd_start_date = reference_date.replace(day=1)
    
    qtd_start_date = (reference_date - timedelta(days=reference_date.day-1)).replace(month=((reference_date.month-1)//3)*3+1, day=1)
    
    return {
        'EOW': (eow_start_date, eow_end_date),
        'WTD': (wtd_start_date, reference_date),
        'MTD': (mtd_start_date, reference_date),
        'QTD': (qtd_start_date, reference_date)
    }

# Get current reference date (for example, last day of ISO week 31)
# reference_date = datetime(2024, 8, 11)  # This can be dynamically set
# Automate the calculation of the reference_date as the day before yesterday
reference_date = datetime.now() - timedelta(days=2)
reference_date = reference_date.replace(hour=0, minute=0, second=0, microsecond=0)

date_ranges = get_date_ranges(reference_date)

def get_previous_period_dates(period_name, start_date, end_date):
    if period_name in ['WTD', 'EOW']:
        # Compare to the same days in the previous week for WoW
        previous_start_date = start_date - timedelta(weeks=1)
        previous_end_date = end_date - timedelta(weeks=1)

        # Calculate YoY based on the same ISO week in the previous year
        previous_yoy_start_date = start_date - timedelta(weeks=52)
        previous_yoy_end_date = end_date - timedelta(weeks=52)
        
        # Adjust if using ISO week logic instead of a fixed 52-week shift:
        start_iso_year, start_iso_week, _ = start_date.isocalendar()
        end_iso_year, end_iso_week, _ = end_date.isocalendar()
        
        previous_yoy_start_date = start_date.replace(year=start_iso_year - 1)
        previous_yoy_end_date = end_date.replace(year=end_iso_year - 1)
        
        previous_yoy_start_date = previous_yoy_start_date - timedelta(days=previous_yoy_start_date.weekday())
        previous_yoy_end_date = previous_yoy_start_date + timedelta(days=6)
        
    elif period_name == 'MTD':
        # Compare to the same days in the previous month
        previous_start_date = start_date.replace(day=1) - timedelta(days=1)
        previous_start_date = previous_start_date.replace(day=start_date.day)
        previous_end_date = end_date.replace(day=1) - timedelta(days=1)
        previous_end_date = previous_end_date.replace(day=end_date.day)
        
        previous_yoy_start_date = start_date.replace(year=start_date.year - 1)
        previous_yoy_end_date = end_date.replace(year=end_date.year - 1)
        
    elif period_name == 'QTD':
        # Determine the current quarter
        current_quarter = (start_date.month - 1) // 3 + 1
        
        # Calculate the start and end dates of the previous quarter
        if current_quarter == 1:
            previous_start_date = datetime(start_date.year - 1, 10, 1)
            previous_end_date = datetime(start_date.year - 1, 12, 31)
        elif current_quarter == 2:
            previous_start_date = datetime(start_date.year, 1, 1)
            previous_end_date = datetime(start_date.year, 3, 31)
        elif current_quarter == 3:
            previous_start_date = datetime(start_date.year, 4, 1)
            previous_end_date = datetime(start_date.year, 6, 30)
        elif current_quarter == 4:
            previous_start_date = datetime(start_date.year, 7, 1)
            previous_end_date = datetime(start_date.year, 9, 30)
        
        # Adjust the previous end date to match the same relative position within the quarter
        previous_end_date = previous_start_date + timedelta(days=(end_date - start_date).days)
        
        # Previous YoY start and end dates
        previous_yoy_start_date = previous_start_date.replace(year=start_date.year - 1)
        previous_yoy_end_date = previous_end_date.replace(year=end_date.year - 1)
        
    else:
        previous_start_date, previous_end_date = None, None
        previous_yoy_start_date, previous_yoy_end_date = None, None
        
    return previous_start_date, previous_end_date, previous_yoy_start_date, previous_yoy_end_date


# Step 4: Expand rows by adding a 'Period' column and concatenating the dataframes
summary_dfs = []
for period_name, (start_date, end_date) in date_ranges.items():
    previous_start_date, previous_end_date, previous_yoy_start_date, previous_yoy_end_date = get_previous_period_dates(period_name, start_date, end_date)
    summary_df = calculate_iso_week_metrics(df, start_date, end_date, period_name, previous_start_date, previous_end_date, previous_yoy_start_date, previous_yoy_end_date)
    summary_df['Period'] = period_name
    summary_dfs.append(summary_df)

# Combine all summaries into one DataFrame by expanding rows
final_summary_df = pd.concat(summary_dfs, ignore_index=True)

# Remove any duplicated columns (if they exist)
#final_summary_df = final_summary_df.loc[:,~final_summary_df.columns.duplicated()]

# Apply the sorting logic as before using the order_df
final_summary_df = final_summary_df.merge(order_df, on=['Channel_Group', 'Campaign_Group'], how='left')
final_summary_df = final_summary_df.sort_values(by='Order_number').reset_index(drop=True)
final_summary_df = final_summary_df.drop(columns=['Order_number'])

# Save the final summary DataFrame to CSV
summary_csv_path = 'ga4-reports/output/ga4_summary_metrics_multiple_periods.csv'
final_summary_df.to_csv(summary_csv_path, index=False)
print(f"Summary metrics for multiple periods have been written to {summary_csv_path}")


# ---------------------------- Save the WTD data into csv ---------------------------- #
# Call the function to calculate WTD-adjusted data using the existing reference_date
df_wtd_adjusted = calculate_adjusted_wtd_data(df, reference_date)

# Define the CSV file path for the WTD-adjusted data
wtd_csv_file_path = 'ga4-reports/output/ga4_wtd_adjusted_data.csv'

# Write the adjusted WTD DataFrame to a CSV file
df_wtd_adjusted.to_csv(wtd_csv_file_path, index=False)

# Display a message indicating that the adjusted WTD data has been written to the CSV file
print(f"Adjusted WTD data has been written to {wtd_csv_file_path}")
