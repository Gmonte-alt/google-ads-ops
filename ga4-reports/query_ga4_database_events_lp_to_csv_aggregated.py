import sqlite3
import pandas as pd
from datetime import datetime, timedelta

# Connect to the SQLite database
conn = sqlite3.connect("ga4_data.db")

# Define a query to select data from the ga4_events_landingpage table
query = """
SELECT
    Date,
    Source,
    Medium,
    Campaign,
    Device,
    LandingPage,
    FF_Purchase_Event_Count,
    FF_Lead_Event_Count,
    FF_BRSubmit_Event_Count,
    FF_DMSubmit_Event_Count,
    FF_PhoneGet_Event_Count,
    FF_HRSubmit_Event_Count,
    HR_Submit_Event_New_Traveler_Lead_Count
FROM
    ga4_events_landingpage
"""

# Execute the query and fetch the results into a Pandas DataFrame
df = pd.read_sql_query(query, conn)

# Close the database connection as we no longer need it
conn.close()

# Convert Date column to datetime format
df['Date'] = pd.to_datetime(df['Date'], format='%Y%m%d')

# Calculate Date Groupings
df['ISO_Year'] = df['Date'].dt.isocalendar().year
df['ISO_Week'] = df['Date'].dt.isocalendar().week
df['ISO_Week_Start_Date'] = df['Date'] - pd.to_timedelta(df['Date'].dt.weekday, unit='D')

# Apply Landing Page Groupings
def landing_page_group(row):
    landing_page = row['LandingPage']
    if "/corporate/" in landing_page:
        return "./corporate/"
    elif "/housing/" in landing_page:
        return "./housing/"
    elif not landing_page or landing_page == "/":
        return ".Home"
    elif "/list-your-property" in landing_page and "/list-your-property-payment" not in landing_page:
        return ".LYP"
    elif "/property/" in landing_page:
        return ".Property"
    elif "/members/" in landing_page:
        return "./members/"
    elif "/list-your-property-payment" in landing_page:
        return ".LYP Payment"
    else:
        return ".Other"

df['LandingPage_Group'] = df.apply(landing_page_group, axis=1)

# Apply Channel Groupings
def channel_group(row):
    source = row['Source']
    medium = row['Medium']
    if source == '(direct)' and medium == '(none)':
        return 'Direct'
    elif source == '(not set)' and medium == '(not set)':
        return 'Undefined'
    elif medium == 'organic':
        return 'SEO'
    elif source in ['facebook', 'google', 'bing'] and medium == 'cpc':
        return 'Paid'
    elif source == 'travelnursehousing.com' and medium == 'referral':
        return 'Paid'
    else:
        return 'Other'

df['Channel_Group'] = df.apply(channel_group, axis=1)

# Apply Device Grouping
df['Device_Group'] = df['Device'].apply(lambda x: 'MOBILE' if x.lower() == 'mobile' else 'NON-MOBILE')

# Aggregate data based on the new groupings
grouped_df = df.groupby(
    ['ISO_Year', 'ISO_Week', 'ISO_Week_Start_Date', 'LandingPage_Group', 'Channel_Group', 'Device_Group']
).agg({
    'FF_Purchase_Event_Count': 'sum',
    'FF_Lead_Event_Count': 'sum',
    'FF_BRSubmit_Event_Count': 'sum',
    'FF_DMSubmit_Event_Count': 'sum',
    'FF_PhoneGet_Event_Count': 'sum',
    'FF_HRSubmit_Event_Count': 'sum',
    'HR_Submit_Event_New_Traveler_Lead_Count': 'sum'
}).reset_index()

# Define the CSV file path for the aggregated output
csv_file_path = "ga4-reports/output/ga4_events_lp_aggregated.csv"

# Write the aggregated DataFrame to a CSV file
grouped_df.to_csv(csv_file_path, index=False)

# Display a message indicating that the data has been written to the CSV file
print(f"Aggregated data has been written to {csv_file_path}")
