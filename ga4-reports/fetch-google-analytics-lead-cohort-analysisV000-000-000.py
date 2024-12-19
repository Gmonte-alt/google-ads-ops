import sqlite3
import pandas as pd
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import RunReportRequest, DateRange, Dimension, Metric, FilterExpression, Filter
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
import pandas as pd
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

def fetch_weekly_data(start_date, end_date):
    """Fetch weekly user data for the 'FF Lead' event."""
    request = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        dimensions=[Dimension(name="week")],
        metrics=[Metric(name="activeUsers")],
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        dimension_filter=FilterExpression(
            filter=Filter(field_name="eventName", string_filter=Filter.StringFilter(value="FF Lead"))
        )
    )

    # Run the report
    response = client.run_report(request)

    # Process the response and store in a DataFrame
    data = []
    for row in response.rows:
        data.append({
            "Week": row.dimension_values[0].value,
            "Active Users": int(row.metric_values[0].value)
        })

    return pd.DataFrame(data)

# Define weekly date ranges and aggregate results
def run_weekly_event_report():
    start_date = datetime(2023, 1, 1)
    end_date = datetime(2024, 3, 24)
    current_date = start_date
    final_data = pd.DataFrame()

    while current_date <= end_date:
        week_end = current_date + timedelta(days=6)
        week_data = fetch_weekly_data(current_date.strftime("%Y-%m-%d"), week_end.strftime("%Y-%m-%d"))
        final_data = pd.concat([final_data, week_data])
        current_date += timedelta(weeks=1)

    # Save to CSV
    output_file = "ga4-reports/output/weekly_ff_lead_event_report.csv"
    final_data.to_csv(output_file, index=False)
    print(f"Weekly event report saved to {output_file}")

# Run the function to generate weekly reports
run_weekly_event_report()
