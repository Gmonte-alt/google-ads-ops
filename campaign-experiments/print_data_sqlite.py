# file name: campaign-experiments/print_data_sqlite.py

import sqlite3

# Function to print the campaigns table
def print_campaigns_table():
    conn = sqlite3.connect('campaigns.db')
    cursor = conn.cursor()
    
    # Query to select all data from the campaigns table
    # select_query = "SELECT * FROM campaigns_basic_facts WHERE campaign_name LIKE '%Travel Nurse Housing%'; "
    select_query = "SELECT * FROM experiment_campaigns_fact; "
    
    cursor.execute(select_query)
    rows = cursor.fetchall()
    
    # Fetch the column names
    column_names = [description[0] for description in cursor.description]
    
    # Print the column names
    print(" | ".join(column_names))
    print("-" * 100)
    
    # Print all rows
    for row in rows:
        print(" | ".join(map(str, row)))
    
    conn.close()

# Call the function to print the campaigns table
if __name__ == "__main__":
    print_campaigns_table()
