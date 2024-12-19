import pandas as pd
from datetime import datetime, timedelta

# Automate the calculation of the reference_date as the day before yesterday
reference_date = datetime.now() - timedelta(days=2)
reference_date = reference_date.replace(hour=0, minute=0, second=0, microsecond=0)

# Load the CSV file
file_path = 'ga4-reports/output/ga4_combined_data.csv'
df = pd.read_csv(file_path)

# Step 1: Filter the DataFrame based on the "Channel_Group" column and select "SEM Non-Brand"
sem_non_brand_data = df[(df['Channel_Group'] == 'SEM Non-Brand')].copy()

# Step 2: Explicitly cast the 'Date' column to datetime and add ISO Week and Year columns
sem_non_brand_data['Date'] = pd.to_datetime(sem_non_brand_data['Date'], format='%Y%m%d')
sem_non_brand_data['Week'] = sem_non_brand_data['Date'].dt.isocalendar().week
sem_non_brand_data['Year'] = sem_non_brand_data['Date'].dt.isocalendar().year

# Calculate the current ISO week dynamically
current_iso_week = reference_date.isocalendar().week

# Filter data for the current ISO week
sem_non_brand_wtd_data = sem_non_brand_data[(sem_non_brand_data['Week'] == current_iso_week) & (sem_non_brand_data['Date'] <= reference_date)]

# Step 2: Check for duplicate rows
duplicates = sem_non_brand_wtd_data[sem_non_brand_wtd_data.duplicated(keep=False)]

# Prepare the output CSV
output_dir = 'budgetting/output/'
wtd_output_path = output_dir + 'sem_non_brand_wtd_data.csv'
sem_non_brand_wtd_data.to_csv(wtd_output_path, index=False)

# Also save the potential duplicates if any
if not duplicates.empty:
    duplicates_output_path = output_dir + 'sem_non_brand_wtd_duplicates.csv'
    duplicates.to_csv(duplicates_output_path, index=False)

# Summarize key metrics
total_spend = sem_non_brand_wtd_data['Cost'].sum()
num_records = sem_non_brand_wtd_data.shape[0]

# Create a summary DataFrame
summary_data = {
    'Metric': ['Number of Records', 'Total Spend'],
    'Value': [num_records, total_spend]
}
summary_df = pd.DataFrame(summary_data)

# Save the summary information to a CSV
summary_output_path = output_dir + 'sem_non_brand_wtd_summary.csv'
summary_df.to_csv(summary_output_path, index=False)

# Print paths to the output files for reference
output_files_info = f"""
Filtered 'SEM Non-Brand' Data (WTD) saved to: {wtd_output_path}
Summary of WTD Data saved to: {summary_output_path}
"""

if not duplicates.empty:
    output_files_info += f"Potential Duplicates Found and saved to: {duplicates_output_path}\n"
else:
    output_files_info += "No Exact Duplicates Found in 'SEM Non-Brand' Data (WTD).\n"

output_files_info
