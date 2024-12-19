# file name: ga4-reports/fetch-google-analytics-events-organic-visitor-fact.py
# version:
# Notes:

# Reconnect to SQLite database for the next operation
conn = sqlite3.connect("ga4_data.db")
cursor = conn.cursor()

# Create table for custom events data if it doesn't exist
cursor.execute("""
CREATE TABLE IF NOT EXISTS ga4_custom_events_visitor_fact_organic (
    Date TEXT,
    SessionID TEXT,
    UserID TEXT,
    EventName TEXT,
    EventCount INTEGER,
    PRIMARY KEY (Date, SessionID, UserID, EventName)
)
""")

# Define the dimensions
dimensions = [
    Dimension(name="date"),
    Dimension(name="sessionId"),
    Dimension(name="userId"),
    Dimension(name="eventName")
]

# Define the metrics
metrics = [
    Metric(name="eventCount")
]

# Define the filter for custom event names
event_name_filters = [
    FilterExpression(filter=Filter(field_name="eventName", string_filter=Filter.StringFilter(match_type=Filter.StringFilter.MatchType.EXACT, value=name)))
    for name in event_names
]

# Combine the filters using FilterExpressionList
combined_filter = FilterExpression(
    or_group=FilterExpressionList(expressions=event_name_filters)
)

# Function to fetch custom event data for a specific date
def fetch_custom_event_data_for_date(date_str):
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
            "Date": row.dimension_values[0].value.replace("-", ""),
            "SessionID": row.dimension_values[1].value,
            "UserID": row.dimension_values[2].value,
            "EventName": row.dimension_values[3].value,
            "EventCount": int(row.metric_values[0].value)
        })

    return data

# Iterate over each day in the date range
current_date = start_date
while current_date <= end_date:
    date_str = current_date.strftime("%Y-%m-%d")
    print(f"Fetching custom event data for {date_str}...")

    # Fetch custom event data for the current date
    custom_data = fetch_custom_event_data_for_date(date_str)
    if custom_data:
        df_custom = pd.DataFrame(custom_data)

        # Insert data into SQLite database
        for index, row in df_custom.iterrows():
            cursor.execute("""
            INSERT INTO ga4_custom_events_visitor_fact_organic (Date, SessionID, UserID, EventName, EventCount)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(Date, SessionID, UserID, EventName) DO UPDATE SET
                EventCount = excluded.EventCount
            """, (
                row["Date"],
                row["SessionID"],
                row["UserID"],
                row["EventName"],
                row["EventCount"]
            ))

    # Commit after each day
    conn.commit()

    # Move to the next day
    current_date += timedelta(days=1)

# Close the connection
conn.close()
