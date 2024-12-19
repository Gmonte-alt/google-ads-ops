import sqlite3
import pandas as pd

# Define the SQLite database file path
database_path = 'search_query_database.db'
# Define the output CSV file path
csv_output_path = 'query-flow/output/NEW_searchquery_data_table-export.csv'

# Connect to the SQLite database
conn = sqlite3.connect(database_path)

# SQL query to perform a left join
query = """
SELECT *
FROM New_Search_Queries --Search_Queries
"""

# Execute the query and load the data into a DataFrame
df = pd.read_sql_query(query, conn)

# Close the connection to the database
conn.close()

# Save the DataFrame to a CSV file
df.to_csv(csv_output_path, index=False)

# Print confirmation message
print(f'Data successfully saved to {csv_output_path}')