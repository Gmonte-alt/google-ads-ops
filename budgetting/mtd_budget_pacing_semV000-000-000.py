# file name:
# version: V000-000-000
# output:
# notes: This is a base version of the MTD budget pacing script. It looks at total spend, new properties & TA and displays the performance vs budget

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

# Aggregate MTD performance by Channel_Group
mtd_performance = data.groupby('Channel_Group').agg({
    'Spend': 'sum',
    'New Properties': 'sum',
    'Tenant Actions': 'sum'
}).reset_index()


# Define the monthly budget for each metric
monthly_budget = {
    'Spend': 473751,  # Example values
    'New Properties': 4204,
    'Tenant Actions': 129674
}

# Calculate MTD metrics
mtd_metrics = {
    'Spend': mtd_performance['Spend'].sum(),
    'New Properties': mtd_performance['New Properties'].sum(),
    'Tenant Actions': mtd_performance['Tenant Actions'].sum()
}

# Calculate remaining budget for the month
remaining_budget = {
    metric: monthly_budget[metric] - mtd_metrics[metric]
    for metric in mtd_metrics
}

# Calculate days left in the month
days_in_month = (reference_date.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)
days_left = (days_in_month - reference_date).days + 1

# Calculate the ratio of MTD to total budget and budget per day left
ratios = {
    metric: mtd_metrics[metric] / monthly_budget[metric]
    for metric in mtd_metrics
}

budget_per_day_left = {
    metric: remaining_budget[metric] / days_left
    for metric in remaining_budget
}

# Calculate the MTD budget by prorating the monthly budget
mtd_budget = {
    metric: (monthly_budget[metric] / days_in_month.day) * days_passed
    for metric in monthly_budget
}

mtd_ratios = {
    metric: mtd_metrics[metric] / mtd_budget[metric]
    for metric in mtd_metrics
}

# Compile the final report
report = {
    'MTD Metrics': mtd_metrics,
    'MTD Budget': mtd_budget,
    'Ratio of MTD to MTD Budget': mtd_ratios,
    'Month Total Budget': monthly_budget,
    'Ratio of MTD to Total Budget': ratios,
    'Remaining Budget': remaining_budget,
    'Budget per Day Left': budget_per_day_left
}

# Display the report
for metric, values in report.items():
    print(f"\n{metric}:")
    for key, value in values.items():
        print(f"  {key}: {value:.2f}")
