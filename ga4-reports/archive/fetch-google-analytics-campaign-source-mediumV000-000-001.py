# file name: ga4-reports/fetch-google-analytics-campaign-source-medium.py
# version: V000-000-001
# Note: Created new functions "FilterExpressionList" and "combined_filter" 



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

# Process the response
for row in response.rows:
    print(f"Date: {row.dimension_values[0].value}")
    print(f"Campaign: {row.dimension_values[1].value}")
    print(f"Source: {row.dimension_values[2].value}")
    print(f"Medium: {row.dimension_values[3].value}")
    print(f"Event Name: {row.dimension_values[4].value}")
    print(f"Sessions: {row.metric_values[0].value}")
    print(f"Users: {row.metric_values[1].value}")
    print(f"New Users: {row.metric_values[2].value}")
    print(f"Event Count: {row.metric_values[3].value}")
    print("\n")
