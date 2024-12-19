# file name: fetch-google-analytics-session-table.py
# Version: V000-000-000
# output:
# Notes: First version of creating the "ga4_sessions" table in sqlite

import sqlite3
import pandas as pd
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import DateRange, Dimension, Metric, RunReportRequest
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

# Get current reference date (for example, last day of ISO week 31)
# reference_date = datetime(2024, 8, 11)  # This can be dynamically set
# Automate the calculation of the reference_date as the day before yesterday
reference_date = datetime.now() - timedelta(days=15)
reference_date = reference_date.replace(hour=0, minute=0, second=0, microsecond=0)

# Define the start and end dates to delete and backfill
# delete_start_date = datetime.strptime("2024-09-20", "%Y-%m-%d")
delete_start_date = reference_date
delete_end_date = datetime.now() - timedelta(days=1)

# Delete existing data from July 15, 2024 to yesterday
print(f"Deleting data from {delete_start_date.strftime('%Y%m%d')} to {delete_end_date.strftime('%Y%m%d')}")
cursor.execute("""
DELETE FROM ga4_sessions
WHERE Date >= ? AND Date <= ?
""", (delete_start_date.strftime("%Y%m%d"), delete_end_date.strftime("%Y%m%d")))
conn.commit()

# Verify deletion
cursor.execute("""
SELECT COUNT(*)
FROM ga4_sessions
WHERE Date >= ? AND Date <= ?
""", (delete_start_date.strftime("%Y%m%d"), delete_end_date.strftime("%Y%m%d")))
deleted_count = cursor.fetchone()[0]
print(f"Number of rows remaining after deletion: {deleted_count}")

# Define the dimensions
dimensions = [
    Dimension(name="date"),
    Dimension(name="sessionSource"),
    Dimension(name="sessionMedium"),
    Dimension(name="sessionCampaignName"),
    Dimension(name="deviceCategory")
]

# Define the metrics
metrics = [
    Metric(name="sessions"),
    Metric(name="totalUsers"),
    Metric(name="newUsers")
]

# Function to fetch data for a specific date
def fetch_data_for_date(date_str):
    date_ranges = [DateRange(start_date=date_str, end_date=date_str)]

    # Create the request
    request = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        date_ranges=date_ranges,
        dimensions=dimensions,
        metrics=metrics
    )

    # Run the report
    response = client.run_report(request)

    # Process the response and store the data in a DataFrame
    data = []
    for row in response.rows:
        data.append({
            "Date": row.dimension_values[0].value.replace("-", ""),
            "Source": row.dimension_values[1].value,
            "Medium": row.dimension_values[2].value,
            "Campaign": row.dimension_values[3].value,
            "Device": row.dimension_values[4].value,
            "Sessions": int(row.metric_values[0].value),
            "TotalUsers": int(row.metric_values[1].value),
            "NewUsers": int(row.metric_values[2].value)
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
            "Source": "",
            "Medium": "",
            "Campaign": "",
            "Device": "",
            "Sessions": 0,
            "TotalUsers": 0,
            "NewUsers": 0
        }]

    df = pd.DataFrame(data)

    # Insert data into SQLite database
    for index, row in df.iterrows():
        cursor.execute("""
        INSERT INTO ga4_sessions (Date, Source, Medium, Campaign, Device, Sessions, TotalUsers, NewUsers)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(Date, Source, Medium, Campaign, Device) DO UPDATE SET
            Sessions = excluded.Sessions,
            TotalUsers = excluded.TotalUsers,
            NewUsers = excluded.NewUsers
        """, (
            row["Date"],
            row["Source"],
            row["Medium"],
            row["Campaign"],
            row["Device"],
            row["Sessions"],
            row["TotalUsers"],
            row["NewUsers"]
        ))

    # Commit after each day
    conn.commit()

    # Move to the next day
    current_date += timedelta(days=1)

# Close the connection
conn.close()
