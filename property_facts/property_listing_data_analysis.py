import sqlite3
import pandas as pd
from datetime import datetime, timedelta

# Database path
sqlite_db_path = 'ff_snowflake.db'

# Connect to the SQLite database
conn = sqlite3.connect(sqlite_db_path)

# Step 1: Number of Active Listings by City and State with specified filters
active_listings_query = """
SELECT property_city AS City, property_state AS State, COUNT(*) AS Active_Listings
FROM property_listing_data
WHERE active_flag = 1
AND PROPERTY_PAID_FLAG = 1
AND TEST_PROPERTY_FLAG = 0
GROUP BY property_city, property_state
"""
active_listings_df = pd.read_sql_query(active_listings_query, conn)

# Step 2: Listing Growth Trends (listings added in the past 6-12 months)
six_months_ago = (datetime.now() - timedelta(days=180)).strftime('%Y-%m-%d')
twelve_months_ago = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')

growth_trends_query = f"""
SELECT property_city AS City, property_state AS State,
    SUM(CASE WHEN UNIT_CREATED_AT >= '{six_months_ago}' THEN 1 ELSE 0 END) AS Listings_Last_6_Months,
    SUM(CASE WHEN UNIT_CREATED_AT >= '{twelve_months_ago}' THEN 1 ELSE 0 END) AS Listings_Last_12_Months
FROM property_listing_data
WHERE active_flag = 1
AND PROPERTY_PAID_FLAG = 1
AND TEST_PROPERTY_FLAG = 0
GROUP BY property_city, property_state
"""
growth_trends_df = pd.read_sql_query(growth_trends_query, conn)

# Step 3: Active Listings Before January 1, 2023, by Destination (City, State)
pre_jan_2023_listings_query = """
SELECT property_city AS City, property_state AS State, COUNT(*) AS Active_Listings_Before_2023
FROM property_listing_data
WHERE active_flag = 1
AND PROPERTY_PAID_FLAG = 1
AND TEST_PROPERTY_FLAG = 0
AND UNIT_CREATED_AT < '2023-01-01'
GROUP BY property_city, property_state
"""
pre_jan_2023_listings_df = pd.read_sql_query(pre_jan_2023_listings_query, conn)

# Step 4: Monthly New Listings from January 2023 to October 2024 by Destination
monthly_new_listings_query = """
SELECT property_city AS City, property_state AS State, 
       strftime('%Y-%m', UNIT_CREATED_AT) AS Month, COUNT(*) AS New_Listings
FROM property_listing_data
WHERE UNIT_CREATED_AT >= '2023-01-01' AND UNIT_CREATED_AT < '2024-11-01'
AND active_flag = 1
AND PROPERTY_PAID_FLAG = 1
AND TEST_PROPERTY_FLAG = 0
GROUP BY property_city, property_state, strftime('%Y-%m', UNIT_CREATED_AT)
"""
monthly_new_listings_df = pd.read_sql_query(monthly_new_listings_query, conn)

# Close the database connection
conn.close()

# Step 5: Merge all DataFrames on City and State for a consolidated view
consolidated_df = (active_listings_df
                   .merge(growth_trends_df, on=["City", "State"], how="left")
                   .merge(pre_jan_2023_listings_df, on=["City", "State"], how="left"))

# Step 6: Pivot Monthly New Listings Data to Create Columns for Each Month
# Reshape the data so each month becomes a column in the final output
monthly_new_listings_pivot = monthly_new_listings_df.pivot_table(
    index=["City", "State"],
    columns="Month",
    values="New_Listings",
    fill_value=0
).reset_index()

# Step 7: Merge the Pivoted Monthly Listings Data with the Consolidated Data
final_df = pd.merge(consolidated_df, monthly_new_listings_pivot, on=["City", "State"], how="left")

# Step 8: Export the final DataFrame to a CSV file
output_csv_path = 'property_facts/output/property_listings_analysis.csv'
final_df.to_csv(output_csv_path, index=False)

print(f"Analysis successfully exported to {output_csv_path}")
