# file name: ga4-reports/fetch-google-analytics-session-table-delete-backfill_geo-data.py
# Version: V000-000-000
# output:
# Notes: First version of creating the "ga4_event_geo-data" table in sqlite

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

# Get current reference date (for example, last day of ISO week 31)
# reference_date = datetime(2024, 8, 11)  # This can be dynamically set
# Automate the calculation of the reference_date as the day before yesterday
reference_date = datetime.now() - timedelta(days=15)
reference_date = reference_date.replace(hour=0, minute=0, second=0, microsecond=0)

# Check if the table exists
cursor.execute("""
SELECT name FROM sqlite_master WHERE type='table' AND name='ga4_sessions_geo';
""")
table_exists = cursor.fetchone()

# Set the end date
delete_end_date = datetime.now() - timedelta(days=1)

# Set the start date based on whether the table exists
if table_exists is None:
    print("Table 'ga4_sessions_geo' does not exist. Skipping deletion step and retrieving data starting from July 1, 2023.")
    delete_start_date = datetime.strptime("2023-07-01", "%Y-%m-%d")
else:
    print("Table 'ga4_sessions_geo' exists. Retrieving data based on the reference date.")
    delete_start_date = reference_date
    print(f"Deleting data from {delete_start_date.strftime('%Y%m%d')} to {delete_end_date.strftime('%Y%m%d')}")
    cursor.execute("""
    DELETE FROM ga4_sessions_geo
    WHERE Date >= ? AND Date <= ?
    """, (delete_start_date.strftime("%Y%m%d"), delete_end_date.strftime("%Y%m%d")))
    conn.commit()


# Verify deletion
cursor.execute("""
SELECT COUNT(*)
FROM ga4_sessions_geo
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
    Dimension(name="deviceCategory"),
    Dimension(name="geoCity"),
    Dimension(name="geoRegion"),
    Dimension(name="geoCountry")
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
            "City": row.dimension_values[5].value,
            "State": row.dimension_values[6].value,
            "Country": row.dimension_values[7].value,
            "Sessions": int(row.metric_values[0].value),
            "TotalUsers": int(row.metric_values[1].value),
            "NewUsers": int(row.metric_values[2].value)
        })

    return data

# Iterate over each day in the date range to backfill data
current_date = delete_start_date
data_fetched = 0
try:
    while current_date <= delete_end_date:
        date_str = current_date.strftime("%Y-%m-%d")
        print(f"Fetching data for {date_str}...")

        try:
            # Fetch data for the current date
            data = fetch_data_for_date(date_str)
        except Exception as e:
            print(f"Error fetching data for {date_str}: {e}")
            current_date += timedelta(days=1)
            continue

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

        # Insert data into the new SQLite table
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
            data_fetched += 1

        # Move to the next day
        current_date += timedelta(days=1)

    # Commit all changes at once
    conn.commit()

    print(f"Number of rows inserted/updated: {data_fetched}")

except Exception as main_error:
    print(f"An error occurred during the backfill process: {main_error}")
finally:
    # Close the connection
    conn.close()