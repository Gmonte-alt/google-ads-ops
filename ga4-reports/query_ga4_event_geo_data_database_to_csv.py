# file name:
# version:
# notes: ga4-reports/query_ga4_database_to_csv.py

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
    Device,
    City,
    Region,
    Country,
/*    SUM(FF_Purchase_Event_Count) AS FF_Purchase_Event_Count,
    SUM(FF_Lead_Event_Count) AS FF_Lead_Event_Count,
    SUM(FF_BRSubmit_Event_Count) AS FF_BRSubmit_Event_Count,
    SUM(FF_DMSubmit_Event_Count) AS FF_DMSubmit_Event_Count,
    SUM(FF_PhoneGet_Event_Count) AS FF_PhoneGet_Event_Count,
    SUM(FF_HRSubmit_Event_Count) AS FF_HRSubmit_Event_Count,
    SUM(HR_Submit_Event_New_Traveler_Lead_Count) AS HR_Submit_Event_New_Traveler_Lead_Count
  */  
    FF_Purchase_Event_Count,
    FF_Lead_Event_Count,
    FF_BRSubmit_Event_Count,
    FF_DMSubmit_Event_Count,
    FF_PhoneGet_Event_Count,
    FF_HRSubmit_Event_Count,
    HR_Submit_Event_New_Traveler_Lead_Count
FROM
    ga4_events_geo_data
--WHERE Source = "google" AND Medium = "cpc"
--GROUP BY Date, Source,
--    Medium,
--    Campaign,
--    Device, Region, Country
    ;
"""

# Execute the query and fetch the results into a Pandas DataFrame
df = pd.read_sql_query(query, conn)

# Define the CSV file path
csv_file_path = "ga4-reports/output/ga4_events_geo-data_data.csv"

# Write the DataFrame to a CSV file
df.to_csv(csv_file_path, index=False)

# Display a message indicating that the data has been written to the CSV file
print(f"Data has been written to {csv_file_path}")

# Close the database connection
conn.close()
