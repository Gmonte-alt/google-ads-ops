# file name: ga4-reports/fetch-google-analytics-campaign-source-medium.py
# version: V000-000-001
# Note: This version now compensates for the database change to include device types
#       Root file "ga4-reports/fetch-google-analytics-campaign-source-mediumV000-000-004.py"

import sqlite3
import pandas as pd
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import DateRange, Dimension, Metric, RunReportRequest, FilterExpression, Filter, FilterExpressionList
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
import os
from datetime import datetime, timedelta

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

# Connect to SQLite database
conn = sqlite3.connect("ga4_data.db")
cursor = conn.cursor()

# Define the start and end dates to delete and backfill
delete_start_date = datetime.strptime("2024-07-15", "%Y-%m-%d")
delete_end_date = datetime.now() - timedelta(days=1)

# Delete existing data from July 15, 2024 to yesterday
print(f"Deleting data from {delete_start_date.strftime('%Y%m%d')} to {delete_end_date.strftime('%Y%m%d')}")
cursor.execute("""
DELETE FROM ga4_events
WHERE Date >= ? AND Date <= ?
""", (delete_start_date.strftime("%Y%m%d"), delete_end_date.strftime("%Y%m%d")))
conn.commit()

# Verify deletion
cursor.execute("""
SELECT COUNT(*)
FROM ga4_events
WHERE Date >= ? AND Date <= ?
""", (delete_start_date.strftime("%Y%m%d"), delete_end_date.strftime("%Y%m%d")))
deleted_count = cursor.fetchone()[0]
print(f"Number of rows remaining after deletion: {deleted_count}")

# Define the dimensions
dimensions = [
    Dimension(name="date"),
    Dimension(name="sessionCampaignName"),
    Dimension(name="sessionSource"),
    Dimension(name="sessionMedium"),
    Dimension(name="deviceCategory"),
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
event_names = ["FF Purchase", "FF Lead", "FF-BRSubmit", "FF-DMSubmit", "FF-PhoneGet"]
event_name_filters = [
    FilterExpression(filter=Filter(field_name="eventName", string_filter=Filter.StringFilter(match_type=Filter.StringFilter.MatchType.EXACT, value=name)))
    for name in event_names
]

# Combine the filters using FilterExpressionList
combined_filter = FilterExpression(
    or_group=FilterExpressionList(expressions=event_name_filters)
)

# Function to fetch data for a specific date
def fetch_data_for_date(date_str):
    date_ranges = [DateRange(start_date=date_str, end_date=date_str)]

    # Create the request
    request = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        date_ranges=date_ranges,
        dimensions=dimensions,
        metrics=metrics,
        dimension_filter=combined_filter
    )

    # Run the report
    response = client.run_report(request)

    # Process the response and store the data in a DataFrame
    data = []
    for row in response.rows:
        data.append({
            "Date": row.dimension_values[0].value.replace("-", ""),
            "Campaign": row.dimension_values[1].value,
            "Source": row.dimension_values[2].value,
            "Medium": row.dimension_values[3].value,
            "Device": row.dimension_values[4].value,
            "Event Name": row.dimension_values[5].value,
            "Event Count": int(row.metric_values[3].value),
        })

    return data

# Iterate over each day in the date range to backfill data
current_date = delete_start_date
while current_date <= delete_end_date:
    date_str = current_date.strftime("%Y-%m-%d")
    print(f"Fetching data for {date_str}...")

    # Fetch data for the current date
    data = fetch_data_for_date(date_str)
    if not data:
        print(f"No data found for {date_str}, inserting zero counts...")
        data = [{
            "Date": current_date.strftime("%Y%m%d"),
            "Campaign": "",
            "Source": "",
            "Medium": "",
            "Device": "",
            "Event Name": event,
            "Event Count": 0
        } for event in event_names]

    df = pd.DataFrame(data)

    # Pivot the DataFrame to get the required format
    pivot_df = df.pivot_table(
        index=["Date", "Source", "Medium", "Campaign", "Device"],
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
        "FF-PhoneGet": "FF-PhoneGet Event Count"
    }, inplace=True)

    # Ensure all necessary columns are present
    for col in ["FF Purchase Event Count", "FF Lead Event Count", "FF-BRSubmit Event Count", "FF-DMSubmit Event Count", "FF-PhoneGet Event Count"]:
        if col not in pivot_df.columns:
            pivot_df[col] = 0

    # Insert data into SQLite database
    for index, row in pivot_df.iterrows():
        cursor.execute("""
        INSERT INTO ga4_events (Date, Source, Medium, Campaign, Device, FF_Purchase_Event_Count, FF_Lead_Event_Count, FF_BRSubmit_Event_Count, FF_DMSubmit_Event_Count, FF_PhoneGet_Event_Count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(Date, Source, Medium, Campaign, Device) DO UPDATE SET
            FF_Purchase_Event_Count = FF_Purchase_Event_Count + excluded.FF_Purchase_Event_Count,
            FF_Lead_Event_Count = FF_Lead_Event_Count + excluded.FF_Lead_Event_Count,
            FF_BRSubmit_Event_Count = FF_BRSubmit_Event_Count + excluded.FF_BRSubmit_Event_Count,
            FF_DMSubmit_Event_Count = FF_DMSubmit_Event_Count + excluded.FF_DMSubmit_Event_Count,
            FF_PhoneGet_Event_Count = FF_PhoneGet_Event_Count + excluded.FF_PhoneGet_Event_Count
        """, (
            row["Date"],
            row["Source"],
            row["Medium"],
            row["Campaign"],
            row["Device"],
            row["FF Purchase Event Count"],
            row["FF Lead Event Count"],
            row["FF-BRSubmit Event Count"],
            row["FF-DMSubmit Event Count"],
            row["FF-PhoneGet Event Count"]
        ))

    # Commit after each day
    conn.commit()

    # Move to the next day
    current_date += timedelta(days=1)

# Close the connection
conn.close()
