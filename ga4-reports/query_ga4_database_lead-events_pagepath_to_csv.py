import sqlite3
import pandas as pd

# Connect to the SQLite database
conn = sqlite3.connect("ga4_data.db")

# Create a cursor object
cursor = conn.cursor()

# Define a query to select data from the ga4_organic_events table
query = """
SELECT
    *
FROM
    ga4_ff_lead_page_data
"""

# Execute the query and fetch the results into a Pandas DataFrame
df = pd.read_sql_query(query, conn)

# Define the CSV file path
csv_file_path = "ga4-reports/output/ga4_lead-events_pagepath_data.csv"

# Write the DataFrame to a CSV file
df.to_csv(csv_file_path, index=False)

# Display a message indicating that the data has been written to the CSV file
print(f"Data has been written to {csv_file_path}")

# Close the database connection
conn.close()
