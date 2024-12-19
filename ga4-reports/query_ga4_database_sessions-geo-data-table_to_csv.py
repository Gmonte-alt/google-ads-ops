# file name: ga4-reports/query_ga4_database_sessions-table_to_csv.py
# version:
# notes: ga4-reports/query_ga4_database_to_csv.py

import sqlite3
import pandas as pd

# Connect to the SQLite database
conn = sqlite3.connect("ga4_data.db")

# Create a cursor object
cursor = conn.cursor()

# Define a query to select data from the ga4_sessions table
query = """
SELECT
    Date,
    Source,
    Medium,
    Campaign,
    Device,
--    City,
    State,
    Country,
    SUM(Sessions) AS Sessions,
    SUM(TotalUsers) AS TotalUsers,
    SUM(NewUsers) AS NewUsers
FROM
    ga4_sessions_geo
WHERE Source = "google" AND Medium = "cpc"
GROUP BY Date, Source,
    Medium,
    Campaign,
    Device, State, Country
    ;
"""

# Execute the query and fetch the results into a Pandas DataFrame
df = pd.read_sql_query(query, conn)

# Define the CSV file path
csv_file_path = "ga4-reports/output/ga4_sessions-geo-data_data.csv"

# Write the DataFrame to a CSV file
df.to_csv(csv_file_path, index=False)

# Display a message indicating that the data has been written to the CSV file
print(f"Data has been written to {csv_file_path}")

# Close the database connection
conn.close()
