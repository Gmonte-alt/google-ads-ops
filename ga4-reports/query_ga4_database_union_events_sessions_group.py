# file name:ga4-reports/query_ga4_database_union_events_sessions_group.py
# version: V000-000-020
# output:
# Notes: Updates the WTD file to now include Device and Form Factor columns for slicer filtering in the excel workbook

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
    Impression_population,
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
    # ('SEM Non-Brand Total', lambda df: df[df['Channel_Group'].str.contains('SEM Non-Brand')]),
    ('SEM Non-Brand Landlord Total', lambda df: df[df['Channel_Group'] == 'SEM Non-Brand']),
    ('SEM Non-Brand Tenant Total', lambda df: df[df['Channel_Group'] == 'SEM Non-Brand - Tenant']),
    ('SEM Brand + Landlord Total', lambda df: df[(df['Channel_Group'] == 'SEM Brand') | (df['Channel_Group'] == 'SEM Non-Brand')]),
    # ('Google SEM Total', lambda df: df[(df['Campaign_Group'].str.contains('Google') & (df['Channel_Group'].str.contains('SEM')))]),
    ('Google FF SEM Total', lambda df: df[(df['Campaign_Group'].str.contains('Google')) & (df['Channel_Group'].str.contains('SEM')) & (~df['Campaign_Group'].str.contains('TNH'))]),
    ('Google TNH SEM Total', lambda df: df[(df['Campaign_Group'].str.contains('Google')) & (df['Channel_Group'].str.contains('SEM')) & (df['Campaign_Group'].str.contains('TNH'))]),
    # ('Google SEM Landlord Total', lambda df: df[(df['Campaign_Group'].str.contains('Google') & (df['Channel_Group'] == 'SEM Non-Brand'))]),
    # ('Google SEM Tenant Total', lambda df: df[(df['Campaign_Group'].str.contains('Google') & (df['Channel_Group'] == 'SEM Non-Brand - Tenant'))]),
    ('Bing FF SEM Total', lambda df: df[(df['Campaign_Group'].str.contains('Bing')) & (df['Channel_Group'].str.contains('SEM')) & (~df['Campaign_Group'].str.contains('TNH'))]),
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
        'Paid Total', 'SEM Brand Total', 'SEM Brand + Landlord Total', 'SEM Non-Brand Landlord Total', 'SEM Non-Brand Tenant Total', #'SEM Non-Brand Total',
        # 'Google SEM Total', 'Google SEM Landlord Total', 'Google SEM Tenant Total', 
        'Google FF SEM Total', 'Google TNH SEM Total', 'Bing FF SEM Total',
        'Paid Social + Display Total', 'Display Total', 'Paid Social Total', 'Non-Paid Total', 'Direct',  # 'Bing SEM Total', 
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
        'Paid Total', 'SEM Brand Total', 'SEM Brand + Landlord Total',  'SEM Non-Brand Landlord Total', 'SEM Non-Brand Tenant Total', #  'SEM Non-Brand Total',
        # 'Google SEM Total', 'Google SEM Landlord Total', 'Google SEM Tenant Total', 
        'Google FF SEM Total', 'Google TNH SEM Total', 'Bing FF SEM Total',
        'Paid Social + Display Total', 'Display Total', 'Paid Social Total', 'Non-Paid Total', 'Direct', # 'Bing SEM Total', 
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

# Function to transform all date formats into the 'YYYYMMDD' string format
def standardize_date_column(df, column_name):
    def transform_date(value):
        try:
            # Parse the value into a datetime object
            date_obj = pd.to_datetime(value)
            # Format the datetime object into the desired string format
            return date_obj.strftime('%Y%m%d')
        except Exception as e:
            print(f"Error processing value {value}: {e}")
            return None  # Return None if the value cannot be processed

    df[column_name] = df[column_name].apply(transform_date)
    return df

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
    
    # New Derived Metrics 9-30-2024
    df[f'Purchase_to_Sessions'] = df[f'FF_Purchase_Event_Count'] / df[f'Sessions'].replace(0, 1)
    df[f'Lead_to_Sessions'] = df[f'FF_Lead_Event_Count'] / df[f'Sessions'].replace(0, 1)
    
    # New Derived Metrics 10-4-2024
    df[f'Impression_Share'] = df[f'Impressions'] / df[f'Impression_population'].replace(0, 1)  # Impression share
    
    return df

def calculate_percentage_changes(df, current_period_name, comparison_period_name, change_label):
    metrics = [
        'FF_Lead_Event_Count', 'FF_Purchase_Event_Count', 'FF_BRSubmit_Event_Count',
        'FF_DMSubmit_Event_Count', 'FF_PhoneGet_Event_Count', 'Housing Requests',
        'Total Traveler Actions', 'Traveler Value', 'Landlord Value', 'Sessions',
        'Impressions', 'Impression_population', 'Clicks', 'Cost', 'Lead_Conversion', 'CPL', 'CAC',
        'Traveler_Conversion', 'CPTA', 'ROAS', 'CPC', 'CTR', 'RPC_Landlord', 'RPC_Tenant',
        'Purchase_to_Sessions', 'Lead_to_Sessions', 'Impression_Share'
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
    df = standardize_date_column(df, 'Date')
    
    # Convert Date to datetime format
    df['Date'] = pd.to_datetime(df['Date'], format='%Y%m%d')

    # Filter for the date range for the current period
    df_period = df[(df['Date'] >= start_date) & (df['Date'] <= end_date)]

    # Group by Channel and Campaign Group and sum metrics
    metrics = [
        'FF_Lead_Event_Count', 'FF_Purchase_Event_Count', 'FF_BRSubmit_Event_Count',
        'FF_DMSubmit_Event_Count', 'FF_PhoneGet_Event_Count', 'Housing Requests',
        'Total Traveler Actions', 'Traveler Value', 'Landlord Value', 'Sessions',
        'Impressions', 'Impression_population','Clicks', 'Cost'
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

def calculate_adjusted_wtd_data(df, reference_date, period_name):
    """
    Calculate adjusted WTD or Full Week data by normalizing each ISO week in the DataFrame.
    If period_name is 'WTD', it limits each historical week to the same day of the week as the current week.
    If period_name is 'Full Week', it includes the entire week's data.
    """
    # Step 1: Convert the date column to datetime format, if not already
    df['Date'] = pd.to_datetime(df['Date'], format='%Y%m%d')

    # Step 2: Extract the ISO week, ISO year, and day of the week
    df['ISO_Year'] = df['Date'].dt.isocalendar().year
    df['ISO_Week'] = df['Date'].dt.isocalendar().week
    df['Day_of_Week'] = df['Date'].dt.dayofweek  # Monday=0, Sunday=6

    # Determine the last full ISO week and day of the week based on reference_date
    last_iso_year = reference_date.isocalendar().year
    last_iso_week = reference_date.isocalendar().week
    last_day_of_week = reference_date.weekday()

    # Step 3: Filter based on the period (WTD or Full Week)
    if period_name == 'WTD':
        # For WTD, filter historical weeks to match the day of the current reporting week
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
        
        # For all historical weeks, restrict to the same number of days as the current week
        df_filtered = df_filtered[
            (df_filtered['Day_of_Week'] <= last_day_of_week)
            ]
        
    elif period_name == 'Full Week':
        # For Full Week, we do not restrict by day of the week
        df_filtered = df[df['ISO_Year'] <= last_iso_year]
    
    # Step 4: Map "Device" to "Device type"
    device_type_mapping = {
        "desktop": "desktop",
        "DESKTOP": "desktop",
        "mobile": "mobile",
        "MOBILE": "mobile",
        "tablet": "tablet",
        "smart tv": "other",
        "CONNECTED_TV": "other",
        "OTHER": "other"
    }
    df_filtered['Device type'] = df_filtered['Device'].map(device_type_mapping)

    # Step 5: Add "Form factor" column
    df_filtered['Form factor'] = df_filtered['Device'].apply(
        lambda x: "MWeb" if x == "mobile" else "Non-mobile"
    )

    # Step 6: Remove unnecessary columns before aggregation
    columns_to_keep = [
        'ISO_Year', 'ISO_Week', 'Channel_Group', 'Campaign_Group', 'Device type', 'Form factor',
        'FF_Purchase_Event_Count', 'FF_Lead_Event_Count', 'FF_BRSubmit_Event_Count',
        'FF_DMSubmit_Event_Count', 'FF_PhoneGet_Event_Count', 'FF_HRSubmit_Event_Count',
        'HR_Submit_Event_New_Traveler_Lead_Count', 'Housing Requests', 'Total Traveler Actions',
        'Traveler Value', 'Landlord Value', 'Impressions', 'Impression_population', 'Clicks', 'Cost', 'Sessions'
    ]
    df_filtered = df_filtered[columns_to_keep]

    # Step 7: Group the data by ISO Year, ISO Week, Channel Group, and Campaign Group
    week_summary = df_filtered.groupby(
        ['ISO_Year', 'ISO_Week', 'Channel_Group', 'Campaign_Group', 'Device type', 'Form factor']
    ).sum().reset_index()

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
        'Impressions', 'Impression_population', 'Clicks', 'Cost', 'Sessions', 'Lead_Conversion', 'CPL', 'CAC',
        'Traveler_Conversion', 'CPTA', 'ROAS', 'CPC', 'CTR', 'RPC_Landlord', 'RPC_Tenant',
        'Purchase_to_Sessions', 'Lead_to_Sessions', 'Impression_Share'
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
        current_period = row['Period']  # 'WTD' or 'Full Week'

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
        previous_week_row = df[
            (df['ISO_Year'] == previous_year) & 
            (df['ISO_Week'] == previous_week) & 
            (df['Channel_Group'] == row['Channel_Group']) & 
            (df['Campaign_Group'] == row['Campaign_Group']) & 
            #(df['Device type'] == row['Device type']) &
            (df['Period'] == current_period)  # Ensure we are comparing the same period (e.g., WTD with WTD)
            ]

        yoy_row = df[
            (df['ISO_Year'] == yoy_year) & 
            (df['ISO_Week'] == yoy_week) & 
            (df['Channel_Group'] == row['Channel_Group']) & 
            (df['Campaign_Group'] == row['Campaign_Group']) &
            #(df['Device type'] == row['Device type']) &
            (df['Period'] == current_period)  # Ensure YoY comparison is with the same period type
            ]
        
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

# Define the periods we want to process
period_wtd_output = ['WTD', 'Full Week']

# Create an empty list to store the outputs from both periods
all_periods_output = []

for period in period_wtd_output:
    # Call the function to calculate WTD-adjusted data using the existing reference_date and period
    df_wtd_adjusted = calculate_adjusted_wtd_data(df, reference_date, period)
    
    # Add a "Period" column and assign the current period
    df_wtd_adjusted['Period'] = period
    
    # Append the output for this period to the list
    all_periods_output.append(df_wtd_adjusted)

# List of metrics that need to be set to zero in ISO Wk 26, ISO_Year 2023
metrics_to_zero = [
    'FF_Purchase_Event_Count', 'FF_Lead_Event_Count', 'FF_BRSubmit_Event_Count',
    'FF_DMSubmit_Event_Count', 'FF_PhoneGet_Event_Count', 'FF_HRSubmit_Event_Count',
    'HR_Submit_Event_New_Traveler_Lead_Count', 'Housing Requests', 'Total Traveler Actions',
    'Traveler Value', 'Landlord Value', 'Sessions', 'Lead_Conversion', 'CPL', 'CAC',
    'Traveler_Conversion', 'CPTA', 'ROAS', 'RPC_Landlord', 'RPC_Tenant',
    'Purchase_to_Sessions', 'Lead_to_Sessions'
]

# Get the ISO week and ISO year for the reference date (i.e., the current week)
current_iso_year = reference_date.isocalendar().year
current_iso_week = reference_date.isocalendar().week

# Concatenate the results from both periods
final_df_wtd_adjusted = pd.concat(all_periods_output, ignore_index=True)

# Add a new column "Total Revenue" that is the sum of "Traveler Value" and "Landlord Value"
final_df_wtd_adjusted['Total Revenue'] = final_df_wtd_adjusted['Traveler Value'] + final_df_wtd_adjusted['Landlord Value']

# Filter df_wtd_adjusted to only include data from 2023 and ISO week >= 1
final_df_wtd_adjusted = final_df_wtd_adjusted[(final_df_wtd_adjusted['ISO_Year'] >= 2023)]

# Verify the filtered result
print(final_df_wtd_adjusted[['ISO_Year', 'ISO_Week']].drop_duplicates())

# Set the values of specific metrics to zero for ISO Week 26 in ISO Year 2023
final_df_wtd_adjusted.loc[
    (final_df_wtd_adjusted['ISO_Week'] == 26) & (final_df_wtd_adjusted['ISO_Year'] == 2023), 
    metrics_to_zero
] = 0

# Optional: Flag this adjustment
final_df_wtd_adjusted.loc[
    (final_df_wtd_adjusted['ISO_Week'] == 26) & (final_df_wtd_adjusted['ISO_Year'] == 2023), 
    'Manual_Adjustment_Flag'
] = 'Adjusted'

# Remove all data for the current ISO week and year for the "Full Week" period
final_df_wtd_adjusted = final_df_wtd_adjusted[
    ~((final_df_wtd_adjusted['ISO_Year'] == current_iso_year) &
      (final_df_wtd_adjusted['ISO_Week'] == current_iso_week) &
      (final_df_wtd_adjusted['Period'] == 'Full Week'))
]

# Merge the flags with the aggregated weekly data (df_wtd_adjusted)
final_df_wtd_adjusted = final_df_wtd_adjusted.merge(incomplete_flags, on=['ISO_Year', 'ISO_Week'], how='left')

# Fill NaN flags with False (meaning the week was complete if no flag exists)
final_df_wtd_adjusted['Incomplete_Week'].fillna(False, inplace=True)
final_df_wtd_adjusted['Incomplete_Week'] = final_df_wtd_adjusted['Incomplete_Week'].fillna(False)

final_df_wtd_adjusted = calculate_derived_metrics(final_df_wtd_adjusted, 'WTD')

# Apply the function to calculate the nominal differences
final_df_wtd_adjusted = calculate_wow_yoy_differences(final_df_wtd_adjusted)

# # Define the CSV file path for the WTD-adjusted data
# wtd_csv_file_path = 'ga4-reports/output/ga4_wtd_adjusted_data.csv'

# # Write the adjusted WTD DataFrame to a CSV file
# df_wtd_adjusted.to_csv(wtd_csv_file_path, index=False)


# # Display a message indicating that the adjusted WTD data has been written to the CSV file
# print(f"Adjusted WTD data with WoW and YoY differences has been written to {wtd_csv_file_path}")

# Save the final concatenated DataFrame to CSV
final_wtd_csv_file_path = 'ga4-reports/output/ga4_wtd_fullweek_adjusted_data.csv'
final_df_wtd_adjusted.to_csv(final_wtd_csv_file_path, index=False)

# Display a message indicating that the adjusted data has been written to the CSV file
print(f"Adjusted WTD and Full Week data with WoW and YoY differences has been written to {final_wtd_csv_file_path}")