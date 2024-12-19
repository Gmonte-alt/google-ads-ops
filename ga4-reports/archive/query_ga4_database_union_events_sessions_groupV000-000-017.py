# file name:ga4-reports/query_ga4_database_union_events_sessions_group.py
# version: V000-000-017
# output:
# Notes: corrects the function "calculate_adjusted_wtd_data" to filter on 'DayofWeek' & resolved "WTD" period YoY comp

import sqlite3
import pandas as pd
from datetime import date, datetime, timedelta


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
    
    # New Derived Metrics 10-31-2024
    df[f'Purchase_to_Sessions'] = df[f'FF_Purchase_Event_Count'] / df[f'Sessions'].replace(0, 1)
    df[f'Lead_to_Sessions'] = df[f'FF_Lead_Event_Count'] / df[f'Sessions'].replace(0, 1)
    
    return df

def calculate_percentage_changes(df, current_period_name, comparison_period_name, change_label):
    metrics = [
        'FF_Lead_Event_Count', 'FF_Purchase_Event_Count', 'FF_BRSubmit_Event_Count',
        'FF_DMSubmit_Event_Count', 'FF_PhoneGet_Event_Count', 'Housing Requests',
        'Total Traveler Actions', 'Traveler Value', 'Landlord Value', 'Sessions',
        'Impressions', 'Clicks', 'Cost', 'Lead_Conversion', 'CPL', 'CAC',
        'Traveler_Conversion', 'CPTA', 'ROAS', 'CPC', 'CTR', 'RPC_Landlord', 'RPC_Tenant',
        'Purchase_to_Sessions', 'Lead_to_Sessions'
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
    to match the day span of the last report week's ISO week, up to the same
    day of the week across historical weeks.
    """
    # Step 1: Convert the date column to datetime format, if not already
    df['Date'] = pd.to_datetime(df['Date'], format='%Y%m%d')

    # Step 2: Extract the ISO week, ISO year, and day of the week
    df['ISO_Year'] = df['Date'].dt.isocalendar().year
    df['ISO_Week'] = df['Date'].dt.isocalendar().week
    df['Day_of_Week'] = df['Date'].dt.dayofweek  # Monday=0, Sunday=6

    # Verify the column creation
    print("Step 2: Columns in the DataFrame after adding ISO Year and Week:")
    print(df.columns)

    # Step 3: Determine the last full ISO week and day of the week based on reference_date
    last_iso_year = reference_date.isocalendar().year
    last_iso_week = reference_date.isocalendar().week
    last_day_of_week = reference_date.weekday()  # Get the last day of the week to compare

    # Step 4: Filter the DataFrame to include only:
    # - ISO weeks up to the last full week
    # - Days of the week that are less than or equal to the last day of the current week
    df_filtered = df[
        (
            (df['ISO_Year'] < last_iso_year) | 
            ((df['ISO_Year'] == last_iso_year) & (df['ISO_Week'] < last_iso_week))
        ) |  # For previous years or weeks
        (
            (df['ISO_Year'] == last_iso_year) & (df['ISO_Week'] == last_iso_week) & 
            (df['Day_of_Week'] <= last_day_of_week)
        )  # For the current week, up to the reference day of the week
    ]

    # Verify columns in df_filtered after applying both the week and day filters
    print("Step 4: Data after filtering by ISO Year, ISO Week, and Day of Week:")
    print(df_filtered[['ISO_Year', 'ISO_Week', 'Day_of_Week']].drop_duplicates())
    
    # For all historical weeks, restrict to the same number of days as the current week
    df_filtered = df_filtered[
        (df_filtered['Day_of_Week'] <= last_day_of_week)
    ]

    # Step 5: Remove unnecessary columns before aggregation
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
    # Calculate dates for existing views
    eow_end_date = reference_date - timedelta(days=reference_date.weekday() + 1)
    eow_start_date = eow_end_date - timedelta(days=6)
    
    wtd_start_date = reference_date.replace(hour=0, minute=0, second=0, microsecond=0)
    wtd_start_date = wtd_start_date - timedelta(days=reference_date.weekday())

    mtd_start_date = reference_date.replace(day=1)
    
    qtd_start_date = (reference_date - timedelta(days=reference_date.day-1)).replace(month=((reference_date.month-1)//3)*3+1, day=1)

    # Calculate dates for new views
    eom_end_date = reference_date.replace(day=1) - timedelta(days=1)  # Last day of the previous month
    eom_start_date = eom_end_date.replace(day=1)  # First day of the previous month

    current_quarter = (reference_date.month - 1) // 3 + 1
    # Adjust to last completed quarter
     # Adjust to last completed quarter
    if current_quarter == 1:
        # If in Q1, last completed quarter is Q4 of the previous year
        eoq_end_date = datetime(reference_date.year - 1, 12, 31)
        eoq_start_date = datetime(reference_date.year - 1, 10, 1)
    else:
        # For other quarters, calculate the last completed quarter's end and start dates
        previous_quarter_end_month = (current_quarter - 1) * 3  # Get the last month of the previous quarter
        eoq_end_date = datetime(reference_date.year, previous_quarter_end_month, 1) + timedelta(days=32)
        eoq_end_date = eoq_end_date.replace(day=1) - timedelta(days=1)  # Last day of the previous quarter
        eoq_start_date = datetime(reference_date.year, previous_quarter_end_month - 2, 1)

    ytd_start_date = reference_date.replace(month=1, day=1)

    return {
        'EOW': (eow_start_date, eow_end_date),
        'WTD': (wtd_start_date, reference_date),
        'MTD': (mtd_start_date, reference_date),
        'QTD': (qtd_start_date, reference_date),
        'EOM': (eom_start_date, eom_end_date),
        'EOQ': (eoq_start_date, eoq_end_date),
        'YTD': (ytd_start_date, reference_date)
    }


# Get current reference date (for example, last day of ISO week 31)
# reference_date = datetime(2024, 8, 11)  # This can be dynamically set
# Automate the calculation of the reference_date as the day before yesterday
reference_date = datetime.now() - timedelta(days=2)
reference_date = reference_date.replace(hour=0, minute=0, second=0, microsecond=0)

date_ranges = get_date_ranges(reference_date)

def get_previous_period_dates(period_name, start_date, end_date, available_dates=None):
    # Optional: Provide a list of available dates in your DataFrame
    if available_dates is None:
        available_dates = []
    
    # Set default values for the variables
    previous_start_date, previous_end_date = None, None
    previous_yoy_start_date, previous_yoy_end_date = None, None

    if period_name in ['EOW']:
        # Compare to the same days in the previous week for WoW
        previous_start_date = start_date - timedelta(weeks=1)
        previous_end_date = end_date - timedelta(weeks=1)

        # Calculate YoY based on the same ISO week in the previous year
        start_iso_year, start_iso_week, _ = start_date.isocalendar()
        end_iso_year, end_iso_week, _ = end_date.isocalendar()

        # Find the first day (Monday) of the same ISO week last year
        previous_yoy_start_date = datetime.fromisocalendar(start_iso_year - 1, start_iso_week, 1)
        # Find the last day (Sunday) of the same ISO week last year
        previous_yoy_end_date = previous_yoy_start_date + timedelta(days=6)
        
    elif period_name in ['WTD']:
        # Compare to the same days in the previous week for WoW
        previous_start_date = start_date - timedelta(weeks=1)
        previous_end_date = end_date - timedelta(weeks=1)

        # Calculate YoY based on the same ISO week and day of week in the previous year
        start_iso_year, start_iso_week, _ = start_date.isocalendar()
        end_day_of_week = end_date.weekday()  # Get the current day of the week (e.g., Monday=0, Sunday=6)

        # Find the first day (Monday) of the same ISO week last year
        previous_yoy_start_date = datetime.fromisocalendar(start_iso_year - 1, start_iso_week, 1)
    
        # Adjust the previous_yoy_end_date to align with the same day of the week
        previous_yoy_end_date = previous_yoy_start_date + timedelta(days=end_day_of_week)
        
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

        # Calculate the start and end dates of the current quarter (for the current quarter calculation)
        if current_quarter == 1:
            start_date = datetime(start_date.year, 1, 1)
            end_date = datetime(start_date.year, 3, 31)
        elif current_quarter == 2:
            start_date = datetime(start_date.year, 4, 1)
            end_date = datetime(start_date.year, 6, 30)
        elif current_quarter == 3:
            start_date = datetime(start_date.year, 7, 1)
            end_date = datetime(start_date.year, 9, 30)
        elif current_quarter == 4:
            start_date = datetime(start_date.year, 10, 1)
            end_date = datetime(start_date.year, 12, 31)

        # Adjust the end_date to match the current progress in the quarter (QTD)
        end_date = start_date + timedelta(days=(reference_date - start_date).days)

        # Previous Quarter Dates (LW equivalent)
        if current_quarter == 1:
            # Previous quarter is Q4 of last year
            previous_start_date = datetime(start_date.year - 1, 10, 1)
            previous_end_date = datetime(start_date.year - 1, 12, 31)
        elif current_quarter == 2:
            # Previous quarter is Q1 of current year
            previous_start_date = datetime(start_date.year, 1, 1)
            previous_end_date = datetime(start_date.year, 3, 31)
        elif current_quarter == 3:
            # Previous quarter is Q2 of current year
            previous_start_date = datetime(start_date.year, 4, 1)
            previous_end_date = datetime(start_date.year, 6, 30)
        elif current_quarter == 4:
            # Previous quarter is Q3 of current year
            previous_start_date = datetime(start_date.year, 7, 1)
            previous_end_date = datetime(start_date.year, 9, 30)

        # Adjust the previous_end_date to match the same relative position within the previous quarter
        previous_end_date = previous_start_date + timedelta(days=(end_date - start_date).days)

        # Previous YoY start and end dates should reference the same quarter last year
        previous_yoy_start_date = previous_start_date.replace(year=start_date.year - 1)
        previous_yoy_end_date = previous_end_date.replace(year=end_date.year - 1)
        
    elif period_name == 'EOM':
        # Compare to the same days in the previous month for EOM
        previous_start_date = (start_date.replace(day=1) - timedelta(days=1)).replace(day=1)
        previous_end_date = start_date.replace(day=1) - timedelta(days=1)
        
        # Previous YoY for the same month last year
        previous_yoy_start_date = previous_start_date.replace(year=start_date.year - 1)
        previous_yoy_end_date = previous_end_date.replace(year=end_date.year - 1)
    
    elif period_name == 'EOQ':
        # Determine the current quarter
        current_quarter = (start_date.month - 1) // 3 + 1
        previous_end_date = start_date.replace(month=current_quarter * 3, day=1) - timedelta(days=1)
        previous_start_date = previous_end_date.replace(day=1) - timedelta(days=previous_end_date.day - 1)

        # Previous YoY for the same quarter last year
        previous_yoy_start_date = previous_start_date.replace(year=start_date.year - 1)
        previous_yoy_end_date = previous_end_date.replace(year=end_date.year - 1)

    elif period_name == 'YTD':
        # Compare to the same days in the previous year for YTD
        previous_start_date = start_date.replace(year=start_date.year - 1, month=1, day=1)
        previous_end_date = end_date.replace(year=end_date.year - 1)
        
        # Check if data exists for the intended YoY period
        if previous_start_date not in available_dates or previous_end_date not in available_dates:
            print(f"Data not available for the YoY comparison in {period_name} period.")
            previous_yoy_start_date, previous_yoy_end_date = None, None  # Set to None if data is missing
        else:
            # Previous YoY is simply the same period last year
            previous_yoy_start_date = previous_start_date.replace(year=start_date.year - 2)
            previous_yoy_end_date = previous_end_date.replace(year=end_date.year - 2)
        
    else:
        # Default values for undefined period names
        previous_start_date, previous_end_date = None, None
        previous_yoy_start_date, previous_yoy_end_date = None, None
        
    # Check if start and end dates are still None, print a warning message
    if previous_start_date is None or previous_end_date is None:
        print(f"Warning: {period_name} period did not get valid start/end dates.")

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


# ------------------------------------------------------------------------------------ #
# ---------------------------- Save the WTD data into csv ---------------------------- #
# ------------------------------------------------------------------------------------ #

def flag_incomplete_weeks(daily_df, reference_date):
    # Ensure Date is in the 'YYYYMMDD' string format and convert it to datetime
    daily_df['Date'] = pd.to_datetime(daily_df['Date'], format='%Y%m%d')
    
    # Create ISO Year, Week, and Weekday columns
    daily_df['ISO_Year'] = daily_df['Date'].dt.isocalendar().year
    daily_df['ISO_Week'] = daily_df['Date'].dt.isocalendar().week
    daily_df['Weekday'] = daily_df['Date'].dt.weekday  # Monday=0, Sunday=6
    
    # List of metrics to check
    metrics = [
        'FF_Purchase_Event_Count'
    ]
    
    # Group by ISO Year, Week, and Weekday to flag days where all values of a metric are zero
    daily_flags = daily_df.groupby(['ISO_Year', 'ISO_Week', 'Weekday']).apply(
        lambda x: pd.Series({
            'Incomplete_Week': (x[metrics].sum() == 0).any(axis=None)  # If all values in a day are zero for the metric, flag as incomplete
        })
    ).reset_index()

    # Group by ISO Year and ISO Week, and set the week as incomplete if any day is flagged as incomplete
    weekly_flags = daily_flags.groupby(['ISO_Year', 'ISO_Week']).apply(
        lambda x: pd.Series({
            'Incomplete_Week': x['Incomplete_Week'].any()  # If any day in the week is flagged as incomplete, flag the entire week
        })
    ).reset_index()

    # Get the current ISO year and ISO week from the reference_date
    current_iso_year = reference_date.isocalendar().year
    current_iso_week = reference_date.isocalendar().week

    # Exclude the current week from being flagged
    weekly_flags = weekly_flags[
        ~((weekly_flags['ISO_Year'] == current_iso_year) & (weekly_flags['ISO_Week'] == current_iso_week))
    ]

    return weekly_flags


# Function to calculate nominal differences for WoW and YoY
def calculate_wow_yoy_differences(df):
    # List of metrics to calculate the differences for
    metrics = [
        'FF_Purchase_Event_Count', 'FF_Lead_Event_Count', 'FF_BRSubmit_Event_Count',
        'FF_DMSubmit_Event_Count', 'FF_PhoneGet_Event_Count', 'FF_HRSubmit_Event_Count',
        'Housing Requests', 'Total Traveler Actions', 'Traveler Value', 'Landlord Value',
        'Impressions', 'Clicks', 'Cost', 'Sessions', 'Lead_Conversion', 'CPL', 'CAC',
        'Traveler_Conversion', 'CPTA', 'ROAS', 'CPC', 'CTR', 'RPC_Landlord', 'RPC_Tenant',
        'Purchase_to_Sessions', 'Lead_to_Sessions'
    ]
    
    # Create empty columns for the WoW and YoY differences
    for metric in metrics:
        df[f'{metric}_WoW'] = 0
        df[f'{metric}_YoY'] = 0

    # Iterate over each row of the DataFrame
    for index, row in df.iterrows():
        # Skip calculation if the current week is flagged as incomplete
        if row['Incomplete_Week']:
            continue

        # Get the current year and week
        current_year = row['ISO_Year']
        current_week = row['ISO_Week']

        # WoW: Get the previous week (accounting for year transitions)
        if current_week == 1:
            previous_week = 52  # Assuming 52 weeks in the previous year
            previous_year = current_year - 1
        else:
            previous_week = current_week - 1
            previous_year = current_year
        
        # YoY: Get the same week in the previous year
        yoy_year = current_year - 1
        yoy_week = current_week
        
        # Filter the DataFrame to find the corresponding previous week and YoY rows
        previous_week_row = df[(df['ISO_Year'] == previous_year) & (df['ISO_Week'] == previous_week) & 
                               (df['Channel_Group'] == row['Channel_Group']) & (df['Campaign_Group'] == row['Campaign_Group'])]

        yoy_row = df[(df['ISO_Year'] == yoy_year) & (df['ISO_Week'] == yoy_week) & 
                     (df['Channel_Group'] == row['Channel_Group']) & (df['Campaign_Group'] == row['Campaign_Group'])]
        
        # Iterate over the metrics for each row
        for metric in metrics:
            # Skip WoW calculation if the previous week is incomplete
            if not previous_week_row.empty and not previous_week_row.iloc[0]['Incomplete_Week']:
                previous_value = previous_week_row.iloc[0][metric]
                if previous_value != 0:
                    df.at[index, f'{metric}_WoW'] = row[metric] - previous_value
                else:
                    df.at[index, f'{metric}_WoW'] = 0

            # Skip YoY calculation if the YoY week is incomplete
            if not yoy_row.empty and not yoy_row.iloc[0]['Incomplete_Week']:
                yoy_value = yoy_row.iloc[0][metric]
                if yoy_value != 0:
                    df.at[index, f'{metric}_YoY'] = row[metric] - yoy_value
                else:
                    df.at[index, f'{metric}_YoY'] = 0

    return df


# Assuming you have access to the original daily data in 'df'
incomplete_flags = flag_incomplete_weeks(df, reference_date)

# Export incomplete week flags to a CSV file for review
incomplete_flags_csv_path = 'ga4-reports/output/incomplete_week_flags.csv'

# Save the incomplete flags DataFrame to CSV
incomplete_flags.to_csv(incomplete_flags_csv_path, index=False)

# Display a message indicating that the incomplete week flags have been written to the CSV file
print(f"Incomplete week flags have been written to {incomplete_flags_csv_path}")

# Call the function to calculate WTD-adjusted data using the existing reference_date
df_wtd_adjusted = calculate_adjusted_wtd_data(df, reference_date)

# Filter df_wtd_adjusted to only include data from 2023 and ISO week >= 1
df_wtd_adjusted = df_wtd_adjusted[(df_wtd_adjusted['ISO_Year'] >= 2023)]

# Verify the filtered result
print(df_wtd_adjusted[['ISO_Year', 'ISO_Week']].drop_duplicates())

# Merge the flags with the aggregated weekly data (df_wtd_adjusted)
df_wtd_adjusted = df_wtd_adjusted.merge(incomplete_flags, on=['ISO_Year', 'ISO_Week'], how='left')

# Fill NaN flags with False (meaning the week was complete if no flag exists)
df_wtd_adjusted['Incomplete_Week'].fillna(False, inplace=True)
df_wtd_adjusted['Incomplete_Week'] = df_wtd_adjusted['Incomplete_Week'].fillna(False)

df_wtd_adjusted = calculate_derived_metrics(df_wtd_adjusted, 'WTD')

# Apply the function to calculate the nominal differences
df_wtd_adjusted = calculate_wow_yoy_differences(df_wtd_adjusted)

# Define the CSV file path for the WTD-adjusted data
wtd_csv_file_path = 'ga4-reports/output/ga4_wtd_adjusted_data.csv'

# Write the adjusted WTD DataFrame to a CSV file
df_wtd_adjusted.to_csv(wtd_csv_file_path, index=False)


# Display a message indicating that the adjusted WTD data has been written to the CSV file
print(f"Adjusted WTD data with WoW and YoY differences has been written to {wtd_csv_file_path}")