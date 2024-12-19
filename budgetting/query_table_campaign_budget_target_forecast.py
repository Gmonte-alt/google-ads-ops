import sqlite3

# Define the SQLite database file path
database_path = 'campaigns.db'

# Function to query and print all data from the campaign_details table
def query_campaign_details():
    conn = sqlite3.connect(database_path)
    cursor = conn.cursor()

    # Query to select all data from the campaign_details table
    query = '''
    SELECT * FROM campaign_details
    '''

    cursor.execute(query)
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

# Main execution
if __name__ == '__main__':
    query_campaign_details()
