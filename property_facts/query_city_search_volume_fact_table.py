import sqlite3

# Define the database path
sqlite_db_path = 'ff_snowflake.db'

# Connect to the SQLite database
conn = sqlite3.connect(sqlite_db_path)
cursor = conn.cursor()

# Query to get column information for the city_search_volume_fact table
cursor.execute("PRAGMA table_info(city_search_volume_fact);")
columns = cursor.fetchall()

# Close the connection
conn.close()

# Print column names
print("Columns in city_search_volume_fact:")
for column in columns:
    print(f"Column Name: {column[1]}, Data Type: {column[2]}")
