# file name: campaign-experiments/process_experiment_campaign_data.py
# version number: V000-000-000
# Note: Connects to the resulting tables from campaign-experiments/experiment_campaigns_fact_procedure.py and uses reports/output/final_data.xlsx
#       First working version.      

import pandas as pd
import sqlite3
from datetime import datetime

# Define a function to get test_name and campaign_base_campaign_name from the database based on the conditions
def get_test_name_base_campaign(excel_campaign_id, excel_date):
    conn = sqlite3.connect('campaigns.db')
    cursor = conn.cursor()

    query = """
    SELECT test_name,
           (SELECT cbf2.campaign_name
            FROM campaigns_basic_facts cbf2
            WHERE cbf2.campaign_id = SUBSTR(cbf.campaign_base_campaign_id, INSTR(cbf.campaign_base_campaign_id, '/campaigns/') + LENGTH('/campaigns/'))
           ) AS campaign_base_campaign_name,
           (SELECT cbf2.experiment_type
            FROM campaigns_basic_facts cbf2
            WHERE cbf2.campaign_id = SUBSTR(cbf.campaign_base_campaign_id, INSTR(cbf.campaign_base_campaign_id, '/campaigns/') + LENGTH('/campaigns/'))
           ) AS experiment_type
    FROM experiment_campaigns_fact cbf
    WHERE (cbf.campaign_id = ? OR cbf.campaign_base_campaign_id LIKE '%' || ?)
    AND ? BETWEEN cbf.campaign_start_date AND cbf.campaign_end_date
    AND cbf.experiment_type = 'EXPERIMENT'
    """
    
    cursor.execute(query, (excel_campaign_id, excel_campaign_id, excel_date))
    result = cursor.fetchone()
    conn.close()
    
    if result:
        return result[0], result[1], result[2]
    else:
        return None, None, None

# Function to process the Excel file and update the dataframe
def process_campaign_performance_data(excel_file):
    # Load the Excel file into a pandas dataframe
    df = pd.read_excel(excel_file)
    
    # Check and print columns to debug
    print("Columns in the dataframe:", df.columns)
    
    # Ensure the date column is in datetime format (replace 'date' with the actual date column name if different)
    date_column = 'Date'  # Change this to the correct column name if necessary
    campaign_id_column = 'Campaign ID'  # Change this to the correct column name if necessary
    
    if date_column not in df.columns:
        raise KeyError(f"Column '{date_column}' not found in the Excel file.")
    if campaign_id_column not in df.columns:
        raise KeyError(f"Column '{campaign_id_column}' not found in the Excel file.")
    
    df[date_column] = pd.to_datetime(df[date_column])
    
    # Create new columns 'test_name' and 'campaign_base_campaign_name'
    df['test_name'] = None
    df['campaign_base_campaign_name'] = None
    df['experiment_type'] = None
    
    # Populate the new columns using the get_test_name_base_campaign function
    for index, row in df.iterrows():
        test_name, campaign_base_campaign_name, experiment_type = get_test_name_base_campaign(row[campaign_id_column], row[date_column].strftime('%Y-%m-%d'))
        df.at[index, 'test_name'] = test_name
        df.at[index, 'campaign_base_campaign_name'] = campaign_base_campaign_name
        df.at[index, 'experiment_type'] = experiment_type
    
    return df

# Main execution
if __name__ == "__main__":
    # Path to the Excel file
    excel_file_path = "reports/output/final_data.xlsx"
    
    # Process the campaign performance data
    try:
        updated_df = process_campaign_performance_data(excel_file_path)
        
        # Print the updated dataframe
        print(updated_df)
        
        # Optionally, save the updated dataframe to a new Excel file
        updated_df.to_excel("reports/output/experiments_final_data.xlsx", index=False)
    except Exception as e:
        print(f"An error occurred: {e}")


