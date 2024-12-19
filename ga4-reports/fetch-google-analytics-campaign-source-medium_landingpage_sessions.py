# file name: ga4-reports/fetch-google-analytics-campaign-source-medium.py
# version: V000-000-007
# Note: This version builds on V000-000-006 to include a dynamic start date


import sqlite3
import pandas as pd
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import DateRange, Dimension, Metric, RunReportRequest, FilterExpression, Filter, FilterExpressionList
from google.api_core.retry import Retry
from google.api_core.timeout import ExponentialTimeout
from google.api_core.exceptions import Unknown
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
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

# Define a longer timeout duration (e.g., 120 seconds)
timeout = ExponentialTimeout(initial=120)

# Connect to SQLite database
conn = sqlite3.connect("ga4_data.db")
cursor = conn.cursor()

# Create a new table to store landing page data
cursor.execute("DROP TABLE IF EXISTS ga4_landingpage_session_data")
cursor.execute("""
CREATE TABLE ga4_landingpage_session_data (
    Date TEXT,
    LandingPage TEXT,
    Source TEXT,
    Medium TEXT,
    Campaign TEXT,
    Device TEXT,
    Sessions INTEGER,
    TotalUsers INTEGER,
    NewUsers INTEGER,
    Pageviews INTEGER,
    PRIMARY KEY (Date, LandingPage, Source, Medium, Campaign, Device)
)
""")

# Define dimensions and metrics for landing page data
dimensions = [
    Dimension(name="date"),
    Dimension(name="landingPage"),  # Track metrics by landing page
    Dimension(name="sessionSource"),
    Dimension(name="sessionMedium"),
    Dimension(name="sessionCampaignName"),
    Dimension(name="deviceCategory")
]

metrics = [
    Metric(name="sessions"),
    Metric(name="totalUsers"),
    Metric(name="newUsers"),
    Metric(name="screenPageViews")  # Equivalent to Pageviews in GA4
]

# Function to fetch data for a specific date
def fetch_landingpage_data_for_date(date_str):
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
            "Date": row.dimension_values[0].value,
            "LandingPage": row.dimension_values[1].value,
            "Source": row.dimension_values[2].value,
            "Medium": row.dimension_values[3].value,
            "Campaign": row.dimension_values[4].value,
            "Device": row.dimension_values[5].value,
            "Sessions": int(row.metric_values[0].value),
            "TotalUsers": int(row.metric_values[1].value),
            "NewUsers": int(row.metric_values[2].value),
            "Pageviews": int(row.metric_values[3].value)
        })

    return data

# Iterate over each day in the date range
start_date = datetime.strptime("2023-07-01", "%Y-%m-%d")
end_date = datetime.now() - timedelta(days=1)
current_date = start_date

while current_date <= end_date:
    date_str = current_date.strftime("%Y-%m-%d")
    print(f"Fetching landing page data for {date_str}...")

    # Fetch data for the current date
    data = fetch_landingpage_data_for_date(date_str)
    if not data:
        print(f"No data found for {date_str}. Inserting zero values...")
        data = [{
            "Date": date_str,
            "LandingPage": "",
            "Source": "",
            "Medium": "",
            "Campaign": "",
            "Device": "",
            "Sessions": 0,
            "TotalUsers": 0,
            "NewUsers": 0,
            "Pageviews": 0
        }]

    df = pd.DataFrame(data)

    # Insert data into SQLite database
    for index, row in df.iterrows():
        cursor.execute("""
        INSERT INTO ga4_landingpage_session_data (Date, LandingPage, Source, Medium, Campaign, Device, Sessions, TotalUsers, NewUsers, Pageviews)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(Date, LandingPage, Source, Medium, Campaign, Device) DO UPDATE SET
            Sessions = Sessions + excluded.Sessions,
            TotalUsers = TotalUsers + excluded.TotalUsers,
            NewUsers = NewUsers + excluded.NewUsers,
            Pageviews = Pageviews + excluded.Pageviews
        """, (
            row["Date"],
            row["LandingPage"],
            row["Source"],
            row["Medium"],
            row["Campaign"],
            row["Device"],
            row["Sessions"],
            row["TotalUsers"],
            row["NewUsers"],
            row["Pageviews"]
        ))

    # Commit after each day
    conn.commit()

    # Move to the next day
    current_date += timedelta(days=1)

# Close the connection
conn.close()