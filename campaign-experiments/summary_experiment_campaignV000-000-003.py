# file name: campaign-experiments/summary_experiment_campaign.py
# version number: V000-000-003
# Note: Connects to the resulting tables from campaign-experiments/experiment_campaigns_fact_procedure.py and uses reports/output/final_data.xlsx
#       Creates a reference date and sets a start and end time

import pandas as pd
import numpy as np
from openpyxl import Workbook
from openpyxl.utils.dataframe import dataframe_to_rows

def create_summary_workbook(input_file, output_file, reference_date):
    # Load the data
    df = pd.read_excel(input_file)
    
    # Print column names to debug
    print("Column names in the DataFrame:")
    print(df.columns)
    
    # Clean up column names by stripping leading/trailing spaces and converting to lower case
    df.columns = df.columns.str.strip().str.lower()
    
    # Ensure test_name is a string and handle NaN values
    df['test_name'] = df['test_name'].astype(str).fillna('Unknown')
    
    # Ensure dates are in datetime format (assuming you have a 'date' column for filtering)
    df['date'] = pd.to_datetime(df['date'], errors='coerce')  # Adjust this to match your actual date column
    
    # Calculate the start and end date based on the ISO week of the reference date
    iso_year, iso_week, iso_day = reference_date.isocalendar()
    
    # Calculate the start (Monday) and end (Sunday) of the ISO week
    start_date = reference_date - pd.Timedelta(days=reference_date.weekday())  # Monday of the ISO week
    end_date = start_date + pd.Timedelta(days=6)  # Sunday of the same ISO week
    
    # Filter the data based on the ISO week
    filtered_df = df[(df['date'] >= start_date) & (df['date'] <= end_date)]
    
    # Check if the filtered data exists
    if filtered_df.empty:
        raise ValueError(f"No data available between {start_date} and {end_date}")
    
    # Fill blank values in 'experiment_type' with 'Unknown'
    filtered_df['experiment_type'] = filtered_df['experiment_type'].astype(str).fillna('Unknown')
    
    # Verify required columns exist
    required_columns = ['experiment_type', 'device', 'campaign_base_campaign_name', 'impressions', 'clicks', 'cost', 
                        'furnished finder - ga4 (web) ff lead', 'furnished finder - ga4 (web) ff purchase']
    for col in required_columns:
        if col not in filtered_df.columns:
            raise KeyError(f"Column '{col}' not found in the input data.")
    
    # Print out the first few rows to check for correct loading
    print(filtered_df.head())

    # Create a new workbook
    wb = Workbook()
    wb.remove(wb.active)  # Remove the default sheet
    
    # List of metrics to calculate
    metrics = ['impressions', 'clicks', 'cost', 'furnished finder - ga4 (web) ff lead', 'furnished finder - ga4 (web) ff purchase', 
               'furnished finder - ga4 (web) ff-brsubmit', 'furnished finder - ga4 (web) ff-dmsubmit', 'furnished finder - ga4 (web) ff-phoneget', 
               'combined_tenant_metrics']

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
        summary['roas'] = ((summary['furnished finder - ga4 (web) ff purchase'] * 170 ) + 
                           (summary['furnished finder - ga4 (web) ff-brsubmit'] * 25) + 
                           (summary['furnished finder - ga4 (web) ff-dmsubmit'] * 8.75) + 
                           (summary['furnished finder - ga4 (web) ff-phoneget'] * 3.75)) / summary['cost']
        summary = summary.replace([np.inf, -np.inf], np.nan).fillna(0)
        return summary

    # Function to calculate percentage difference
    def calculate_percentage_difference(experiment, base):
        return ((experiment - base) / base) #* 100

    # Process each test_name
    for test_name in filtered_df['test_name'].unique():
        test_df = filtered_df[filtered_df['test_name'] == test_name]
        
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
        ws.append(['Start date:', str(start_date), 'End date:', str(end_date)])
        
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
    reference_date = pd.Timestamp("2024-09-02")  # Set your reference date here
    input_file = "reports/output/updated_final_data.xlsx"
    output_file = "reports/output/experiments_summary_metrics.xlsx"
    create_summary_workbook(input_file, output_file, reference_date)
