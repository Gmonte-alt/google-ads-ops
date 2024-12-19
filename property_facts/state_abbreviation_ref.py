# name:
# version:
# notes:

import sqlite3

# Define a list of tuples for state names and abbreviations
states = [
    ("Alabama", "AL"), ("Alaska", "AK"), ("Arizona", "AZ"), ("Arkansas", "AR"),
    ("California", "CA"), ("Colorado", "CO"), ("Connecticut", "CT"), ("Delaware", "DE"),
    ("Florida", "FL"), ("Georgia", "GA"), ("Hawaii", "HI"), ("Idaho", "ID"),
    ("Illinois", "IL"), ("Indiana", "IN"), ("Iowa", "IA"), ("Kansas", "KS"),
    ("Kentucky", "KY"), ("Louisiana", "LA"), ("Maine", "ME"), ("Maryland", "MD"),
    ("Massachusetts", "MA"), ("Michigan", "MI"), ("Minnesota", "MN"), ("Mississippi", "MS"),
    ("Missouri", "MO"), ("Montana", "MT"), ("Nebraska", "NE"), ("Nevada", "NV"),
    ("New Hampshire", "NH"), ("New Jersey", "NJ"), ("New Mexico", "NM"), ("New York", "NY"),
    ("North Carolina", "NC"), ("North Dakota", "ND"), ("Ohio", "OH"), ("Oklahoma", "OK"),
    ("Oregon", "OR"), ("Pennsylvania", "PA"), ("Rhode Island", "RI"), ("South Carolina", "SC"),
    ("South Dakota", "SD"), ("Tennessee", "TN"), ("Texas", "TX"), ("Utah", "UT"),
    ("Vermont", "VT"), ("Virginia", "VA"), ("Washington", "WA"), ("West Virginia", "WV"),
    ("Wisconsin", "WI"), ("Wyoming", "WY")
]

# Connect to the SQLite database (or create it if it doesn't exist)
sqlite_db_path = 'ff_snowflake.db'
conn = sqlite3.connect(sqlite_db_path)
cursor = conn.cursor()

# Create the state_abbreviations table
create_table_query = """
CREATE TABLE IF NOT EXISTS state_abbreviations (
    state_name TEXT PRIMARY KEY,
    state_abbreviation TEXT
);
"""
cursor.execute(create_table_query)

# Insert state data into the table
insert_query = "INSERT OR IGNORE INTO state_abbreviations (state_name, state_abbreviation) VALUES (?, ?)"
cursor.executemany(insert_query, states)

# Commit the transaction and close the connection
conn.commit()
conn.close()

print("State abbreviations table created and populated successfully.")
