# file name: ga4-reports/query_ga4_database_union_events_sessions.py
# version: V000-000-000
# output: table; csv
# Notes: Combines all ga4 data into a new combined table

import sqlite3
import pandas as pd

# Connect to the SQLite database
conn = sqlite3.connect("ga4_data.db")
cursor = conn.cursor()

# Create DataFrames from both tables
query_events = """
SELECT
    Date,
    Source,
    Medium,
    Campaign,
    Device,
    0 AS Sessions,
    0 AS TotalUsers,
    0 AS NewUsers,
    FF_Purchase_Event_Count,
    FF_Lead_Event_Count,
    FF_BRSubmit_Event_Count,
    FF_DMSubmit_Event_Count,
    FF_PhoneGet_Event_Count,
    FF_HRSubmit_Event_Count,
    HR_Submit_Event_New_Traveler_Lead_Count
FROM
    ga4_events
"""

query_sessions = """
SELECT
    Date,
    Source,
    Medium,
    Campaign,
    Device,
    Sessions,
    TotalUsers,
    NewUsers,
    0 AS FF_Purchase_Event_Count,
    0 AS FF_Lead_Event_Count,
    0 AS FF_BRSubmit_Event_Count,
    0 AS FF_DMSubmit_Event_Count,
    0 AS FF_PhoneGet_Event_Count,
    0 AS FF_HRSubmit_Event_Count,
    0 AS HR_Submit_Event_New_Traveler_Lead_Count
FROM
    ga4_sessions
"""

df_events = pd.read_sql_query(query_events, conn)
df_sessions = pd.read_sql_query(query_sessions, conn)

# Combine the two DataFrames using union all
df_combined = pd.concat([df_events, df_sessions])

# Replace NaNs with zeros
df_combined.fillna(0, inplace=True)

# Aggregate the columns for "FF_HRSubmit_Event_Count" and "HR_Submit_Event_New_Traveler_Lead_Count"
df_combined["Total Traveler Actions"] = (
    df_combined["FF_BRSubmit_Event_Count"] +
    df_combined["FF_DMSubmit_Event_Count"] +
    df_combined["FF_PhoneGet_Event_Count"] +
    df_combined["FF_HRSubmit_Event_Count"] +
    df_combined["HR_Submit_Event_New_Traveler_Lead_Count"]
)

# Calculate "Traveler Value" and "Landlord Value"
df_combined["Traveler Value"] = df_combined["Total Traveler Actions"] * 2
df_combined["Landlord Value"] = df_combined["FF_Purchase_Event_Count"] * 240

# Write the combined DataFrame to a new table in the SQLite database
df_combined.to_sql("ga4_combined", conn, if_exists="replace", index=False)

# Define the CSV file path
csv_file_path = "ga4-reports/output/ga4_combined_data.csv"

# Write the DataFrame to a CSV file
df_combined.to_csv(csv_file_path, index=False)

# Display a message indicating that the data has been written to the CSV file
print(f"Data has been written to {csv_file_path}")

# Close the database connection
conn.close()
