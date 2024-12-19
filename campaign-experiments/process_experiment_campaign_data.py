# file name: campaign-experiments/process_experiment_campaign_data.py
# version number: V000-000-003
# Note: Connects to the resulting tables from campaign-experiments/experiment_campaigns_fact_procedure.py and uses reports/output/final_data.xlsx
#       Creates the extract_experiment_theme function to create the test_variable

import pandas as pd
import sqlite3
from datetime import datetime

def extract_experiment_theme(experiment_campaign_name, base_campaign_name):
    """
    Function to extract the experiment theme from the experiment campaign name
    by removing the base campaign name.
    """
    # Remove leading/trailing spaces and convert to lowercase for comparison
    experiment_campaign_name = experiment_campaign_name.strip().lower()
    base_campaign_name = base_campaign_name.strip().lower()
    
    # Check if the base campaign name is a prefix of the experiment campaign name
    if experiment_campaign_name.startswith(base_campaign_name):
        # Remove the base campaign name and strip any extra spaces from the tail-end
        theme = experiment_campaign_name[len(base_campaign_name):].strip()
        # Capitalize the first letter for consistency
        theme = theme.capitalize()
        return theme
    else:
        # If the base name isn't a prefix, return the experiment campaign name as is
        return experiment_campaign_name

# Define a function to get test_name, campaign_base_campaign_name, and experiment_type from the database based on the conditions
def get_test_name_base_campaign(excel_campaign_id, excel_date):
    conn = sqlite3.connect('campaigns.db')
    cursor = conn.cursor()

    query = """
    SELECT test_name,
           (SELECT cbf2.campaign_name
            FROM campaigns_basic_facts cbf2
            WHERE cbf2.campaign_id = SUBSTR(cbf.campaign_base_campaign_id, INSTR(cbf.campaign_base_campaign_id, '/campaigns/') + LENGTH('/campaigns/'))
           ) AS campaign_base_campaign_name,
           cbf.experiment_type
    FROM experiment_campaigns_fact cbf
    WHERE cbf.campaign_id = ?
    AND ? BETWEEN cbf.campaign_start_date AND cbf.campaign_end_date
    UNION ALL
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
    WHERE (cbf.campaign_id != ? AND cbf.campaign_base_campaign_id LIKE '%' || ?)
    AND ? BETWEEN cbf.campaign_start_date AND cbf.campaign_end_date
    """
    
    cursor.execute(query, (excel_campaign_id, excel_date, excel_campaign_id, excel_campaign_id, excel_date))
    result = cursor.fetchone()
    conn.close()
    
    if result:
        test_name = result[0].replace('  ', ' ') if result[0] else None  # Remove double spaces
        return test_name, result[1], result[2]
    else:
        return None, None, None

# Function to process the Excel file and update the dataframe
def process_campaign_performance_data(excel_file):
    # Load the Excel file into a pandas dataframe
    df = pd.read_excel(excel_file)
    
    # Ensure the date column is in datetime format (replace 'Date' with the actual date column name if different)
    date_column = 'Date'  # Change this to the correct column name if necessary
    campaign_id_column = 'Campaign ID'  # Change this to the correct column name if necessary
    campaign_name_column = 'Campaign Name'  # Adjust as needed for your data structure

    if date_column not in df.columns:
        raise KeyError(f"Column '{date_column}' not found in the Excel file.")
    if campaign_id_column not in df.columns:
        raise KeyError(f"Column '{campaign_id_column}' not found in the Excel file.")
    if campaign_name_column not in df.columns:
        raise KeyError(f"Column '{campaign_name_column}' not found in the Excel file.")
    
    df[date_column] = pd.to_datetime(df[date_column])
    
        # Add a new column, ombined_tenant_metrics, which is the sum of the three tenant metrics
    df['combined_tenant_metrics'] = df[['Furnished Finder - GA4 (web) FF-BRSubmit', 'Furnished Finder - GA4 (web) FF-DMSubmit', 'Furnished Finder - GA4 (web) FF-PhoneGet']].sum(axis=1)
    
    # Create new columns 'test_name', 'campaign_base_campaign_name', and 'experiment_type' (if not already present)
    if 'test_name' not in df.columns:
        df['test_name'] = None
    if 'campaign_base_campaign_name' not in df.columns:
        df['campaign_base_campaign_name'] = None
    if 'experiment_type' not in df.columns:
        df['experiment_type'] = None
    
    # Populate the new columns using the get_test_name_base_campaign function
    for index, row in df.iterrows():
        test_name, campaign_base_campaign_name, experiment_type = get_test_name_base_campaign(
            row[campaign_id_column], row[date_column].strftime('%Y-%m-%d')
        )
        
        # If the base campaign name is found, use it to extract the experiment theme
        if campaign_base_campaign_name:
            theme = extract_experiment_theme(row[campaign_name_column], campaign_base_campaign_name)
        else:
            theme = None
        
        # Update the DataFrame with the new values
        df.at[index, 'test_name'] = theme
        df.at[index, 'campaign_base_campaign_name'] = campaign_base_campaign_name
        df.at[index, 'experiment_type'] = experiment_type
    
    return df

# Main execution
if __name__ == "__main__":
    # Path to the Excel file
    excel_file_path = "reports/output/final_combined_data_ga4.xlsx" # "reports/output/final_data.xlsx"
    
    # Process the campaign performance data
    try:
        updated_df = process_campaign_performance_data(excel_file_path)
        
        # Print the updated dataframe
        print(updated_df)
        
        # Optionally, save the updated dataframe to a new Excel file
        updated_df.to_excel("reports/output/updated_final_data.xlsx", index=False)
    except Exception as e:
        print(f"An error occurred: {e}")

