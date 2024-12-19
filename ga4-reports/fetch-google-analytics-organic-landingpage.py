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

# Create table for organic medium data if it doesn't exist
cursor.execute("""
CREATE TABLE IF NOT EXISTS ga4_organic_events (
    Date TEXT,
    LandingPage TEXT,
    Sessions INTEGER,
    TotalUsers INTEGER,
    NewUsers INTEGER,
    FF_Purchase_Event_Count INTEGER,
    FF_Lead_Event_Count INTEGER,
    FF_BRSubmit_Event_Count INTEGER,
    FF_DMSubmit_Event_Count INTEGER,
    FF_PhoneGet_Event_Count INTEGER,
    PRIMARY KEY (Date, LandingPage)
)
""")

# Define the dimensions
dimensions = [
    Dimension(name="date"),
    Dimension(name="pagePathPlusQueryString"),
    Dimension(name="sessionMedium"),
    Dimension(name="eventName")
]

# Define the metrics
metrics = [
    Metric(name="sessions"),
    Metric(name="totalUsers"),
    Metric(name="newUsers"),
    Metric(name="eventCount"),
]

# Define the filter for medium and event names
medium_filter = FilterExpression(
    filter=Filter(field_name="sessionMedium", string_filter=Filter.StringFilter(match_type=Filter.StringFilter.MatchType.EXACT, value="organic"))
)

event_names = ["FF Purchase", "FF Lead", "FF-BRSubmit", "FF-DMSubmit", "FF-PhoneGet"]
event_name_filters = [
    FilterExpression(filter=Filter(field_name="eventName", string_filter=Filter.StringFilter(match_type=Filter.StringFilter.MatchType.EXACT, value=name)))
    for name in event_names
]

# Combine the filters using FilterExpressionList
combined_filter = FilterExpression(
    and_group=FilterExpressionList(expressions=[medium_filter] + event_name_filters)
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
        event_name = row.dimension_values[3].value
        data.append({
            "Date": row.dimension_values[0].value.replace("-", ""),
            "LandingPage": row.dimension_values[1].value,
            "Sessions": int(row.metric_values[0].value),
            "TotalUsers": int(row.metric_values[1].value),
            "NewUsers": int(row.metric_values[2].value),
            "EventName": event_name,
            "EventCount": int(row.metric_values[3].value)
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

        # Pivot the DataFrame to get event counts by event name
        pivot_df = df.pivot_table(
            index=["Date", "LandingPage", "Sessions", "TotalUsers", "NewUsers"],
            columns="EventName",
            values="EventCount",
            aggfunc="sum",
            fill_value=0
        ).reset_index()

        # Rename columns
        pivot_df.columns.name = None
        pivot_df.rename(columns={
            "FF Purchase": "FF_Purchase_Event_Count",
            "FF Lead": "FF_Lead_Event_Count",
            "FF-BRSubmit": "FF_BRSubmit_Event_Count",
            "FF-DMSubmit": "FF_DMSubmit_Event_Count",
            "FF-PhoneGet": "FF_PhoneGet_Event_Count"
        }, inplace=True)

        # Ensure all necessary columns are present
        for col in ["FF_Purchase_Event_Count", "FF_Lead_Event_Count", "FF_BRSubmit_Event_Count", "FF_DMSubmit_Event_Count", "FF_PhoneGet_Event_Count"]:
            if col not in pivot_df.columns:
                pivot_df[col] = 0

        # Insert data into SQLite database
        for index, row in pivot_df.iterrows():
            cursor.execute("""
            INSERT INTO ga4_organic_events (Date, LandingPage, Sessions, TotalUsers, NewUsers, FF_Purchase_Event_Count, FF_Lead_Event_Count, FF_BRSubmit_Event_Count, FF_DMSubmit_Event_Count, FF_PhoneGet_Event_Count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(Date, LandingPage) DO UPDATE SET
                Sessions = excluded.Sessions,
                TotalUsers = excluded.TotalUsers,
                NewUsers = excluded.NewUsers,
                FF_Purchase_Event_Count = excluded.FF_Purchase_Event_Count,
                FF_Lead_Event_Count = excluded.FF_Lead_Event_Count,
                FF_BRSubmit_Event_Count = excluded.FF_BRSubmit_Event_Count,
                FF_DMSubmit_Event_Count = excluded.FF_DMSubmit_Event_Count,
                FF_PhoneGet_Event_Count = excluded.FF_PhoneGet_Event_Count
            """, (
                row["Date"],
                row["LandingPage"],
                row["Sessions"],
                row["TotalUsers"],
                row["NewUsers"],
                row["FF_Purchase_Event_Count"],
                row["FF_Lead_Event_Count"],
                row["FF_BRSubmit_Event_Count"],
                row["FF_DMSubmit_Event_Count"],
                row["FF_PhoneGet_Event_Count"]
            ))

    # Commit after each day
    conn.commit()

    # Move to the next day
    current_date += timedelta(days=1)

# Close the connection
conn.close()
