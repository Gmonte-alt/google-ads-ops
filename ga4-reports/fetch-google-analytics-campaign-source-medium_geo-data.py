# file name: ga4-reports/fetch-google-analytics-campaign-source-medium.py
# version: V000-000-001
# Note: This version builds on V000-000-006 to include a dynamic start date


import sqlite3
import pandas as pd
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import DateRange, Dimension, Metric, RunReportRequest, FilterExpression, Filter, FilterExpressionList
from google.api_core.retry import Retry
from google.api_core.timeout import ExponentialTimeout
from google.api_core.exceptions import Unknown, DeadlineExceeded, ServiceUnavailable
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
import os
from datetime import datetime, timedelta
import time

# Replace with your GA4 property ID
PROPERTY_ID = '338470276'

# Set the path to your OAuth 2.0 client ID file
CLIENT_SECRET_FILE = 'authentication/client_secret_739322396993-c4tvnl9molcabaoag36iqu1di26cqhvf.apps.googleusercontent.com.json'
SCOPES = ['https://www.googleapis.com/auth/analytics.readonly']

# Authenticate and get credentials
flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
credentials = flow.run_local_server(port=0)

# Initialize the client with credentials
client = BetaAnalyticsDataClient(credentials=credentials)

# Set batch size for commits
batch_size = 7
days_processed = 0  # Counter for processed days

# Define a longer timeout duration (e.g., 120 seconds)
timeout = ExponentialTimeout(initial=120)

# Connect to SQLite database
conn = sqlite3.connect("ga4_data.db")
cursor = conn.cursor()

# Check if the table exists and drop it if it does
cursor.execute("DROP TABLE IF EXISTS ga4_events_geo_data")

# Create table with the new schema
cursor.execute("""
CREATE TABLE ga4_events_geo_data (
    Date TEXT,
    Source TEXT,
    Medium TEXT,
    Campaign TEXT,
    Device TEXT,
    City TEXT,
    Region TEXT,
    Country TEXT,
    FF_Purchase_Event_Count INTEGER,
    FF_Lead_Event_Count INTEGER,
    FF_BRSubmit_Event_Count INTEGER,
    FF_DMSubmit_Event_Count INTEGER,
    FF_PhoneGet_Event_Count INTEGER,
    FF_HRSubmit_Event_Count INTEGER,
    HR_Submit_Event_New_Traveler_Lead_Count INTEGER,
    PRIMARY KEY (Date, Source, Medium, Campaign, Device)
)
""")

# Define the dimensions
dimensions = [
    Dimension(name="date"),
    Dimension(name="sessionCampaignName"),
    Dimension(name="sessionSource"),
    Dimension(name="sessionMedium"),
    Dimension(name="deviceCategory"),
    Dimension(name="city"),
    Dimension(name="region"),
    Dimension(name="country"),
    Dimension(name="eventName"),
]

# Define the metrics
metrics = [
    Metric(name="sessions"),
    Metric(name="totalUsers"),
    Metric(name="newUsers"),
    Metric(name="eventCount"),
]

# Define the filter for event names
event_names = ["FF Purchase", "FF Lead", "FF-BRSubmit", "FF-DMSubmit", "FF-PhoneGet", "FF-HRSubmit", "hr_submit_event_new_traveler_lead"]
event_name_filters = [
    FilterExpression(filter=Filter(field_name="eventName", string_filter=Filter.StringFilter(match_type=Filter.StringFilter.MatchType.EXACT, value=name)))
    for name in event_names
]

# Combine the filters using FilterExpressionList
combined_filter = FilterExpression(
    or_group=FilterExpressionList(expressions=event_name_filters)
)

# Function to fetch data for a specific date
def fetch_data_for_date(date_str, timeout=300):
    date_ranges = [DateRange(start_date=date_str, end_date=date_str)]

    # Create the request
    request = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        date_ranges=date_ranges,
        dimensions=dimensions,
        metrics=metrics,
        dimension_filter=combined_filter
    )

    # Run the report with a specified timeout
    response = client.run_report(request, timeout=timeout)

    # Process the response and store the data in a DataFrame
    data = []
    for row in response.rows:
       # print(row)
        data.append({
            "Date": row.dimension_values[0].value,
            "Campaign": row.dimension_values[1].value,
            "Source": row.dimension_values[2].value,
            "Medium": row.dimension_values[3].value,
            "Device": row.dimension_values[4].value,
            "City": row.dimension_values[5].value,
            "Region": row.dimension_values[6].value,
            "Country": row.dimension_values[7].value,
            "Event Name": row.dimension_values[8].value,
            "Event Count": int(row.metric_values[3].value),
        })

    return data

def fetch_data_with_retry(date_str, retries=5, timeout=300):
    for attempt in range(retries):
        try:
            return fetch_data_for_date(date_str, timeout=timeout)
        except (Unknown, DeadlineExceeded, ServiceUnavailable) as e:
            print(f"Attempt {attempt + 1} failed: {e}")
            if attempt < retries - 1:
                # Add jitter to backoff timing
                time.sleep(2 ** attempt + (time.time() % 1))  # Exponential backoff
            else:
                raise Exception(f"Failed to fetch data after {retries} attempts")

# Get current reference date (for example, last day of ISO week 31)
# reference_date = datetime(2024, 8, 11)  # This can be dynamically set
# Automate the calculation of the reference_date as the day before yesterday
reference_date = datetime.now() - timedelta(days=15)
reference_date = reference_date.replace(hour=0, minute=0, second=0, microsecond=0)

# Define the start and end dates
start_date = datetime.strptime("2024-09-01", "%Y-%m-%d")
#start_date = reference_date
end_date = datetime.now() - timedelta(days=1)

# Iterate over each day in the date range
current_date = start_date
while current_date <= end_date:
    date_str = current_date.strftime("%Y-%m-%d")
    print(f"Fetching data for {date_str}...")

    # Fetch data for the current date
    data = fetch_data_with_retry(date_str)
    if not data:
        print(f"No data found for {date_str}, inserting zero counts...")
        data = [{
            "Date": date_str,
            "Campaign": "",
            "Source": "",
            "Medium": "",
            "Device": "",
            "City": "",
            "Region": "",
            "Country": "",
            "Event Name": event,
            "Event Count": 0
        } for event in event_names]

    df = pd.DataFrame(data)

    # Pivot the DataFrame to get the required format
    pivot_df = df.pivot_table(
        index=["Date", "Source", "Medium", "Campaign", "Device", "City", "Region", "Country"],
        columns="Event Name",
        values="Event Count",
        aggfunc="sum",
        fill_value=0
    ).reset_index()

    # Rename columns
    pivot_df.columns.name = None
    pivot_df.rename(columns={
        "FF Purchase": "FF Purchase Event Count",
        "FF Lead": "FF Lead Event Count",
        "FF-BRSubmit": "FF-BRSubmit Event Count",
        "FF-DMSubmit": "FF-DMSubmit Event Count",
        "FF-PhoneGet": "FF-PhoneGet Event Count",
        "FF-HRSubmit": "FF-HRSubmit Event Count",
        "hr_submit_event_new_traveler_lead": "HR Submit Event New Traveler Lead Count"
    }, inplace=True)

    # Ensure all necessary columns are present
    for col in ["FF Purchase Event Count", "FF Lead Event Count", "FF-BRSubmit Event Count", "FF-DMSubmit Event Count", "FF-PhoneGet Event Count", "FF-HRSubmit Event Count", "HR Submit Event New Traveler Lead Count"]:
        if col not in pivot_df.columns:
            pivot_df[col] = 0

    # Insert data into SQLite database
    for index, row in pivot_df.iterrows():
        cursor.execute("""
        INSERT INTO ga4_events_geo_data (Date, Source, Medium, Campaign, Device, City, Region, Country, FF_Purchase_Event_Count, FF_Lead_Event_Count, FF_BRSubmit_Event_Count, FF_DMSubmit_Event_Count, FF_PhoneGet_Event_Count, FF_HRSubmit_Event_Count, HR_Submit_Event_New_Traveler_Lead_Count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(Date, Source, Medium, Campaign, Device) DO UPDATE SET
            FF_Purchase_Event_Count = FF_Purchase_Event_Count + excluded.FF_Purchase_Event_Count,
            FF_Lead_Event_Count = FF_Lead_Event_Count + excluded.FF_Lead_Event_Count,
            FF_BRSubmit_Event_Count = FF_BRSubmit_Event_Count + excluded.FF_BRSubmit_Event_Count,
            FF_DMSubmit_Event_Count = FF_DMSubmit_Event_Count + excluded.FF_DMSubmit_Event_Count,
            FF_PhoneGet_Event_Count = FF_PhoneGet_Event_Count + excluded.FF_PhoneGet_Event_Count,
            FF_HRSubmit_Event_Count = FF_HRSubmit_Event_Count + excluded.FF_HRSubmit_Event_Count,
            HR_Submit_Event_New_Traveler_Lead_Count = HR_Submit_Event_New_Traveler_Lead_Count + excluded.HR_Submit_Event_New_Traveler_Lead_Count
        """, (
            row["Date"],
            row["Source"],
            row["Medium"],
            row["Campaign"],
            row["Device"],
            row["City"],
            row["Region"],
            row["Country"],
            row["FF Purchase Event Count"],
            row["FF Lead Event Count"],
            row["FF-BRSubmit Event Count"],
            row["FF-DMSubmit Event Count"],
            row["FF-PhoneGet Event Count"],
            row["FF-HRSubmit Event Count"],
            row["HR Submit Event New Traveler Lead Count"]
        ))
        
    # Increment processed days counter
    days_processed += 1
    
    # Commit after every batch_size days
    if days_processed % batch_size == 0:
        conn.commit()
        print(f"Committed after processing {batch_size} days.")
        
    # Move to the next day
    current_date += timedelta(days=1)
    
# Final commit to save any remaining data
conn.commit()
print("Final commit complete.")

# Close the connection
conn.close()
