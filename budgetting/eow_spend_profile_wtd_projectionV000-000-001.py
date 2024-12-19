# file name:
# version: V000-000-001
# output:
# Note: This version takes the WTD distribution of last week and extrapolates this week's EOW projections

import pandas as pd
import numpy as np
from datetime import datetime, timedelta

# Automate the calculation of the reference_date as the day before yesterday
reference_date = datetime.now() - timedelta(days=2)
reference_date = reference_date.replace(hour=0, minute=0, second=0, microsecond=0)

# Load the CSV file
file_path = 'ga4-reports/output/ga4_combined_data.csv'
df = pd.read_csv(file_path)

# Step 1: Filter the DataFrame based on the "Channel_Group" column
channel_groups = ["SEM Non-Brand", "SEM Non-Brand - Tenant", "SEM Brand"]
df_filtered = df[df['Channel_Group'].isin(channel_groups)].copy()

# Step 2: Explicitly cast the 'Date' column to datetime
df_filtered['Date'] = pd.to_datetime(df_filtered['Date'], format='%Y%m%d')

# Step 3: Add ISO Week and Year columns
df_filtered['Week'] = df_filtered['Date'].dt.isocalendar().week
df_filtered['Year'] = df_filtered['Date'].dt.isocalendar().year

# Step 4: Calculate the current ISO week dynamically
current_iso_week = reference_date.isocalendar().week

# Calculate the number of days in the WTD data (filtering by year to avoid including previous years)
wtd_days = df_filtered[(df_filtered['Week'] == current_iso_week) & (df_filtered['Year'] == reference_date.year) & (df_filtered['Date'] <= reference_date)].groupby('Channel_Group')['Date'].nunique().max()

# Step 5: Calculate WTD spend and metrics for the current ISO week
wtd_metrics = df_filtered[(df_filtered['Week'] == current_iso_week) & (df_filtered['Year'] == reference_date.year) & (df_filtered['Date'] <= reference_date)].groupby('Channel_Group').agg({
    'Cost': 'sum',
    'FF_Purchase_Event_Count': 'sum',
    'FF_Lead_Event_Count': 'sum',
    'FF_BRSubmit_Event_Count': 'sum',
    'FF_DMSubmit_Event_Count': 'sum',
    'FF_PhoneGet_Event_Count': 'sum'
}).reset_index()

# Step 6: Calculate last week's ISO week dynamically
previous_iso_week = current_iso_week - 1 if current_iso_week > 1 else 52

# Step 7: Calculate last week's WTD performance (for the same number of days) and the full week's performance
last_week_wtd_metrics = df_filtered[(df_filtered['Week'] == previous_iso_week) & (df_filtered['Year'] == reference_date.year) & (df_filtered['Date'].dt.weekday < wtd_days)].groupby('Channel_Group').agg({
    'Cost': 'sum',
    'FF_Purchase_Event_Count': 'sum',
    'FF_Lead_Event_Count': 'sum',
    'FF_BRSubmit_Event_Count': 'sum',
    'FF_DMSubmit_Event_Count': 'sum',
    'FF_PhoneGet_Event_Count': 'sum'
}).reset_index()

last_week_full_metrics = df_filtered[(df_filtered['Week'] == previous_iso_week) & (df_filtered['Year'] == reference_date.year)].groupby('Channel_Group').agg({
    'Cost': 'sum',
    'FF_Purchase_Event_Count': 'sum',
    'FF_Lead_Event_Count': 'sum',
    'FF_BRSubmit_Event_Count': 'sum',
    'FF_DMSubmit_Event_Count': 'sum',
    'FF_PhoneGet_Event_Count': 'sum'
}).reset_index()

# Step 8: Calculate ratios and projected full week performance for this week
summary_projections = []

for group in channel_groups:
    # Fetch WTD and full week metrics from last week
    last_week_wtd_data = last_week_wtd_metrics[last_week_wtd_metrics['Channel_Group'] == group]
    last_week_full_data = last_week_full_metrics[last_week_full_metrics['Channel_Group'] == group]

    # Calculate ratios (WTD / Full Week)
    ratio_spend = last_week_wtd_data['Cost'].values[0] / last_week_full_data['Cost'].values[0] if last_week_full_data['Cost'].values[0] != 0 else 1
    ratio_purchase = last_week_wtd_data['FF_Purchase_Event_Count'].values[0] / last_week_full_data['FF_Purchase_Event_Count'].values[0] if last_week_full_data['FF_Purchase_Event_Count'].values[0] != 0 else 1
    ratio_lead = last_week_wtd_data['FF_Lead_Event_Count'].values[0] / last_week_full_data['FF_Lead_Event_Count'].values[0] if last_week_full_data['FF_Lead_Event_Count'].values[0] != 0 else 1
    ratio_brsubmit = last_week_wtd_data['FF_BRSubmit_Event_Count'].values[0] / last_week_full_data['FF_BRSubmit_Event_Count'].values[0] if last_week_full_data['FF_BRSubmit_Event_Count'].values[0] != 0 else 1
    ratio_dm_submit = last_week_wtd_data['FF_DMSubmit_Event_Count'].values[0] / last_week_full_data['FF_DMSubmit_Event_Count'].values[0] if last_week_full_data['FF_DMSubmit_Event_Count'].values[0] != 0 else 1
    ratio_phoneget = last_week_wtd_data['FF_PhoneGet_Event_Count'].values[0] / last_week_full_data['FF_PhoneGet_Event_Count'].values[0] if last_week_full_data['FF_PhoneGet_Event_Count'].values[0] != 0 else 1

    # Fetch current week's WTD metrics
    current_wtd_data = wtd_metrics[wtd_metrics['Channel_Group'] == group]
    wtd_spend = current_wtd_data['Cost'].values[0]
    wtd_purchase_count = current_wtd_data['FF_Purchase_Event_Count'].values[0]
    wtd_lead_count = current_wtd_data['FF_Lead_Event_Count'].values[0]
    wtd_brsubmit_count = current_wtd_data['FF_BRSubmit_Event_Count'].values[0]
    wtd_dm_submit_count = current_wtd_data['FF_DMSubmit_Event_Count'].values[0]
    wtd_phoneget_count = current_wtd_data['FF_PhoneGet_Event_Count'].values[0]

    # Estimate full week performance based on the ratio
    projected_spend = wtd_spend / ratio_spend
    projected_purchase = wtd_purchase_count / ratio_purchase
    projected_lead = wtd_lead_count / ratio_lead
    projected_brsubmit = wtd_brsubmit_count / ratio_brsubmit
    projected_dm_submit = wtd_dm_submit_count / ratio_dm_submit
    projected_phoneget = wtd_phoneget_count / ratio_phoneget

    # Add to summary
    summary_projections.append({
        'Channel Group': group,
        'WTD Spend': f"${wtd_spend:,.2f}",
        'Projected Full Week Spend': f"${projected_spend:,.2f}",
        'WTD Purchases': f"{wtd_purchase_count:,.2f}",
        'Projected Full Week Purchases': f"{projected_purchase:,.2f}",
        'WTD Leads': f"{wtd_lead_count:,.2f}",
        'Projected Full Week Leads': f"{projected_lead:,.2f}",
        'WTD BR Submits': f"{wtd_brsubmit_count:,.2f}",
        'Projected Full Week BR Submits': f"{projected_brsubmit:,.2f}",
        'WTD DM Submits': f"{wtd_dm_submit_count:,.2f}",
        'Projected Full Week DM Submits': f"{projected_dm_submit:,.2f}",
        'WTD Phone Gets': f"{wtd_phoneget_count:,.2f}",
        'Projected Full Week Phone Gets': f"{projected_phoneget:,.2f}"
    })

# Display the summary projections
print("Updated Summary of Projections:")
for projection in summary_projections:
    print(f"{projection['Channel Group']}\t{projection['WTD Spend']}\t{projection['Projected Full Week Spend']}\t"
          f"{projection['WTD Purchases']}\t{projection['Projected Full Week Purchases']}\t"
          f"{projection['WTD Leads']}\t{projection['Projected Full Week Leads']}\t"
          f"{projection['WTD BR Submits']}\t{projection['Projected Full Week BR Submits']}\t"
          f"{projection['WTD DM Submits']}\t{projection['Projected Full Week DM Submits']}\t"
          f"{projection['WTD Phone Gets']}\t{projection['Projected Full Week Phone Gets']}")

# Convert the summary projections to a DataFrame
summary_df = pd.DataFrame(summary_projections)

# Define the output path for the CSV file
output_csv_path = 'budgetting/output/summary_projections.csv'

# Save the DataFrame to a CSV file
summary_df.to_csv(output_csv_path, index=False)

# Confirmation message
print(f"Summary of projections saved to {output_csv_path}")
