# file name:
# version: V000-000-001
# output:
# notes: This version builds on the base version of the MTD budget pacing script. It now incorporates channel & campaign grouping

import pandas as pd
from datetime import datetime, timedelta

# Load CSV data
data = pd.read_csv('ga4-reports/output/ga4_combined_data.csv') # 'mtd_performance.csv')

# Automate the calculation of the reference_date as the day before yesterday
reference_date = datetime.now()
reference_date = reference_date.replace(hour=0, minute=0, second=0, microsecond=0)
end_date = reference_date - timedelta(days=2)
print(reference_date)
print(end_date)

# Convert Date column from integer format YYYYMMDD to datetime
data['Date'] = pd.to_datetime(data['Date'].astype(str), format='%Y%m%d')

# Filter data up to the reference date (two days ago) and within the current month
data['Date'] = pd.to_datetime(data['Date'])

start_of_month = reference_date.replace(day=1)
data = data[(data['Date'] <= end_date) & (data['Date'] >= start_of_month)]
print(start_of_month)

# Calculate the number of days passed in the current month
days_passed = (end_date - start_of_month).days + 1  # Including the current day
print(days_passed)

# Calculate days left in the month
days_in_month = (reference_date.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)
days_left = (days_in_month - reference_date).days + 1

# Filter data to only include rows where Channel_Group contains "SEM"
data = data[data['Channel_Group'].str.contains('SEM')]

# Rename columns in the DataFrame
data.rename(columns={
    'Cost': 'Spend',
    'FF_Purchase_Event_Count': 'New Properties'
}, inplace=True)

# Calculate 'Tenant Actions' by summing the three relevant metrics
data['Tenant Actions'] = (
    data['FF_BRSubmit_Event_Count'] + 
    data['FF_DMSubmit_Event_Count'] + 
    data['FF_PhoneGet_Event_Count']
)

# Define the budget for each combination of Channel_Group and Campaign_Group
budget_dict = {
    ('SEM Brand', 'Google Search - Brand'): {'Spend': 73652, 'New Properties': 2447, 'Tenant Actions': 71141},
    ('SEM Brand', 'Google Search - Brand Broad'): {'Spend': 91974, 'New Properties': 321, 'Tenant Actions': 11573},
    ('SEM Non-Brand', 'Google LL Non-Brand'): {'Spend': 86800, 'New Properties': 208, 'Tenant Actions': 3385},
    ('SEM Non-Brand - Tenant', 'Google Tenant'): {'Spend': 64218, 'New Properties': 120, 'Tenant Actions': 21682},
    ('SEM Brand', 'Bing Brand'): {'Spend': 12189, 'New Properties': 521, 'Tenant Actions': 14025},
    ('SEM Non-Brand', 'Bing LL Non-Brand'): {'Spend': 78951, 'New Properties': 245, 'Tenant Actions': 4203},
    ('SEM Brand', 'Google TNH Brand'): {'Spend': 25270, 'New Properties': 132, 'Tenant Actions': 1404},
    ('SEM Non-Brand', 'Google TNH Non-Brand'): {'Spend': 40698, 'New Properties': 212, 'Tenant Actions': 2261}
}

# Initialize dictionaries for MTD metrics and budgets
mtd_metrics = {}
mtd_budget = {}
ratios = {}
mtd_ratios = {}

# Calculate the number of days passed in the current month
days_passed = (end_date - start_of_month).days + 1  # Including the current day

# Filter and aggregate data based on Channel_Group and Campaign_Group
for (channel_group, campaign_group_name), budgets in budget_dict.items():
    if "Google LL Non-Brand" in campaign_group_name:
        filtered_data = data[(data['Channel_Group'] == channel_group) & 
                             (data['Campaign_Group'].str.contains('Google') & ~data['Campaign_Group'].str.contains('TNH'))]
    elif "Google Tenant" in campaign_group_name:
        filtered_data = data[(data['Channel_Group'] == channel_group) & 
                             (data['Campaign_Group'].str.contains('Google') & ~data['Campaign_Group'].str.contains('TNH'))]
    elif "Bing Brand" in campaign_group_name:
        filtered_data = data[(data['Channel_Group'] == channel_group) & 
                             (data['Campaign_Group'].str.contains('Bing'))]
    elif "Bing LL Non-Brand" in campaign_group_name:
        filtered_data = data[(data['Channel_Group'] == channel_group) & 
                             (data['Campaign_Group'].str.contains('Bing'))]
    elif "Google TNH Brand" in campaign_group_name:
        filtered_data = data[(data['Channel_Group'] == channel_group) & 
                             (data['Campaign_Group'].str.contains('TNH'))]
    elif "Google TNH Non-Brand" in campaign_group_name:
        filtered_data = data[(data['Channel_Group'] == channel_group) & 
                             (data['Campaign_Group'].str.contains('TNH'))]
    else:
        filtered_data = data[(data['Channel_Group'] == channel_group) & 
                             (data['Campaign_Group'] == campaign_group_name)]

    # Aggregate MTD performance for the filtered data
    mtd_performance = filtered_data.agg({
        'Spend': 'sum',
        'New Properties': 'sum',
        'Tenant Actions': 'sum'
    })
    
    # Calculate MTD metrics
    mtd_metrics[(channel_group, campaign_group_name)] = mtd_performance.to_dict()
    
    # Calculate the MTD budget by prorating the monthly budget
    mtd_budget[(channel_group, campaign_group_name)] = {
        metric: (budget / days_in_month.day) * days_passed
        for metric, budget in budgets.items()
    }
    
    # Calculate the ratio of MTD to total budget
    ratios[(channel_group, campaign_group_name)] = {
        metric: mtd_metrics[(channel_group, campaign_group_name)][metric] / budgets[metric]
        for metric in mtd_metrics[(channel_group, campaign_group_name)]
    }
    
    # Calculate the ratio of MTD to MTD budget
    mtd_ratios[(channel_group, campaign_group_name)] = {
        metric: mtd_metrics[(channel_group, campaign_group_name)][metric] / mtd_budget[(channel_group, campaign_group_name)][metric]
        for metric in mtd_metrics[(channel_group, campaign_group_name)]
    }

# Compile the final report
report = {
    'MTD Metrics': mtd_metrics,
    'MTD Budget': mtd_budget,
    'Ratio of MTD to MTD Budget': mtd_ratios,
    'Ratio of MTD to Total Budget': ratios,
}

# Display the report
for key, values in report.items():
    print(f"\n{key}:")
    for (channel_group, campaign_group_name), metrics in values.items():
        print(f"  Channel Group: {channel_group}, Campaign Group: {campaign_group_name}")
        for metric, value in metrics.items():
            print(f"    {metric}: {value:.2f}")
