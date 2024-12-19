# file name: ga4-reports/fetch-google-analytics-campaign-source-medium.py
# version: V000-000-002
# Note: This version builds on V000-000-001 and creates a sqlite database



import sqlite3
import pandas as pd
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import DateRange, Dimension, Metric, RunReportRequest, FilterExpression, Filter, FilterExpressionList
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
import os

# Replace with your GA4 property ID
PROPERTY_ID = '338470276'

# Set the path to your OAuth 2.0 client ID file
CLIENT_SECRET_FILE = 'authentication/client_secret_739322396993-c4tvnl9molcabaoag36iqu1di26cqhvf.apps.googleusercontent.com.json' #'path/to/your/client_secret.json'
SCOPES = ['https://www.googleapis.com/auth/analytics.readonly']

# Authenticate and get credentials
flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
credentials = flow.run_local_server(port=0)

# Initialize the client with credentials
client = BetaAnalyticsDataClient(credentials=credentials)

# Define the date range (last 14 days)
date_ranges = [DateRange(start_date="14daysAgo", end_date="yesterday")]

# Define the dimensions
dimensions = [
    Dimension(name="date"),
    Dimension(name="sessionCampaignName"),
    Dimension(name="sessionSource"),
    Dimension(name="sessionMedium"),
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
        "Event Name": row.dimension_values[4].value,
        "Event Count": int(row.metric_values[3].value),
    })

df = pd.DataFrame(data)

# Pivot the DataFrame to get the required format
pivot_df = df.pivot_table(
    index=["Date", "Source", "Medium", "Campaign"],
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

# Connect to SQLite database
conn = sqlite3.connect("ga4_data.db")
cursor = conn.cursor()

# Create table if it doesn't exist
cursor.execute("""
CREATE TABLE IF NOT EXISTS ga4_events (
    Date TEXT,
    Source TEXT,
    Medium TEXT,
    Campaign TEXT,
    FF_Purchase_Event_Count INTEGER,
    FF_Lead_Event_Count INTEGER,
    FF_BRSubmit_Event_Count INTEGER,
    FF_DMSubmit_Event_Count INTEGER,
    FF_PhoneGet_Event_Count INTEGER,
    PRIMARY KEY (Date, Source, Medium, Campaign)
)
""")

# Insert data into SQLite database
for index, row in pivot_df.iterrows():
    cursor.execute("""
    INSERT INTO ga4_events (Date, Source, Medium, Campaign, FF_Purchase_Event_Count, FF_Lead_Event_Count, FF_BRSubmit_Event_Count, FF_DMSubmit_Event_Count, FF_PhoneGet_Event_Count)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(Date, Source, Medium, Campaign) DO UPDATE SET
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
        row["FF Purchase Event Count"],
        row["FF Lead Event Count"],
        row["FF-BRSubmit Event Count"],
        row["FF-DMSubmit Event Count"],
        row["FF-PhoneGet Event Count"]
    ))

# Commit and close the connection
conn.commit()
conn.close()
