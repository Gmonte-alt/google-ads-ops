# file name: fetch-google-analytics-default-channel-group-fact-table.py
# version: V000-000-000
# output:
# Notes: Default Channel Grouping - the initial data pull from ga4. To be updated with new logic in V000-000-001

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

# Drop table if it exists
cursor.execute("DROP TABLE IF EXISTS ga4_channel_groups")

# Create table for channel group lookup
cursor.execute("""
CREATE TABLE IF NOT EXISTS ga4_channel_groups (
    Source TEXT,
    Medium TEXT,
    Campaign TEXT,
    Default_Channel_Group TEXT
)
""")

# Define the dimensions
dimensions = [
    Dimension(name="sessionSource"),
    Dimension(name="sessionMedium"),
    Dimension(name="sessionCampaignName"),
    Dimension(name="defaultChannelGroup"),
]

# Define the metrics (even though we don't need them, it's required to include at least one)
metrics = [
    Metric(name="sessions")
]

# Define the function to fetch data and insert into SQLite
def fetch_and_store_channel_groups(start_date, end_date):
    date_ranges = [DateRange(start_date=start_date, end_date=end_date)]
    request = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        date_ranges=date_ranges,
        dimensions=dimensions,
        metrics=metrics
    )

    response = client.run_report(request)

    data = []
    for row in response.rows:
        data.append({
            "Source": row.dimension_values[0].value,
            "Medium": row.dimension_values[1].value,
            "Campaign": row.dimension_values[2].value,
            "Default_Channel_Group": row.dimension_values[3].value
        })

    df = pd.DataFrame(data)

    # Insert data into SQLite database
    for index, row in df.iterrows():
        cursor.execute("""
        INSERT INTO ga4_channel_groups (Source, Medium, Campaign, Default_Channel_Group)
        VALUES (?, ?, ?, ?)
        """, (
            row["Source"],
            row["Medium"],
            row["Campaign"],
            row["Default_Channel_Group"]
        ))

    conn.commit()

    # Write the DataFrame to a CSV file
    csv_output_path = 'ga4-reports/output/ga4_channel_groups.csv'
    df.to_csv(csv_output_path, index=False)
    print(f"Default channel groups have been stored in the database and exported to {csv_output_path}")

# Define the date range
start_date = "2023-01-01"
end_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

# Fetch and store channel groups for the defined date range
fetch_and_store_channel_groups(start_date, end_date)

# Close the connection
conn.close()
