# file name: property_facts/city_search_volume_fact.py
# version: V000-000-000
# notes: Takes the CSV file generated by the keyword planner script and insert it into a SQLite table while removing duplicates

import pandas as pd
import sqlite3

# Define file paths and database details
csv_file_path = 'property_facts/output/city_based_search_volume.csv'
sqlite_db_path = 'ff_snowflake.db'
table_name = 'city_search_volume_fact'

# Step 1: Load the CSV file into a DataFrame
df = pd.read_csv(csv_file_path)

# Step 2: Drop duplicates based on City, State Abbreviation, and Keyword columns
df.drop_duplicates(subset=['City', 'State Abbreviation', 'Keyword'], inplace=True)

# Step 3: Connect to the SQLite database
conn = sqlite3.connect(sqlite_db_path)
cursor = conn.cursor()

# Step 4: Create the SQLite table if it doesn't already exist
create_table_query = f"""
CREATE TABLE IF NOT EXISTS {table_name} (
    City TEXT,
    State_Abbreviation TEXT,
    Keyword TEXT,
    Average_Monthly_Searches INTEGER,
    Competition TEXT,
    PRIMARY KEY (City, State_Abbreviation, Keyword)
);
"""
cursor.execute(create_table_query)

# Step 5: Insert the data into the SQLite table, avoiding duplicates
# Use 'replace' to avoid duplicate entries based on the primary key
df.to_sql(table_name, conn, if_exists='replace', index=False)

# Commit the transaction and close the connection
conn.commit()
conn.close()

print(f"Data from {csv_file_path} has been inserted into the {table_name} table in {sqlite_db_path} with duplicates removed.")