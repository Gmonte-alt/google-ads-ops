# file name:
# version:
# Notes: this script is to test hte ga4 api pull without overwritting the database

import pandas as pd
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import DateRange, Dimension, Metric, RunReportRequest, FilterExpression, Filter, FilterExpressionList
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
    Metric(name="sessions"),
    Metric(name="totalUsers"),
    Metric(name="newUsers"),
    Metric(name="eventCount"),
]

# Define the filter for event names
event_names = ["FF Purchase", "FF Lead", "FF-BRSubmit", "FF-DMSubmit", "FF-PhoneGet", "FF-HRSubmit", "hr_submit_event_new_traveler_lead"]
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
            "Event Count": int(row.metric_values[3].value),
        })

    return data

# Define the start and end dates
end_date = datetime.now() - timedelta(days=1)
start_date = end_date - timedelta(days=29)

# Fetch data for the date range
all_data = []
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
            "Event Count": 0
        } for event in event_names]

    all_data.extend(data)
    current_date += timedelta(days=1)

# Convert the collected data to a DataFrame
df = pd.DataFrame(all_data)

# Pivot the DataFrame to get the required format
pivot_df = df.pivot_table(
    index=["Date", "Source", "Medium", "Campaign", "Device"],
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
    "FF-PhoneGet": "FF-PhoneGet Event Count",
    "FF-HRSubmit": "FF-HRSubmit Event Count",
    "hr_submit_event_new_traveler_lead": "HR Submit Event New Traveler Lead Count"
}, inplace=True)

# Print the resulting DataFrame
print(pivot_df)
