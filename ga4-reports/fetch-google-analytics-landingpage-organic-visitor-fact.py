# file name: ga4-reports/fetch-google-analytics-landingpage-organic-visitor-fact.py
# version:
# Notes:

import sqlite3
import pandas as pd
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import DateRange, Dimension, Metric, RunReportRequest, FilterExpression, Filter
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

# Create table for sessions and users data if it doesn't exist
cursor.execute("""
CREATE TABLE IF NOT EXISTS ga4_sessions_users_landingpage_visitor_fact_organic (
    Date TEXT,
    SessionID TEXT,
    UserID TEXT,
    LandingPage TEXT,
    Sessions INTEGER,
    TotalUsers INTEGER,
    NewUsers INTEGER,
    PRIMARY KEY (Date, SessionID, UserID)
)
""")

# Define the dimensions
dimensions = [
    Dimension(name="date"),
    Dimension(name="sessionId"),
    Dimension(name="userId"),
    Dimension(name="pagePathPlusQueryString"),
    Dimension(name="sessionMedium")
]

# Define the metrics
metrics = [
    Metric(name="sessions"),
    Metric(name="totalUsers"),
    Metric(name="newUsers")
]

# Define the filter for medium
medium_filter = FilterExpression(
    filter=Filter(field_name="sessionMedium", string_filter=Filter.StringFilter(match_type=Filter.StringFilter.MatchType.EXACT, value="organic"))
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
        dimension_filter=medium_filter
    )

    # Run the report
    response = client.run_report(request)

    # Process the response and store the data in a DataFrame
    data = []
    for row in response.rows:
        data.append({
            "Date": row.dimension_values[0].value.replace("-", ""),
            "SessionID": row.dimension_values[1].value,
            "UserID": row.dimension_values[2].value,
            "LandingPage": row.dimension_values[3].value,
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
    data = fetch_data_for_date(date_str)
    if data:
        df = pd.DataFrame(data)

        # Insert data into SQLite database
        for index, row in df.iterrows():
            cursor.execute("""
            INSERT INTO ga4_sessions_users_landingpage_visitor_fact_organic (Date, SessionID, UserID, LandingPage, Sessions, TotalUsers, NewUsers)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(Date, SessionID, UserID) DO UPDATE SET
                Sessions = excluded.Sessions,
                TotalUsers = excluded.TotalUsers,
                NewUsers = excluded.NewUsers
            """, (
                row["Date"],
                row["SessionID"],
                row["UserID"],
                row["LandingPage"],
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
