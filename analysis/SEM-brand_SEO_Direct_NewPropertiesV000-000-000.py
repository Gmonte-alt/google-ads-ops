# file name: analysis/SEM-brand_SEO_Direct_NewProperties.py
# version: V000-000-000
# Notes: Create the initial dataframe to conduct the analysis with
# file path: ga4-reports/output/ga4_wtd_adjusted_data.csv

# ----------------------- SAMPLE DATAFRAME OF INITIAL DATA ----------------- #

# import pandas as pd


# Sample DataFrame for illustration
# data = {
#     'iso_week': list(range(1, 53)),
#     'sem_brand_new_leads': [100 + i for i in range(52)],
#     'seo_new_leads': [50 + i for i in range(52)],
#     'direct_new_leads': [70 - i for i in range(52)],
#     'sem_brand_impression_share': [90 + (i * 0.5) for i in range(52)],
#     'sem_brand_new_properties': [30 + i for i in range(52)],
#     'seo_new_properties': [20 + (i * 1.1) for i in range(52)],
#     'direct_new_properties': [15 - (i * 0.7) for i in range(52)]
# }

# df = pd.DataFrame(data)

# data_path = 'analysis/output/sem-brand-seo-direct-data.csv'

# df.to_csv(data_path, index=False)
# print(f"Summary metrics for multiple periods have been written to {data_path}")
# print(df)

# ----------------------------------------------------------------------- #

import pandas as pd

# Load the CSV file
input_csv_file = 'ga4-reports/output/ga4_wtd_adjusted_data.csv'  # Replace with the actual file path
df = pd.read_csv(input_csv_file)

# Combine ISO_Year and ISO_Week to create 'iso_week'
df['iso_yr_week'] = df['ISO_Year'].astype(str) + '-W' + df['ISO_Week'].astype(str).str.zfill(2)

# Filter and create the desired metrics

# Create 'sem_brand_new_leads' by filtering Campaign_Group for 'SEM Brand Total' and using 'FF_Lead_Event_Count'
sem_brand_filter = df['Campaign_Group'].str.contains('SEM Brand Total', case=False, na=False)
df['sem_brand_new_leads'] = df[sem_brand_filter]['FF_Lead_Event_Count']

# Create 'seo_new_leads' by filtering Campaign_Group for 'Organic Search (SEO)' and using 'FF_Lead_Event_Count'
seo_filter = df['Campaign_Group'].str.contains('Organic Search', case=False, na=False)
df['seo_new_leads'] = df[seo_filter]['FF_Lead_Event_Count']

# Create 'direct_new_leads' by filtering Campaign_Group for 'Direct' and using 'FF_Lead_Event_Count'
direct_filter = df['Campaign_Group'].str.contains('Direct', case=False, na=False)
df['direct_new_leads'] = df[direct_filter]['FF_Lead_Event_Count']

# Create 'sem_brand_impression_share' by filtering Campaign_Group for 'SEM Brand Total' and using 'Impression_Share'
df['sem_brand_impression_share'] = df[sem_brand_filter]['Impression_Share']

# Create 'sem_brand_new_properties' by filtering Campaign_Group for 'SEM Brand Total' and using 'FF_Purchase_Event_Count'
df['sem_brand_new_properties'] = df[sem_brand_filter]['FF_Purchase_Event_Count']

# Create 'seo_new_properties' by filtering Campaign_Group for 'Organic Search (SEO)' and using 'FF_Purchase_Event_Count'
df['seo_new_properties'] = df[seo_filter]['FF_Purchase_Event_Count']

# Create 'direct_new_properties' by filtering Campaign_Group for 'Direct' and using 'FF_Purchase_Event_Count'
df['direct_new_properties'] = df[direct_filter]['FF_Purchase_Event_Count']

# Select relevant columns for the output
output_df = df[['iso_yr_week', 'sem_brand_new_leads', 'seo_new_leads', 'direct_new_leads',
                'sem_brand_impression_share', 'sem_brand_new_properties', 'seo_new_properties',
                'direct_new_properties']]

# Aggregate by iso_week and sum the numeric columns
output_df = output_df.groupby('iso_yr_week').sum().reset_index()

# Save the output to a CSV file
output_file = 'analysis/output/sem-brand-seo-direct-data.csv'  # Specify the desired output file path
output_df.to_csv(output_file, index=False)

print(f"Output saved to {output_file}")
