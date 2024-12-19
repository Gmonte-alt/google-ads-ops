import sqlite3
import pandas as pd
import os

# Define the database path and output CSV file path
sqlite_db_path = 'ff_snowflake.db'
output_folder = 'property_facts/output'
output_csv_path = os.path.join(output_folder, 'property_listing_data_output.csv')

# Ensure the output folder exists
os.makedirs(output_folder, exist_ok=True)

# Connect to the SQLite database
conn = sqlite3.connect(sqlite_db_path)

# Query to retrieve all data from the property_listing_data table
query = "SELECT * FROM property_listing_data"

# Load the data into a DataFrame
df = pd.read_sql_query(query, conn)

# Close the database connection
conn.close()

# Export the DataFrame to a CSV file
df.to_csv(output_csv_path, index=False)

print(f"Data successfully exported to {output_csv_path}")
