import sqlite3
import pandas as pd

# Database path
sqlite_db_path = 'ff_snowflake.db'

# Connect to the SQLite database
conn = sqlite3.connect(sqlite_db_path)

# Step 1: Total Booking Requests by City and State (joined with property_listing_data)
total_booking_requests_query = """
SELECT p.property_city AS City, p.property_state AS State, COUNT(b.BOOKING_REQUEST_ID) AS Total_Booking_Requests
FROM booking_request_fact AS b
JOIN property_listing_data AS p ON b.PROPERTY_ID = p.property_id
GROUP BY p.property_city, p.property_state
"""
total_booking_requests_df = pd.read_sql_query(total_booking_requests_query, conn)

# Step 2: Number of Active Listings by City and State
active_listings_query = """
SELECT property_city AS City, property_state AS State, COUNT(*) AS Active_Listings
FROM property_listing_data
WHERE active_flag = 1
AND PROPERTY_PAID_FLAG = 1
AND TEST_PROPERTY_FLAG = 0
GROUP BY property_city, property_state
"""
active_listings_df = pd.read_sql_query(active_listings_query, conn)

# Step 3: Booking Requests Growth Trends (requests submitted in the last 6-12 months)
six_months_ago = (pd.Timestamp.now() - pd.DateOffset(months=6)).strftime('%Y-%m-%d')
twelve_months_ago = (pd.Timestamp.now() - pd.DateOffset(months=12)).strftime('%Y-%m-%d')

growth_trends_query = f"""
SELECT p.property_city AS City, p.property_state AS State,
    SUM(CASE WHEN b.SUBMITTED_DATE >= '{six_months_ago}' THEN 1 ELSE 0 END) AS Booking_Requests_Last_6_Months,
    SUM(CASE WHEN b.SUBMITTED_DATE >= '{twelve_months_ago}' THEN 1 ELSE 0 END) AS Booking_Requests_Last_12_Months
FROM booking_request_fact AS b
JOIN property_listing_data AS p ON b.PROPERTY_ID = p.property_id
GROUP BY p.property_city, p.property_state
"""
growth_trends_df = pd.read_sql_query(growth_trends_query, conn)

# Step 4: Total Booking Requests Before January 1, 2023, by Destination (City, State)
pre_jan_2023_requests_query = """
SELECT p.property_city AS City, p.property_state AS State, COUNT(b.BOOKING_REQUEST_ID) AS Booking_Requests_Before_2023
FROM booking_request_fact AS b
JOIN property_listing_data AS p ON b.PROPERTY_ID = p.property_id
WHERE b.SUBMITTED_DATE < '2023-01-01'
GROUP BY p.property_city, p.property_state
"""
pre_jan_2023_requests_df = pd.read_sql_query(pre_jan_2023_requests_query, conn)

# Step 5: Monthly New Booking Requests from January 2023 to October 2024 by Destination
monthly_new_requests_query = """
SELECT p.property_city AS City, p.property_state AS State,
       strftime('%Y-%m', b.SUBMITTED_DATE) AS Month, COUNT(b.BOOKING_REQUEST_ID) AS Monthly_Booking_Requests
FROM booking_request_fact AS b
JOIN property_listing_data AS p ON b.PROPERTY_ID = p.property_id
WHERE b.SUBMITTED_DATE >= '2023-01-01' AND b.SUBMITTED_DATE < '2024-11-01'
GROUP BY p.property_city, p.property_state, strftime('%Y-%m', b.SUBMITTED_DATE)
"""
monthly_new_requests_df = pd.read_sql_query(monthly_new_requests_query, conn)

# Close the database connection
conn.close()

# Step 6: Merge all DataFrames on City and State for a consolidated view
consolidated_df = (total_booking_requests_df
                   .merge(active_listings_df, on=["City", "State"], how="left")
                   .merge(growth_trends_df, on=["City", "State"], how="left")
                   .merge(pre_jan_2023_requests_df, on=["City", "State"], how="left"))

# Step 7: Pivot Monthly New Requests Data to Create Columns for Each Month
# Reshape the data so each month becomes a column in the final output
monthly_new_requests_pivot = monthly_new_requests_df.pivot_table(
    index=["City", "State"],
    columns="Month",
    values="Monthly_Booking_Requests",
    fill_value=0
).reset_index()

# Step 8: Merge the Pivoted Monthly Requests Data with the Consolidated Data
final_df = pd.merge(consolidated_df, monthly_new_requests_pivot, on=["City", "State"], how="left")

# Step 9: Sort the final DataFrame by Active_Listings in descending order
final_df = final_df.sort_values(by="Active_Listings", ascending=False)

# Step 10: Export the final DataFrame to a CSV file
output_csv_path = 'property_facts/output/booking_requests_analysis.csv'
final_df.to_csv(output_csv_path, index=False)

print(f"Analysis successfully exported to {output_csv_path}")

