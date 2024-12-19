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

def fetch_first_time_lead_sessions_with_grouping(start_date, end_date):
    """Fetch daily count of first-time sessions with 'FF Lead' event, including source, medium, and page path grouping."""
    request = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        dimensions=[
            Dimension(name="date"),
            Dimension(name="newVsReturning"),
            Dimension(name="sessionSource"),
            Dimension(name="sessionMedium"),
            Dimension(name="pagePathPlusQueryString")
        ],
        metrics=[Metric(name="sessions")],
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        dimension_filter=FilterExpression(
            filter=Filter(field_name="eventName", string_filter=Filter.StringFilter(value="FF Lead"))
        )
    )

    # Run the report
    response = client.run_report(request)
    print(response)

    # Process the response and store in a DataFrame with page path groupings
    data = []
    for row in response.rows:
        if row.dimension_values[1].value == "new":  # Only include first-time sessions
            page_path = row.dimension_values[4].value
            # Define page path groupings
            if page_path == "/":
                page_path_group = "Home_Page"
            elif "/property/" in page_path:
                page_path_group = "PDP"
            elif "/housing/" in page_path:
                page_path_group = "Housing_Search"
            elif "/corporate/housing/" in page_path:
                page_path_group = "Corporate_Housing_Search"
            else:
                page_path_group = "Other"

            # Append data with grouped page path
            data.append({
                "Date": row.dimension_values[0].value,
                "Session_Source": row.dimension_values[2].value,
                "Session_Medium": row.dimension_values[3].value,
                "Page_Path_Group": page_path_group,
                "First-Time_Sessions_with_FF_Lead": int(row.metric_values[0].value)
            })

    return pd.DataFrame(data)

def run_first_time_lead_sessions_with_grouping_report():
    start_date = '2023-01-01'  # Adjust as needed
    end_date = '2024-03-24'    # Adjust as needed

    data = fetch_first_time_lead_sessions_with_grouping(start_date, end_date)
    
    # data = data.rename(columns=lambda x: x.replace("-", "_").replace(" ", "_"))

    # Save to CSV
    output_file = "ga4-reports/output/first_time_lead_sessions_with_grouping_report.csv"
    data.to_csv(output_file, index=False)
    print(f"First-time lead sessions report with page path grouping saved to {output_file}")

    # Optionally, save the data to an SQLite database
    # conn = sqlite3.connect("ga4_data.db")
    # data.to_sql("ga4_first_time_lead_sessions_with_grouping", conn, if_exists="replace", index=False)
    # conn.close()
    # print("First-time lead sessions data with page path grouping saved to SQLite database.")

# Run the function to generate the report
run_first_time_lead_sessions_with_grouping_report()