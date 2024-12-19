import sqlite3
import pandas as pd

# Define the database path and output CSV file path
sqlite_db_path = 'ff_snowflake.db'
output_csv_path = 'property_facts/output/destination_insights_output.csv'

# Connect to the SQLite database
conn = sqlite3.connect(sqlite_db_path)

# Query to retrieve all data from the destination_insights table
query = "SELECT * FROM destination_insights"

# Load the data into a DataFrame
df = pd.read_sql_query(query, conn)

# Close the database connection
conn.close()

# Export the DataFrame to a CSV file
df.to_csv(output_csv_path, index=False)

print(f"Data successfully exported to {output_csv_path}")
