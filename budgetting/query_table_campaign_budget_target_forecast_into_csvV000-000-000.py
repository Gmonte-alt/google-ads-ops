# file name: budgetting/query_table_campaign_budget_target_forecast_into_csv.py
# version: V000-000-000
# output file:
# input file: reports/output/final_combined_data.csv
# notes:

import sqlite3
import pandas as pd

# Define the SQLite database file path
database_path = 'campaigns.db'
# Define the CSV file path
csv_file_path = 'budgetting/output/campaign_details.csv'

# Function to query and save all data from the campaign_details table to a CSV file
def query_campaign_details_to_csv():
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

    # Create a pandas DataFrame from the fetched data
    df = pd.DataFrame(rows, columns=column_names)

    # Save the DataFrame to a CSV file
    df.to_csv(csv_file_path, index=False)

    conn.close()
    print(f'Data successfully saved to {csv_file_path}')

# Main execution
if __name__ == '__main__':
    query_campaign_details_to_csv()
