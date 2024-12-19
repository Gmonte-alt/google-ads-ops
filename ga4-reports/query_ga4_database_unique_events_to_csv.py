# File name:
# Version:
# Notes:

import sqlite3
import pandas as pd

# Connect to the SQLite database
conn = sqlite3.connect("ga4_data.db")

# Create a cursor object
cursor = conn.cursor()

# Define a query to select data from the ga4_unique_events table
query = """
SELECT
    Date,
    Source,
    Medium,
    Campaign,
    Device,
    FF_Purchase_Unique_Event_Count,
    FF_Lead_Unique_Event_Count,
    FF_BRSubmit_Unique_Event_Count,
    FF_DMSubmit_Unique_Event_Count,
    FF_PhoneGet_Unique_Event_Count,
    Active_Users,
    New_Users
FROM
    ga4_unique_events
"""

# Execute the query and fetch the results into a Pandas DataFrame
df = pd.read_sql_query(query, conn)

# Define the CSV file path
csv_file_path = "ga4-reports/output/ga4_unique_events_data.csv"

# Write the DataFrame to a CSV file
df.to_csv(csv_file_path, index=False)

# Display a message indicating that the data has been written to the CSV file
print(f"Data has been written to {csv_file_path}")

# Close the database connection
conn.close()
