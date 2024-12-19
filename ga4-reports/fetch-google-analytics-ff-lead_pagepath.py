import sqlite3
import pandas as pd
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import DateRange, Dimension, Metric, RunReportRequest, FilterExpression, Filter
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
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

# Create a new table to store "FF Lead" event data by page
cursor.execute("DROP TABLE IF EXISTS ga4_ff_lead_page_data")
cursor.execute("""
CREATE TABLE ga4_ff_lead_page_data (
    Date TEXT,
    PagePath TEXT,
    Source TEXT,
    Medium TEXT,
    Campaign TEXT,
    Device TEXT,
    EventCount INTEGER,
    PRIMARY KEY (Date, PagePath, Source, Medium, Campaign, Device)
)
""")

# Define dimensions and metrics for "FF Lead" event
dimensions = [
    Dimension(name="date"),
    Dimension(name="pagePath"),  # Track specific pages
    Dimension(name="sessionSource"),
    Dimension(name="sessionMedium"),
    Dimension(name="sessionCampaignName"),
    Dimension(name="deviceCategory")
]

metrics = [
    Metric(name="eventCount")  # Count the number of "FF Lead" events
]

# Define a filter to focus only on the "FF Lead" event
event_name_filter = FilterExpression(
    filter=Filter(field_name="eventName", string_filter=Filter.StringFilter(match_type=Filter.StringFilter.MatchType.EXACT, value="FF Lead"))
)

# Function to fetch data for a specific date
def fetch_ff_lead_data_for_date(date_str):
    date_ranges = [DateRange(start_date=date_str, end_date=date_str)]

    # Create the request
    request = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        date_ranges=date_ranges,
        dimensions=dimensions,
        metrics=metrics,
        dimension_filter=event_name_filter
    )

    # Run the report
    response = client.run_report(request)

    # Process the response and store the data in a DataFrame
    data = []
    for row in response.rows:
        data.append({
            "Date": row.dimension_values[0].value,
            "PagePath": row.dimension_values[1].value,
            "Source": row.dimension_values[2].value,
            "Medium": row.dimension_values[3].value,
            "Campaign": row.dimension_values[4].value,
            "Device": row.dimension_values[5].value,
            "EventCount": int(row.metric_values[0].value)
        })

    return data

# Iterate over each day in the date range
start_date = datetime.strptime("2024-08-01", "%Y-%m-%d")
end_date = datetime.now() - timedelta(days=1)
current_date = start_date

while current_date <= end_date:
    date_str = current_date.strftime("%Y-%m-%d")
    print(f"Fetching 'FF Lead' data for {date_str}...")

    # Fetch data for the current date
    data = fetch_ff_lead_data_for_date(date_str)
    if not data:
        print(f"No data found for {date_str}. Inserting zero values...")
        data = [{
            "Date": date_str,
            "PagePath": "",
            "Source": "",
            "Medium": "",
            "Campaign": "",
            "Device": "",
            "EventCount": 0
        }]

    df = pd.DataFrame(data)

    # Insert data into SQLite database
    for index, row in df.iterrows():
        cursor.execute("""
        INSERT INTO ga4_ff_lead_page_data (Date, PagePath, Source, Medium, Campaign, Device, EventCount)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(Date, PagePath, Source, Medium, Campaign, Device) DO UPDATE SET
            EventCount = EventCount + excluded.EventCount
        """, (
            row["Date"],
            row["PagePath"],
            row["Source"],
            row["Medium"],
            row["Campaign"],
            row["Device"],
            row["EventCount"]
        ))

    # Commit after each day
    conn.commit()

    # Move to the next day
    current_date += timedelta(days=1)

# Close the connection
conn.close()
