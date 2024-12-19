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

# Create a new SQLite table to store the geographic data
cursor.execute("""
CREATE TABLE IF NOT EXISTS ga4_sessions_geo (
    Date TEXT,
    Source TEXT,
    Medium TEXT,
    Campaign TEXT,
    Device TEXT,
    City TEXT,
    State TEXT,
    Country TEXT,
    Sessions INTEGER,
    TotalUsers INTEGER,
    NewUsers INTEGER,
    PRIMARY KEY (Date, Source, Medium, Campaign, Device, City, State, Country)
)
""")

# Define the dimensions
dimensions = [
    Dimension(name="date"),
    Dimension(name="sessionSource"),
    Dimension(name="sessionMedium"),
    Dimension(name="sessionCampaignName"),
    Dimension(name="deviceCategory"),
    Dimension(name="City"),
    Dimension(name="Region"),
    Dimension(name="Country")
]

# Define the metrics
metrics = [
    Metric(name="sessions"),
    Metric(name="totalUsers"),
    Metric(name="newUsers")
]

# Function to fetch data for a specific date with a longer timeout
def fetch_data_for_date(date_str):
    date_ranges = [DateRange(start_date=date_str, end_date=date_str)]

    # Create the request
    request = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        date_ranges=date_ranges,
        dimensions=dimensions,
        metrics=metrics
    )

    # Run the report with a longer timeout
    response = client.run_report(request, timeout=300)  # 5 minutes timeout

    # Process the response and store the data in a DataFrame
    data = []
    for row in response.rows:
        data.append({
            "Date": row.dimension_values[0].value.replace("-", ""),
            "Source": row.dimension_values[1].value,
            "Medium": row.dimension_values[2].value,
            "Campaign": row.dimension_values[3].value,
            "Device": row.dimension_values[4].value,
            "City": row.dimension_values[5].value,
            "State": row.dimension_values[6].value,
            "Country": row.dimension_values[7].value,
            "Sessions": int(row.metric_values[0].value),
            "TotalUsers": int(row.metric_values[1].value),
            "NewUsers": int(row.metric_values[2].value)
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
    try:
        data = fetch_data_for_date(date_str)
        if not data:
            print(f"No data found for {date_str}, inserting zero counts...")
            data = [{
                "Date": current_date.strftime("%Y%m%d"),
                "Source": "",
                "Medium": "",
                "Campaign": "",
                "Device": "",
                "City": "",
                "State": "",
                "Country": "",
                "Sessions": 0,
                "TotalUsers": 0,
                "NewUsers": 0
            }]

        df = pd.DataFrame(data)

        # Insert data into SQLite database
        for index, row in df.iterrows():
            cursor.execute("""
            INSERT INTO ga4_sessions_geo (Date, Source, Medium, Campaign, Device, City, State, Country, Sessions, TotalUsers, NewUsers)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(Date, Source, Medium, Campaign, Device, City, State, Country) DO UPDATE SET
                Sessions = excluded.Sessions,
                TotalUsers = excluded.TotalUsers,
                NewUsers = excluded.NewUsers
            """, (
                row["Date"],
                row["Source"],
                row["Medium"],
                row["Campaign"],
                row["Device"],
                row["City"],
                row["State"],
                row["Country"],
                row["Sessions"],
                row["TotalUsers"],
                row["NewUsers"]
            ))

        # Commit after each day
        conn.commit()

    except Exception as e:
        print(f"Error fetching data for {date_str}: {e}")

    # Move to the next day
    current_date += timedelta(days=1)

# Close the connection
conn.close()
