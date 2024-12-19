from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import RunReportRequest, DateRange, Dimension, Metric, FilterExpression, Filter
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
import pandas as pd
from datetime import datetime, timedelta
import sqlite3

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

def fetch_first_time_lead_sessions(start_date, end_date):
    """Fetch daily count of first-time sessions with the 'FF Lead' event."""
    request = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        dimensions=[Dimension(name="date"), Dimension(name="newVsReturning")],
        metrics=[Metric(name="sessions")],
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
        if row.dimension_values[1].value == "new":  # Check if it's a first-time session
            data.append({
                "Date": row.dimension_values[0].value,
                "First-Time Sessions with FF Lead": int(row.metric_values[0].value)
            })

    return pd.DataFrame(data)

def run_first_time_lead_sessions_report():
    start_date = '2023-01-01'  # Adjust as needed
    end_date = '2024-10-31'    # Adjust as needed

    data = fetch_first_time_lead_sessions(start_date, end_date)

    # Save to CSV
    output_file = "first_time_lead_sessions_report.csv"
    data.to_csv(output_file, index=False)
    print(f"First-time lead sessions report saved to {output_file}")

    # Optionally, save the data to an SQLite database
    conn = sqlite3.connect("ga4_data.db")
    data.to_sql("ga4_first_time_lead_sessions", conn, if_exists="replace", index=False)
    conn.close()
    print("First-time lead sessions data saved to SQLite database.")

# Run the function to generate the report
run_first_time_lead_sessions_report()
