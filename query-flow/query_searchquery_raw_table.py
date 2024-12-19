import sqlite3
import pandas as pd

# Define the SQLite database file path
database_path = 'search_query_database.db'
# Define the output CSV file path
csv_output_path = 'query-flow/output/searchquery_raw_data_bydate.csv'

# Connect to the SQLite database
conn = sqlite3.connect(database_path)

# SQL query to perform a left join
query = """
SELECT DISTINCT "Search Term"
FROM google_ads_search_term_raw
; --Search_Queries
"""

# Execute the query and load the data into a DataFrame
df = pd.read_sql_query(query, conn)
print(df.columns)
# Close the connection to the database
conn.close()

# Save the DataFrame to a CSV file
df.to_csv(csv_output_path, index=False)

# Print confirmation message
print(f'Data successfully saved to {csv_output_path}')