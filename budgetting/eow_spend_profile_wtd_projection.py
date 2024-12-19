# file name:
# version: V000-000-004
# output:
# Note: This version builds on V000-000-002 and incorporates a wtd trend factor

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

# ------------------ MANUAL ADJUSTMENT FACTOR ------------------ # 
# Define manual adjustment factors (e.g., 1.04 for 4% growth) in times of anomalies or budget changes
manual_adjustments = {
    # "SEM Brand": 0.9
    # "SEM Non-Brand": 1.07
    # 'SEM Non-Brand - Tenant': 1.04  # Adjust this dictionary for other channel groups if needed
}

# ------------------ TREND FACTOR WITH EMA ------------------ # 
# Step 1: Collect daily historical data (including current week up to reference_date)
historical_days = 14  # Number of days to consider for trend calculation (2 weeks)
historical_daily_data = df_filtered[(df_filtered['Date'] >= reference_date - timedelta(days=historical_days)) & 
                                    (df_filtered['Date'] <= reference_date)]

# Step 2: Calculate daily metrics and EMA for each Channel Group
ema_trend_factors = {}

for group in channel_groups:
    # Filter data for the specific group
    group_data = historical_daily_data[historical_daily_data['Channel_Group'] == group]

    # Calculate daily spend over the specified period
    daily_spend = group_data.groupby('Date')['Cost'].sum()

    # Calculate the EMA with higher weight on recent days (alpha = 0.8 gives more weight to recent values)
    # You can adjust the alpha parameter based on your preference (e.g., higher value = more weight on recent days)
    ema_spend = daily_spend.ewm(alpha=0.9, adjust=False).mean()
    
    # Calculate the EMA trend factor based on the ratio of the last 3 days of this week to the EMA value
    # This gives more weight to recent days compared to older days
    if len(ema_spend) > 0:
        recent_spend = daily_spend[-3:].sum()
        trend_factor = recent_spend / ema_spend[-3:].sum() if ema_spend[-3:].sum() != 0 else 1
    else:
        trend_factor = 1  # Neutral trend if no data

    ema_trend_factors[group] = trend_factor

# ----------------------------------------------------------- #

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

    # Apply EMA-based trend factor to projections
    if group in ema_trend_factors:
        ema_trend_factor = ema_trend_factors[group]
        projected_spend *= ema_trend_factor
        projected_purchase *= ema_trend_factor
        projected_lead *= ema_trend_factor
        projected_brsubmit *= ema_trend_factor
        projected_dm_submit *= ema_trend_factor
        projected_phoneget *= ema_trend_factor
    
    # Apply manual adjustment if specified and needed
    if group in manual_adjustments:
        adjustment_factor = manual_adjustments[group]
        if adjustment_factor != ema_trend_factor:
            projected_spend = wtd_spend * adjustment_factor
            projected_purchase = wtd_purchase_count * adjustment_factor
            projected_lead = wtd_lead_count * adjustment_factor
            projected_brsubmit = wtd_brsubmit_count * adjustment_factor
            projected_dm_submit = wtd_dm_submit_count * adjustment_factor
            projected_phoneget = wtd_phoneget_count * adjustment_factor

    # Calculate summed metrics for current WTD + projected
    total_spend = wtd_spend + projected_spend
    total_purchase = wtd_purchase_count + projected_purchase
    total_lead = wtd_lead_count + projected_lead
    total_brsubmit = wtd_brsubmit_count + projected_brsubmit
    total_dm_submit = wtd_dm_submit_count + projected_dm_submit
    total_phoneget = wtd_phoneget_count + projected_phoneget

    # Add to summary
    summary_projections.append({
        'Channel Group': group,
        'WTD Spend': f"${wtd_spend:,.2f}",
        'EMA Trend Factor': ema_trend_factor,
        'Manual Adjustment Factor': manual_adjustments.get(group, None),
        'Projected Full Week Spend': f"${projected_spend:,.2f}",
        'Total Spend (WTD + Projected)': f"${total_spend:,.2f}",
        
        'WTD Purchases': f"{wtd_purchase_count:,.2f}",
        'Projected Full Week Purchases': f"{projected_purchase:,.2f}",
        'Total Purchases (WTD + Projected)': f"{total_purchase:,.2f}",
        
        'WTD Leads': f"{wtd_lead_count:,.2f}",
        'Projected Full Week Leads': f"{projected_lead:,.2f}",
        'Total Leads (WTD + Projected)': f"{total_lead:,.2f}",
        
        'WTD BR Submits': f"{wtd_brsubmit_count:,.2f}",
        'Projected Full Week BR Submits': f"{projected_brsubmit:,.2f}",
        'Total BR Submits (WTD + Projected)': f"{total_brsubmit:,.2f}",
        
        'WTD DM Submits': f"{wtd_dm_submit_count:,.2f}",
        'Projected Full Week DM Submits': f"{projected_dm_submit:,.2f}",
        'Total DM Submits (WTD + Projected)': f"{total_dm_submit:,.2f}",
        
        'WTD Phone Gets': f"{wtd_phoneget_count:,.2f}",
        'Projected Full Week Phone Gets': f"{projected_phoneget:,.2f}",
        'Total Phone Gets (WTD + Projected)': f"{total_phoneget:,.2f}"
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
