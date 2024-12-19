# file name: ga4-reports/fetch-google-analytics-campaign-source-medium-unique-events.py
# version: V000-000-000
# Notes: copy of ga4-reports/fetch-google-analytics-campaign-source-medium.py

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

# Check if the new table exists and drop it if it does
cursor.execute("DROP TABLE IF EXISTS ga4_unique_events")

# Create new table with the unique event counts schema
cursor.execute("""
CREATE TABLE ga4_unique_events (
    Date TEXT,
    Source TEXT,
    Medium TEXT,
    Campaign TEXT,
    Device TEXT,
    FF_Purchase_Unique_Event_Count REAL,
    FF_Lead_Unique_Event_Count REAL,
    FF_BRSubmit_Unique_Event_Count REAL,
    FF_DMSubmit_Unique_Event_Count REAL,
    FF_PhoneGet_Unique_Event_Count REAL,
    Active_Users INTEGER,
    New_Users INTEGER,
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
    Dimension(name="eventName"),
]

# Define the metrics
metrics = [
    Metric(name="eventsPerSession"),  # Approximates unique event count within a session
    Metric(name="activeUsers"),  # Active users
    Metric(name="newUsers"),  # New users
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
            "Date": row.dimension_values[0].value,
            "Campaign": row.dimension_values[1].value,
            "Source": row.dimension_values[2].value,
            "Medium": row.dimension_values[3].value,
            "Device": row.dimension_values[4].value,
            "Event Name": row.dimension_values[5].value,
            "Unique Event Count": float(row.metric_values[0].value),  # Unique event count within a session
            "Active Users": int(row.metric_values[1].value),  # Active users
            "New Users": int(row.metric_values[2].value),  # New users
        })

    return data

# Define the start and end dates
start_date = datetime.strptime("2023-07-01", "%Y-%m-%d")
end_date = datetime.now() - timedelta(days=1)

# Iterate over each day in the date range
current_date = start_date
while current_date <= end_date:
    date_str = current_date.strftime("%Y-%m-%d")
    print(f"Fetching data for {date_str}...")

    # Fetch data for the current date
    data = fetch_data_for_date(date_str)
    if not data:
        print(f"No data found for {date_str}, inserting zero counts...")
        data = [{
            "Date": date_str,
            "Campaign": "",
            "Source": "",
            "Medium": "",
            "Device": "",
            "Event Name": event,
            "Unique Event Count": 0.0,
            "Active Users": 0,
            "New Users": 0,
        } for event in event_names]

    df = pd.DataFrame(data)

    # Pivot the DataFrame to get the required format
    pivot_df = df.pivot_table(
        index=["Date", "Source", "Medium", "Campaign", "Device"],
        columns="Event Name",
        values="Unique Event Count",
        aggfunc="sum",
        fill_value=0
    ).reset_index()

    # Add Active Users and New Users to the pivot DataFrame
    pivot_df["Active Users"] = df["Active Users"].max()
    pivot_df["New Users"] = df["New Users"].max()

    # Rename columns
    pivot_df.columns.name = None
    pivot_df.rename(columns={
        "FF Purchase": "FF Purchase Unique Event Count",
        "FF Lead": "FF Lead Unique Event Count",
        "FF-BRSubmit": "FF-BRSubmit Unique Event Count",
        "FF-DMSubmit": "FF-DMSubmit Unique Event Count",
        "FF-PhoneGet": "FF-PhoneGet Unique Event Count"
    }, inplace=True)

    # Ensure all necessary columns are present
    for col in ["FF Purchase Unique Event Count", "FF Lead Unique Event Count", "FF-BRSubmit Unique Event Count", "FF-DMSubmit Unique Event Count", "FF-PhoneGet Unique Event Count"]:
        if col not in pivot_df.columns:
            pivot_df[col] = 0.0

    # Insert data into SQLite database
    for index, row in pivot_df.iterrows():
        cursor.execute("""
        INSERT INTO ga4_unique_events (Date, Source, Medium, Campaign, Device, FF_Purchase_Unique_Event_Count, FF_Lead_Unique_Event_Count, FF_BRSubmit_Unique_Event_Count, FF_DMSubmit_Unique_Event_Count, FF_PhoneGet_Unique_Event_Count, Active_Users, New_Users)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(Date, Source, Medium, Campaign, Device) DO UPDATE SET
            FF_Purchase_Unique_Event_Count = FF_Purchase_Unique_Event_Count + excluded.FF_Purchase_Unique_Event_Count,
            FF_Lead_Unique_Event_Count = FF_Lead_Unique_Event_Count + excluded.FF_Lead_Unique_Event_Count,
            FF_BRSubmit_Unique_Event_Count = FF_BRSubmit_Unique_Event_Count + excluded.FF_BRSubmit_Unique_Event_Count,
            FF_DMSubmit_Unique_Event_Count = FF_DMSubmit_Unique_Event_Count + excluded.FF_DMSubmit_Unique_Event_Count,
            FF_PhoneGet_Unique_Event_Count = FF_PhoneGet_Unique_Event_Count + excluded.FF_PhoneGet_Unique_Event_Count,
            Active_Users = Active_Users + excluded.Active_Users,
            New_Users = New_Users + excluded.New_Users
        """, (
            row["Date"],
            row["Source"],
            row["Medium"],
            row["Campaign"],
            row["Device"],
            row["FF Purchase Unique Event Count"],
            row["FF Lead Unique Event Count"],
            row["FF-BRSubmit Unique Event Count"],
            row["FF-DMSubmit Unique Event Count"],
            row["FF-PhoneGet Unique Event Count"],
            row["Active Users"],
            row["New Users"]
        ))

    # Commit after each day
    conn.commit()

    # Move to the next day
    current_date += timedelta(days=1)

# Close the connection
conn.close()
