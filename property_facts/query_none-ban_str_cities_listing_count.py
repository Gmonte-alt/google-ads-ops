import sqlite3
import pandas as pd

# Define the database path and output CSV file path
sqlite_db_path = 'ff_snowflake.db'
output_csv_path = 'property_facts/output/non_str_ban_listings.csv'

# Connect to the SQLite database
conn = sqlite3.connect(sqlite_db_path)

# SQL query to retrieve city-state pairs not in the STR_BAN table with listing counts
query = """
SELECT DISTINCT p.property_city AS City, s.state_abbreviation AS "State Abbreviation", COUNT(p.Listing_ID) AS total_listings
FROM property_listing_data AS p
JOIN state_abbreviations AS s ON p.property_state = s.state_name
LEFT JOIN STR_BAN AS str ON p.property_city = str.city AND p.property_state = str.state
WHERE (str.city IS NULL OR str.state IS NULL)  -- Exclude cities and states in STR_BAN
AND p.ACTIVE_FLAG = 1
AND p.PROPERTY_PAID_FLAG = 1
AND p.UNIT_HIDDEN_FLAG = 0
AND p.HAS_MIN_PHOTO_COUNT_FLAG = 1
AND p.TEST_PROPERTY_FLAG = 0
GROUP BY p.property_city, s.state_abbreviation
"""

# Execute the query and load the results into a DataFrame
df = pd.read_sql_query(query, conn)

# Close the database connection
conn.close()

# Export the DataFrame to a CSV file
df.to_csv(output_csv_path, index=False)

print(f"Data successfully exported to {output_csv_path}")
