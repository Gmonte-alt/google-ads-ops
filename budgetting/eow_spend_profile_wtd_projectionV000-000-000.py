# file name: eow_spend_profile_wtd_projection.py
# version: V000-000-000
# output:
# notes: this version calculates projection based on the daily average performance wtd, this week, and estimates rest of week based on that average, then adjusts based on WoW WTD

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
df_filtered.loc[:, 'Week'] = df_filtered['Date'].dt.isocalendar().week
df_filtered.loc[:, 'Year'] = df_filtered['Date'].dt.isocalendar().year

# Step 4: Calculate the current ISO week dynamically
current_iso_week = reference_date.isocalendar().week

# Calculate the number of days in the WTD data, ensuring it's filtered for the current year
wtd_days = df_filtered[(df_filtered['Week'] == current_iso_week) & (df_filtered['Year'] == reference_date.year) & (df_filtered['Date'] <= reference_date)].groupby('Channel_Group')['Date'].nunique().max()

# Step 5: Calculate WTD spend and metrics for the current ISO week (current year only)
wtd_metrics = df_filtered[(df_filtered['Week'] == current_iso_week) & (df_filtered['Year'] == reference_date.year) & (df_filtered['Date'] <= reference_date)].groupby('Channel_Group').agg({
    'Cost': 'sum',
    'FF_Purchase_Event_Count': 'sum',
    'FF_Lead_Event_Count': 'sum',
    'FF_BRSubmit_Event_Count': 'sum',
    'FF_DMSubmit_Event_Count': 'sum',
    'FF_PhoneGet_Event_Count': 'sum'
}).reset_index()

# Debugging step: Print the WTD metrics to verify the values
print("WTD Metrics Debugging Information:")
print(wtd_metrics)

# Step 6: Calculate the previous ISO week dynamically
previous_iso_week = current_iso_week - 1 if current_iso_week > 1 else 52

# Step 7: Calculate last week's WTD performance for the same number of days (current year only)
previous_wtd_metrics = df_filtered[(df_filtered['Week'] == previous_iso_week) & (df_filtered['Year'] == reference_date.year) & (df_filtered['Date'].dt.weekday < wtd_days)].groupby('Channel_Group').agg({
    'Cost': 'sum',
    'FF_Purchase_Event_Count': 'sum',
    'FF_Lead_Event_Count': 'sum',
    'FF_BRSubmit_Event_Count': 'sum',
    'FF_DMSubmit_Event_Count': 'sum',
    'FF_PhoneGet_Event_Count': 'sum'
}).reset_index()

# Initialize dictionary to store WoW adjustment factors for each metric
wow_adjustment_factors = {}

# Calculate the WoW adjustment factors for each metric
for group in channel_groups:
    group_data_current = wtd_metrics[wtd_metrics['Channel_Group'] == group]
    group_data_previous = previous_wtd_metrics[previous_wtd_metrics['Channel_Group'] == group]
    
    wow_adjustment_factors[group] = {
        'Cost': group_data_current['Cost'].values[0] / group_data_previous['Cost'].values[0] if group_data_previous['Cost'].values[0] != 0 else 1,
        'FF_Purchase_Event_Count': group_data_current['FF_Purchase_Event_Count'].values[0] / group_data_previous['FF_Purchase_Event_Count'].values[0] if group_data_previous['FF_Purchase_Event_Count'].values[0] != 0 else 1,
        'FF_Lead_Event_Count': group_data_current['FF_Lead_Event_Count'].values[0] / group_data_previous['FF_Lead_Event_Count'].values[0] if group_data_previous['FF_Lead_Event_Count'].values[0] != 0 else 1,
        'FF_BRSubmit_Event_Count': group_data_current['FF_BRSubmit_Event_Count'].values[0] / group_data_previous['FF_BRSubmit_Event_Count'].values[0] if group_data_previous['FF_BRSubmit_Event_Count'].values[0] != 0 else 1,
        'FF_DMSubmit_Event_Count': group_data_current['FF_DMSubmit_Event_Count'].values[0] / group_data_previous['FF_DMSubmit_Event_Count'].values[0] if group_data_previous['FF_DMSubmit_Event_Count'].values[0] != 0 else 1,
        'FF_PhoneGet_Event_Count': group_data_current['FF_PhoneGet_Event_Count'].values[0] / group_data_previous['FF_PhoneGet_Event_Count'].values[0] if group_data_previous['FF_PhoneGet_Event_Count'].values[0] != 0 else 1
    }

# Initialize dictionary to store final projections for each metric
final_projections = {}

# Calculate the number of days left in the current ISO week
days_left_in_week = 7 - wtd_days

# Step 8: Calculate projections for each metric and prepare the summary
summary_projections = []

for group in channel_groups:
    # WTD values for each metric
    group_data = wtd_metrics[wtd_metrics['Channel_Group'] == group]
    
    wtd_spend = group_data['Cost'].values[0]
    wtd_purchase_count = group_data['FF_Purchase_Event_Count'].values[0]
    wtd_lead_count = group_data['FF_Lead_Event_Count'].values[0]
    wtd_brsubmit_count = group_data['FF_BRSubmit_Event_Count'].values[0]
    wtd_dm_submit_count = group_data['FF_DMSubmit_Event_Count'].values[0]
    wtd_phoneget_count = group_data['FF_PhoneGet_Event_Count'].values[0]

    # Average daily values based on WTD data
    average_daily_spend = wtd_spend / wtd_days
    average_daily_purchase = wtd_purchase_count / wtd_days
    average_daily_lead = wtd_lead_count / wtd_days
    average_daily_brsubmit = wtd_brsubmit_count / wtd_days
    average_daily_dm_submit = wtd_dm_submit_count / wtd_days
    average_daily_phoneget = wtd_phoneget_count / wtd_days

    # Project remaining spend and other metrics based on days left in the week
    remaining_spend = average_daily_spend * days_left_in_week
    remaining_purchase = average_daily_purchase * days_left_in_week
    remaining_lead = average_daily_lead * days_left_in_week
    remaining_brsubmit = average_daily_brsubmit * days_left_in_week
    remaining_dm_submit = average_daily_dm_submit * days_left_in_week
    remaining_phoneget = average_daily_phoneget * days_left_in_week

    # Calculate total projected spend and metrics before adjustment
    total_projected_spend = wtd_spend + remaining_spend
    total_projected_purchase = wtd_purchase_count + remaining_purchase
    total_projected_lead = wtd_lead_count + remaining_lead
    total_projected_brsubmit = wtd_brsubmit_count + remaining_brsubmit
    total_projected_dm_submit = wtd_dm_submit_count + remaining_dm_submit
    total_projected_phoneget = wtd_phoneget_count + remaining_phoneget

    # Apply the WoW adjustment factors to each metric
    adjusted_spend = total_projected_spend * wow_adjustment_factors[group]['Cost']
    adjusted_purchase = total_projected_purchase * wow_adjustment_factors[group]['FF_Purchase_Event_Count']
    adjusted_lead = total_projected_lead * wow_adjustment_factors[group]['FF_Lead_Event_Count']
    adjusted_brsubmit = total_projected_brsubmit * wow_adjustment_factors[group]['FF_BRSubmit_Event_Count']
    adjusted_dm_submit = total_projected_dm_submit * wow_adjustment_factors[group]['FF_DMSubmit_Event_Count']
    adjusted_phoneget = total_projected_phoneget * wow_adjustment_factors[group]['FF_PhoneGet_Event_Count']

    # Store the final projections
    final_projections[group] = {
        'Cost': adjusted_spend,
        'FF_Purchase_Event_Count': adjusted_purchase,
        'FF_Lead_Event_Count': adjusted_lead,
        'FF_BRSubmit_Event_Count': adjusted_brsubmit,
        'FF_DMSubmit_Event_Count': adjusted_dm_submit,
        'FF_PhoneGet_Event_Count': adjusted_phoneget
    }

    # Add to summary
    summary_projections.append({
        'Channel Group': group,
        'WTD Spend': f"${wtd_spend:,.2f}",
        'Average Daily Spend': f"${average_daily_spend:,.2f}",
        'Projected Remaining Spend': f"${remaining_spend:,.2f}",
        'Total Projected Spend': f"${total_projected_spend:,.2f}",
        'WoW Adjusted Spend': f"${adjusted_spend:,.2f}",

        'WTD Purchases': f"{wtd_purchase_count:,.2f}",
        'Average Daily Purchases': f"{average_daily_purchase:,.2f}",
        'Projected Remaining Purchases': f"{remaining_purchase:,.2f}",
        'Total Projected Purchases': f"{total_projected_purchase:,.2f}",
        'WoW Adjusted Purchases': f"{adjusted_purchase:,.2f}",

        'WTD Leads': f"{wtd_lead_count:,.2f}",
        'Average Daily Leads': f"{average_daily_lead:,.2f}",
        'Projected Remaining Leads': f"{remaining_lead:,.2f}",
        'Total Projected Leads': f"{total_projected_lead:,.2f}",
        'WoW Adjusted Leads': f"{adjusted_lead:,.2f}",

        'WTD BR Submits': f"{wtd_brsubmit_count:,.2f}",
        'Average Daily BR Submits': f"{average_daily_brsubmit:,.2f}",
        'Projected Remaining BR Submits': f"{remaining_brsubmit:,.2f}",
        'Total Projected BR Submits': f"{total_projected_brsubmit:,.2f}",
        'WoW Adjusted BR Submits': f"{adjusted_brsubmit:,.2f}",

        'WTD DM Submits': f"{wtd_dm_submit_count:,.2f}",
        'Average Daily DM Submits': f"{average_daily_dm_submit:,.2f}",
        'Projected Remaining DM Submits': f"{remaining_dm_submit:,.2f}",
        'Total Projected DM Submits': f"{total_projected_dm_submit:,.2f}",
        'WoW Adjusted DM Submits': f"{adjusted_dm_submit:,.2f}",

        'WTD Phone Gets': f"{wtd_phoneget_count:,.2f}",
        'Average Daily Phone Gets': f"{average_daily_phoneget:,.2f}",
        'Projected Remaining Phone Gets': f"{remaining_phoneget:,.2f}",
        'Total Projected Phone Gets': f"{total_projected_phoneget:,.2f}",
        'WoW Adjusted Phone Gets': f"{adjusted_phoneget:,.2f}"
    })

# Display the summary projections
print("Updated Summary of Projections:")
print("Channel Group\tWTD Spend\tAvg Daily Spend\tProj Rem Spend\tTotal Proj Spend\tWoW Adj Spend\tWTD Purchases\tAvg Daily Purchases\tProj Rem Purchases\tTotal Proj Purchases\tWoW Adj Purchases")
for projection in summary_projections:
    print(f"{projection['Channel Group']}\t{projection['WTD Spend']}\t{projection['Average Daily Spend']}\t{projection['Projected Remaining Spend']}\t{projection['Total Projected Spend']}\t{projection['WoW Adjusted Spend']}\t"
          f"{projection['WTD Purchases']}\t{projection['Average Daily Purchases']}\t{projection['Projected Remaining Purchases']}\t{projection['Total Projected Purchases']}\t{projection['WoW Adjusted Purchases']}")

# Convert the summary projections to a DataFrame
summary_df = pd.DataFrame(summary_projections)

# Define the output path for the CSV file
output_csv_path = 'budgetting/output/summary_projections.csv'

# Save the DataFrame to a CSV file
summary_df.to_csv(output_csv_path, index=False)

# Confirmation message
print(f"Summary of projections saved to {output_csv_path}")
