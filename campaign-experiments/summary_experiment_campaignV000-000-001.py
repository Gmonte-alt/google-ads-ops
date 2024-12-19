# file name: campaign-experiments/summary_experiment_campaign.py
# version number: V000-000-001
# Note: Connects to the resulting tables from campaign-experiments/experiment_campaigns_fact_procedure.py and uses reports/output/final_data.xlsx
#       This script reformats the mobile & device tables

import pandas as pd
import numpy as np
from openpyxl import Workbook
from openpyxl.utils.dataframe import dataframe_to_rows

def create_summary_workbook(input_file, output_file):
    # Load the data
    df = pd.read_excel(input_file)
    
    # Print column names to debug
    print("Column names in the DataFrame:")
    print(df.columns)
    
    # Clean up column names by stripping leading/trailing spaces and converting to lower case
    df.columns = df.columns.str.strip().str.lower()
    
    # Ensure test_name is a string and handle NaN values
    df['test_name'] = df['test_name'].astype(str).fillna('Unknown')
    
    # Fill blank values in 'experiment_type' with 'Unknown'
    df['experiment_type'] = df['experiment_type'].astype(str).fillna('Unknown')
    
    # Verify required columns exist
    required_columns = ['experiment_type', 'device', 'campaign_base_campaign_name', 'impressions', 'clicks', 'cost', 'furnished finder - ga4 (web) ff lead', 'furnished finder - ga4 (web) ff purchase']
    for col in required_columns:
        if col not in df.columns:
            raise KeyError(f"Column '{col}' not found in the input data.")
    
    # Print out the first few rows to check for correct loading
    print(df.head())

    # Create a new workbook
    wb = Workbook()
    wb.remove(wb.active)  # Remove the default sheet
    
    # List of metrics to calculate
    metrics = ['impressions', 'clicks', 'cost', 'furnished finder - ga4 (web) ff lead', 'furnished finder - ga4 (web) ff purchase']
    
    # Function to calculate summary metrics
    def calculate_summary(df):
        # Ensure 'experiment_type' is included for grouping
        summary_df = df[['experiment_type'] + metrics]
        # Select only numeric columns for summation
        numeric_df = summary_df[metrics].apply(pd.to_numeric, errors='coerce').fillna(0)
        numeric_df['experiment_type'] = summary_df['experiment_type']
        summary = numeric_df.groupby('experiment_type').sum()[metrics]
        summary['cpc'] = summary['cost'] / summary['clicks']
        summary['cpl'] = summary['cost'] / summary['furnished finder - ga4 (web) ff lead']
        summary['cac'] = summary['cost'] / summary['furnished finder - ga4 (web) ff purchase']
        summary['ctr'] = summary['clicks'] / summary['impressions']
        summary['lead/clicks'] = summary['furnished finder - ga4 (web) ff lead'] / summary['clicks']
        summary['purchase/leads'] = summary['furnished finder - ga4 (web) ff purchase'] / summary['furnished finder - ga4 (web) ff lead']
        summary = summary.replace([np.inf, -np.inf], np.nan).fillna(0)
        return summary
    
    # Function to calculate percentage difference
    def calculate_percentage_difference(experiment, base):
        return ((experiment - base) / base) #* 100
    
    # Process each test_name
    for test_name in df['test_name'].unique():
        test_df = df[df['test_name'] == test_name]
        
        # Ensure the sheet title is valid
        sheet_title = test_name[:31]  # Limit to 31 characters
        ws = wb.create_sheet(title=sheet_title)
        
        # Overall summary
        overall_summary = calculate_summary(test_df)
        if 'EXPERIMENT' in overall_summary.index and 'BASE' in overall_summary.index:
            overall_summary.loc['% Difference'] = calculate_percentage_difference(
                overall_summary.loc['EXPERIMENT'], overall_summary.loc['BASE'])
        
        # Write the overall summary to the worksheet
        ws.append(['Account ID'])
        ws.append(['Platform:', 'Google Ads', 'Test name:', test_name])
        ws.append(['Start date:', '', 'End date:'])
        
        for r in dataframe_to_rows(overall_summary.reset_index(), index=False, header=True):
            ws.append(r)
        
        # Mobile and Desktop summary
        for device in ['mobile', 'desktop']:
            device_df = test_df[test_df['device'] == device.upper()]  # Ensure matching the exact case
            device_summary = calculate_summary(device_df)
            if 'EXPERIMENT' in device_summary.index and 'BASE' in device_summary.index:
                device_summary.loc['% Difference'] = calculate_percentage_difference(
                    device_summary.loc['EXPERIMENT'], device_summary.loc['BASE'])
            
            ws.append([])
            ws.append([f'{device.capitalize()}:'])
            for r in dataframe_to_rows(device_summary.reset_index(), index=False, header=True):
                ws.append(r)
        
        # Campaign-specific summary
        for campaign_name in test_df['campaign_base_campaign_name'].unique():
            campaign_df = test_df[test_df['campaign_base_campaign_name'] == campaign_name]
            campaign_summary = calculate_summary(campaign_df)
            if 'EXPERIMENT' in campaign_summary.index and 'BASE' in campaign_summary.index:
                campaign_summary.loc['% Difference'] = calculate_percentage_difference(
                    campaign_summary.loc['EXPERIMENT'], campaign_summary.loc['BASE'])
            
            ws.append([])
            ws.append([f'Campaign name: {campaign_name}'])
            for r in dataframe_to_rows(campaign_summary.reset_index(), index=False, header=True):
                ws.append(r)
            
            # Mobile and Desktop summary for each campaign
            for device in ['mobile', 'desktop']:
                device_campaign_df = campaign_df[campaign_df['device'] == device.upper()]  # Ensure matching the exact case
                device_campaign_summary = calculate_summary(device_campaign_df)
                if 'EXPERIMENT' in device_campaign_summary.index and 'BASE' in device_campaign_summary.index:
                    device_campaign_summary.loc['% Difference'] = calculate_percentage_difference(
                        device_campaign_summary.loc['EXPERIMENT'], device_campaign_summary.loc['BASE'])
                
                ws.append([])
                ws.append([f'{device.capitalize()}:'])
                for r in dataframe_to_rows(device_campaign_summary.reset_index(), index=False, header=True):
                    ws.append(r)
    
    # Save the workbook
    wb.save(output_file)

# Main execution
if __name__ == "__main__":
    input_file = "reports/output/updated_final_data.xlsx"
    output_file = "reports/output/experiments_summary_metrics.xlsx"
    create_summary_workbook(input_file, output_file)
