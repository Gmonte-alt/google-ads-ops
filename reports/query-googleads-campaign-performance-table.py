# file name: reports/query-googleads-campaign-performance-table.py
# version: V000-000-000
# output:
# Notes:

import sqlite3
import pandas as pd

# Define the SQLite database file path
database_path = 'campaigns.db'
# Define the output CSV file path
csv_output_path = 'reports/output/joined_campaign_data.csv'

# Connect to the SQLite database
conn = sqlite3.connect(database_path)

# SQL query to perform a left join
query = """
SELECT p.*, f.campaign_base_campaign_name
FROM google_ads_campaign_performance p
LEFT JOIN experiment_campaigns_fact f ON p.Campaign_ID = f.campaign_id
"""

# Execute the query and load the data into a DataFrame
df = pd.read_sql_query(query, conn)

# Close the connection to the database
conn.close()

# Save the DataFrame to a CSV file
df.to_csv(csv_output_path, index=False)

# Print confirmation message
print(f'Data successfully saved to {csv_output_path}')
