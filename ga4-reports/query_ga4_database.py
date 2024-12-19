# file name:
# version:
# Notes:

import sqlite3
import pandas as pd

# Connect to the SQLite database
conn = sqlite3.connect("ga4_data.db")

# Create a cursor object
cursor = conn.cursor()

# Define a query to select data from the ga4_events table
query = """
SELECT
    Date,
    Source,
    Medium,
    Campaign,
    FF_Purchase_Event_Count,
    FF_Lead_Event_Count,
    FF_BRSubmit_Event_Count,
    FF_DMSubmit_Event_Count,
    FF_PhoneGet_Event_Count
FROM
    ga4_events
"""

# Execute the query and fetch the results into a Pandas DataFrame
df = pd.read_sql_query(query, conn)

# Display the DataFrame
print(df)

# Close the database connection
conn.close()
