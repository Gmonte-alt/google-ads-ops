# file name: 
# version:
# Notes: str_ban_list

import pandas as pd
import sqlite3
from datetime import datetime

# Define the path to the Excel file
excel_file_path = 'property_facts/input/str_ban_list.xlsx'

# Load the Excel data into a DataFrame
df = pd.read_excel(excel_file_path, sheet_name='str_ban_list')  # Specify the sheet name if needed

# Convert ban_status column to boolean (1 for ban, 0 for no ban)
# Adjust the mapping based on the actual values in your Excel file (e.g., "Yes" and "No")
df['ban_status'] = df['ban_status'].apply(lambda x: 1 if str(x).lower() in ['yes', 'true', 'banned'] else 0)

# Add the current date as the update_date column
df['update_date'] = datetime.today().date()

# Inspect the DataFrame (optional)
print(df.head())

# Connect to SQLite database (or create it if it doesn't exist)
conn = sqlite3.connect('ff_snowflake.db')
cursor = conn.cursor()

# Define the SQL command to create the STR table
# Modify the column definitions according to your CSV file structure
create_table_query = """
CREATE TABLE IF NOT EXISTS STR_BAN (
    city TEXT,
    state TEXT,
    ban_status INTEGER,  -- Stored as 1 (True) or 0 (False)
    update_date DATE
);
"""

# Execute the table creation command
cursor.execute(create_table_query)

# Insert data from the DataFrame into the STR table
df.to_sql('STR_BAN', conn, if_exists='replace', index=False)

# Commit the transaction and close the connection
conn.commit()
conn.close()

print("STR_BAN table created and data inserted successfully with update_date.")
