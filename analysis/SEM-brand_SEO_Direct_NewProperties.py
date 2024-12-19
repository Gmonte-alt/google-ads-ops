# file name:
# version:
# Notes:
# file path: ga4-reports/output/ga4_wtd_adjusted_data.csv

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

# ---------------------------------- Add Lagged Metrics onto the dataframe ----------------------------------------- #

# Creating lagged columns for SEM Brand new leads and impression share (3 to 5 week lags)
output_df['sem_brand_leads_lag_3'] = output_df['sem_brand_new_leads'].shift(3)
output_df['sem_brand_leads_lag_4'] = output_df['sem_brand_new_leads'].shift(4)
output_df['sem_brand_leads_lag_5'] = output_df['sem_brand_new_leads'].shift(5)
output_df['sem_brand_leads_lag_6'] = output_df['sem_brand_new_leads'].shift(6)
output_df['sem_brand_leads_lag_7'] = output_df['sem_brand_new_leads'].shift(7)
output_df['sem_brand_leads_lag_8'] = output_df['sem_brand_new_leads'].shift(8)
output_df['sem_brand_leads_lag_9'] = output_df['sem_brand_new_leads'].shift(9)
output_df['sem_brand_leads_lag_10'] = output_df['sem_brand_new_leads'].shift(10)
output_df['sem_brand_leads_lag_11'] = output_df['sem_brand_new_leads'].shift(11)

output_df['sem_brand_impression_share_lag_3'] = output_df['sem_brand_impression_share'].shift(3)
output_df['sem_brand_impression_share_lag_4'] = output_df['sem_brand_impression_share'].shift(4)
output_df['sem_brand_impression_share_lag_5'] = output_df['sem_brand_impression_share'].shift(5)
output_df['sem_brand_impression_share_lag_6'] = output_df['sem_brand_impression_share'].shift(6)
output_df['sem_brand_impression_share_lag_7'] = output_df['sem_brand_impression_share'].shift(7)
output_df['sem_brand_impression_share_lag_8'] = output_df['sem_brand_impression_share'].shift(8)
output_df['sem_brand_impression_share_lag_9'] = output_df['sem_brand_impression_share'].shift(9)
output_df['sem_brand_impression_share_lag_10'] = output_df['sem_brand_impression_share'].shift(10)
output_df['sem_brand_impression_share_lag_11'] = output_df['sem_brand_impression_share'].shift(11)

output_df.dropna(inplace=True)

# Save the output to a CSV file
output_file = 'analysis/output/sem-brand-seo-direct-data.csv'  # Specify the desired output file path
output_df.to_csv(output_file, index=False)

print(f"Output saved to {output_file}")

# Filter the data to include all weeks starting from ISO Week "2024-W13"
output_df = output_df[output_df['iso_yr_week'] >= "2024-W13"]

# ---------------------------------------- EXPLORE DATA - Data Visuals----------------------------------------------- #


import matplotlib.pyplot as plt

# Plotting SEM Brand, SEO, and Direct new leads and new properties over time
fig, axs = plt.subplots(3, 1, figsize=(10, 12), sharex=True)

# New Leads for each channel
axs[0].plot(output_df['iso_yr_week'], output_df['sem_brand_new_leads'], label='SEM Brand Leads', color='blue')
axs[0].plot(output_df['iso_yr_week'], output_df['seo_new_leads'], label='SEO Leads', color='green')
axs[0].plot(output_df['iso_yr_week'], output_df['direct_new_leads'], label='Direct Leads', color='red')
axs[0].set_title('New Leads Volume by Channel')
axs[0].legend()

# SEM Brand Impression Share
axs[1].plot(output_df['iso_yr_week'], output_df['sem_brand_impression_share'], label='SEM Brand Impression Share', color='purple')
axs[1].set_title('SEM Brand Impression Share')

# New Properties for each channel
axs[2].plot(output_df['iso_yr_week'], output_df['sem_brand_new_properties'], label='SEM Brand Properties', color='blue')
axs[2].plot(output_df['iso_yr_week'], output_df['seo_new_properties'], label='SEO Properties', color='green')
axs[2].plot(output_df['iso_yr_week'], output_df['direct_new_properties'], label='Direct Properties', color='red')
axs[2].set_title('New Properties by Channel')
axs[2].legend()

plt.xlabel('ISO Week')
plt.tight_layout()
plt.show()


# -------------------------------- Lagged Visuals -------------------------------------------------------------------------- #

# Plotting the lagged new leads against new property subscriptions
fig, axs = plt.subplots(2, 1, figsize=(10, 10), sharex=True)

# SEM Brand New Properties vs. SEM Brand Lagged Leads
axs[0].plot(output_df['iso_yr_week'], output_df['sem_brand_new_properties'], label='SEM Brand Properties', color='blue')
axs[0].plot(output_df['iso_yr_week'], output_df['sem_brand_leads_lag_3'], label='SEM Brand Leads (Lag 3 Weeks)', linestyle='--', color='red')
axs[0].plot(output_df['iso_yr_week'], output_df['sem_brand_leads_lag_4'], label='SEM Brand Leads (Lag 4 Weeks)', linestyle='--', color='green')
axs[0].plot(output_df['iso_yr_week'], output_df['sem_brand_leads_lag_5'], label='SEM Brand Leads (Lag 5 Weeks)', linestyle='--', color='orange')
axs[0].plot(output_df['iso_yr_week'], output_df['sem_brand_leads_lag_6'], label='SEM Brand Leads (Lag 6 Weeks)', linestyle='--', color='blue')
axs[0].set_title('SEM Brand: New Properties vs. Lagged Leads')
axs[0].legend()

# SEO New Properties vs. SEM Brand Lagged Leads
axs[1].plot(output_df['iso_yr_week'], output_df['seo_new_properties'], label='SEO Properties', color='green')
axs[1].plot(output_df['iso_yr_week'], output_df['sem_brand_leads_lag_3'], label='SEM Brand Leads (Lag 3 Weeks)', linestyle='--', color='red')
axs[1].plot(output_df['iso_yr_week'], output_df['sem_brand_leads_lag_4'], label='SEM Brand Leads (Lag 4 Weeks)', linestyle='--', color='green')
axs[1].plot(output_df['iso_yr_week'], output_df['sem_brand_leads_lag_5'], label='SEM Brand Leads (Lag 5 Weeks)', linestyle='--', color='orange')
axs[1].plot(output_df['iso_yr_week'], output_df['sem_brand_leads_lag_6'], label='SEM Brand Leads (Lag 6 Weeks)', linestyle='--', color='blue')
axs[1].set_title('SEO: New Properties vs. Lagged SEM Brand Leads')
axs[1].legend()

plt.xlabel('ISO Week')
plt.tight_layout()
plt.show()


# Plotting the lagged SEM Brand Impression Share against new property subscriptions
fig, axs = plt.subplots(2, 1, figsize=(10, 10), sharex=True)

# SEM Brand New Properties vs. SEM Brand Lagged Impression Share
ax1 = axs[0]
ax2 = ax1.twinx()  # Create a second Y-axis for SEM Brand Impression Share

# Primary axis - New Properties
ax1.plot(output_df['iso_yr_week'], output_df['sem_brand_new_properties'], label='SEM Brand Properties', color='blue')
ax1.set_ylabel('New Properties')

# Secondary axis - Lagged Impression Share
ax2.plot(output_df['iso_yr_week'], output_df['sem_brand_impression_share_lag_3'], label='SEM Brand Impression Share (Lag 3 Weeks)', linestyle='--', color='red')
ax2.plot(output_df['iso_yr_week'], output_df['sem_brand_impression_share_lag_4'], label='SEM Brand Impression Share (Lag 4 Weeks)', linestyle='--', color='green')
ax2.plot(output_df['iso_yr_week'], output_df['sem_brand_impression_share_lag_5'], label='SEM Brand Impression Share (Lag 5 Weeks)', linestyle='--', color='orange')
ax2.plot(output_df['iso_yr_week'], output_df['sem_brand_impression_share_lag_6'], label='SEM Brand Impression Share (Lag 6 Weeks)', linestyle='--', color='blue')
ax2.set_ylabel('SEM Brand Impression Share (%)')

# Legends and title
ax1.set_title('SEM Brand: New Properties vs. Lagged SEM Brand Impression Share')
ax1.legend(loc='upper left')
ax2.legend(loc='lower right', bbox_to_anchor=(1.1, 0.1))

# SEO New Properties vs. SEM Brand Lagged Impression Share
ax3 = axs[1]
ax4 = ax3.twinx()  # Create a second Y-axis for SEO

# Primary axis - SEO New Properties
ax3.plot(output_df['iso_yr_week'], output_df['seo_new_properties'], label='SEO Properties', color='green')
ax3.set_ylabel('New Properties')

# Secondary axis - Lagged Impression Share
ax4.plot(output_df['iso_yr_week'], output_df['sem_brand_impression_share_lag_3'], label='SEM Brand Impression Share (Lag 3 Weeks)', linestyle='--', color='red')
ax4.plot(output_df['iso_yr_week'], output_df['sem_brand_impression_share_lag_4'], label='SEM Brand Impression Share (Lag 4 Weeks)', linestyle='--', color='blue')
ax4.plot(output_df['iso_yr_week'], output_df['sem_brand_impression_share_lag_5'], label='SEM Brand Impression Share (Lag 5 Weeks)', linestyle='--', color='orange')
ax4.set_ylabel('SEM Brand Impression Share (%)')

# Legends and title
ax3.set_title('SEO: New Properties vs. Lagged SEM Brand Impression Share')
ax3.legend(loc='upper left')
ax4.legend(loc='lower right', bbox_to_anchor=(1.1, 0.1))

plt.xlabel('ISO Week')
plt.tight_layout()
plt.show()


# --------------------------------------------------------- SCATTER PLOT --------------------------------------------------------- #

# Filter the data to include all weeks starting from ISO Week "2024-W13"
filtered_df = output_df[output_df['iso_yr_week'] >= "2024-W13"]

# Check if filtered_df is not empty
if not filtered_df.empty:
    # Create a 2x2 grid of scatter plots for pairwise comparisons
    fig, axs = plt.subplots(2, 3, figsize=(12, 10))

    # SEM Brand new leads vs. SEO new properties
    axs[0, 0].scatter(filtered_df['sem_brand_new_leads'], filtered_df['seo_new_properties'], color='green')
    axs[0, 0].set_title('SEM Brand Leads vs. SEO Properties')
    axs[0, 0].set_xlabel('SEM Brand New Leads')
    axs[0, 0].set_ylabel('SEO New Properties')

    # SEM Brand new leads vs. Direct new properties
    axs[0, 1].scatter(filtered_df['sem_brand_new_leads'], filtered_df['direct_new_properties'], color='red')
    axs[0, 1].set_title('SEM Brand Leads vs. Direct Properties')
    axs[0, 1].set_xlabel('SEM Brand New Leads')
    axs[0, 1].set_ylabel('Direct New Properties')

    # SEM Brand new leads vs. SEM Brand new properties
    axs[0, 2].scatter(filtered_df['sem_brand_new_leads'], filtered_df['sem_brand_new_properties'], color='gray')
    axs[0, 2].set_title('SEM Brand Leads vs. SEM Brand Properties')
    axs[0, 2].set_xlabel('SEM Brand New Leads')
    axs[0, 2].set_ylabel('SEM Brand New Properties')

    # SEM Brand impression share vs. SEM Brand new properties
    axs[1, 2].scatter(filtered_df['sem_brand_impression_share'], filtered_df['sem_brand_new_properties'], color='blue')
    axs[1, 2].set_title('SEM Brand Impression Share vs. SEM Brand Properties')
    axs[1, 2].set_xlabel('SEM Brand Impression Share (%)')
    axs[1, 2].set_ylabel('SEM Brand New Properties')

    # SEM Brand impression share vs. SEO new properties
    axs[1, 0].scatter(filtered_df['sem_brand_impression_share'], filtered_df['seo_new_properties'], color='purple')
    axs[1, 0].set_title('SEM Brand Impression Share vs. SEO Properties')
    axs[1, 0].set_xlabel('SEM Brand Impression Share (%)')
    axs[1, 0].set_ylabel('SEO New Properties')

    # SEM Brand impression share vs. Direct new properties
    axs[1, 1].scatter(filtered_df['sem_brand_impression_share'], filtered_df['direct_new_properties'], color='olive')
    axs[1, 1].set_title('SEM Brand Impression Share vs. Direct Properties')
    axs[1, 1].set_xlabel('SEM Brand Impression Share (%)')
    axs[1, 1].set_ylabel('Direct New Properties')

    plt.tight_layout()
    plt.show()
else:
    print("No data available from ISO Week 2024-W13 onwards")


# --------------------- CORRELATION ANALYSIS ---------------------------------------------------------------#

# Import necessary library
import scipy.stats as stats

# List of variables to compare
variables = ['sem_brand_new_leads', 'sem_brand_leads_lag_3', 'sem_brand_leads_lag_4', 'sem_brand_leads_lag_5', 'sem_brand_leads_lag_6', 
             'sem_brand_leads_lag_7', 'sem_brand_leads_lag_8', 'sem_brand_leads_lag_9', 'sem_brand_leads_lag_10', 'sem_brand_leads_lag_11',    
             'sem_brand_impression_share', 'sem_brand_impression_share_lag_3', 'sem_brand_impression_share_lag_4', 'sem_brand_impression_share_lag_5', 
             'sem_brand_impression_share_lag_6', 'sem_brand_impression_share_lag_7', 'sem_brand_impression_share_lag_8', 'sem_brand_impression_share_lag_9', 
             'sem_brand_impression_share_lag_10', 'sem_brand_impression_share_lag_11']

# Correlation dictionaries to store results
pearson_corr = {}
spearman_corr = {}

# Loop through each variable and calculate correlations with SEM Brand and SEO new property subscriptions
for var in variables:
    pearson_corr[var + '_vs_sem_brand_properties'] = stats.pearsonr(filtered_df[var], filtered_df['sem_brand_new_properties'])
    pearson_corr[var + '_vs_seo_properties'] = stats.pearsonr(filtered_df[var], filtered_df['seo_new_properties'])
    pearson_corr[var + '_vs_direct_properties'] = stats.pearsonr(filtered_df[var], filtered_df['direct_new_properties'])
    
    spearman_corr[var + '_vs_sem_brand_properties'] = stats.spearmanr(filtered_df[var], filtered_df['sem_brand_new_properties'])
    spearman_corr[var + '_vs_seo_properties'] = stats.spearmanr(filtered_df[var], filtered_df['seo_new_properties'])
    spearman_corr[var + '_vs_direct_properties'] = stats.spearmanr(filtered_df[var], filtered_df['direct_new_properties'])

# Display Pearson correlations
print("Pearson Correlation Coefficients:")
for key, value in pearson_corr.items():
    print(f"{key}: Correlation = {value[0]:.4f}, P-value = {value[1]:.4f}")

# Display Spearman correlations
print("\nSpearman Correlation Coefficients:")
for key, value in spearman_corr.items():
    print(f"{key}: Correlation = {value.correlation:.4f}, P-value = {value.pvalue:.4f}")
    
# -------------------------------------------------------------------------------------------------------------------- #
# -------------------------------------------- REGRESSION MODELING --------------------------------------------------- #
# -------------------------------------------------------------------------------------------------------------------- #

# Adding interaction term for SEM Brand New Leads and SEM Brand Impression Share
filtered_df['interaction_term'] = filtered_df['sem_brand_leads_lag_6'] * filtered_df['sem_brand_impression_share']


import statsmodels.api as sm

# -------------------------------------- SEM Brand New Properties ---------------------------------------- #
# Independent variables
X = filtered_df[['sem_brand_leads_lag_6', 'sem_brand_impression_share']]

# Dependent variable (SEM Brand New Properties)
Y = filtered_df['sem_brand_new_properties']

# Add a constant to the model
X = sm.add_constant(X)

# Fit the model
model = sm.OLS(Y, X).fit()

# Summary of regression results
print(model.summary())

# ---------------------- RESIDUAL DIAGNOSTIC for SEM Brand

import matplotlib.pyplot as plt

# Get the predicted values and residuals from the model
predicted_values = model.fittedvalues
residuals = model.resid

# Plot Residuals vs. Fitted Values
plt.figure(figsize=(8, 6))
plt.scatter(predicted_values, residuals, edgecolor='k', facecolor='none')
plt.axhline(0, color='red', linestyle='--')
plt.xlabel('Fitted Values')
plt.ylabel('Residuals')
plt.title('Residuals vs. Fitted Values')
plt.show()


# import scipy.stats as stats
import numpy as np

# Q-Q plot of residuals
fig, ax = plt.subplots(figsize=(8, 6))
stats.probplot(residuals, dist="norm", plot=ax)
plt.title('Q-Q Plot of Residuals')
plt.show()


from statsmodels.stats.stattools import durbin_watson

# Perform Durbin-Watson test
dw_value = durbin_watson(residuals)
print(f"Durbin-Watson Statistic: {dw_value}")


from statsmodels.stats.diagnostic import het_breuschpagan

# Perform Breusch-Pagan test
bp_test = het_breuschpagan(residuals, model.model.exog)
bp_pvalue = bp_test[1]

print(f"Breusch-Pagan Test P-value: {bp_pvalue}")


# -------------------------------------- SEO New Properties ---------------------------------------- #
# Independent variables
X = filtered_df[['sem_brand_leads_lag_7']] # , 'sem_brand_impression_share'

# Dependent variable (SEO New Properties)
Y = filtered_df['seo_new_properties']

# Add a constant to the model
X = sm.add_constant(X)

# Fit the model
model = sm.OLS(Y, X).fit()

# Summary of regression results
print(model.summary())


# -------------------------------------- VIF & Multicollinearity ---------------------------------------- #

# from statsmodels.stats.outliers_influence import variance_inflation_factor

# # Independent variables for the SEM Brand model
# X_sem_brand = filtered_df[['sem_brand_leads_lag_6', 'sem_brand_impression_share']]

# # Add constant to X data
# X_sem_brand = sm.add_constant(X_sem_brand)

# # Calculate VIF for each variable
# vif_data_sem_brand = pd.DataFrame()
# vif_data_sem_brand['Variable'] = X_sem_brand.columns
# vif_data_sem_brand['VIF'] = [variance_inflation_factor(X_sem_brand.values, i) for i in range(X_sem_brand.shape[1])]

# # Display VIF for SEM Brand model
# print("VIF for SEM Brand New Properties Model:")
# print(vif_data_sem_brand)


# # Independent variables for the SEO model
# X_seo = filtered_df[['sem_brand_leads_lag_7', 'sem_brand_impression_share']]

# # Add constant to X data
# X_seo = sm.add_constant(X_seo)

# # Calculate VIF for each variable
# vif_data_seo = pd.DataFrame()
# vif_data_seo['Variable'] = X_seo.columns
# vif_data_seo['VIF'] = [variance_inflation_factor(X_seo.values, i) for i in range(X_seo.shape[1])]

# # Display VIF for SEO model
# print("VIF for SEO New Properties Model:")
# print(vif_data_seo)

# -------------------------------------- Residual Diagnostic for SEO ---------------------------------------- #

# import matplotlib.pyplot as plt

# Get the predicted values and residuals from the model
predicted_values = model.fittedvalues
residuals = model.resid

# Plot Residuals vs. Fitted Values
plt.figure(figsize=(8, 6))
plt.scatter(predicted_values, residuals, edgecolor='k', facecolor='none')
plt.axhline(0, color='red', linestyle='--')
plt.xlabel('Fitted Values')
plt.ylabel('Residuals')
plt.title('Residuals vs. Fitted Values')
plt.show()


# import scipy.stats as stats
# import numpy as np

# Q-Q plot of residuals
fig, ax = plt.subplots(figsize=(8, 6))
stats.probplot(residuals, dist="norm", plot=ax)
plt.title('Q-Q Plot of Residuals')
plt.show()


# from statsmodels.stats.stattools import durbin_watson

# Perform Durbin-Watson test
dw_value = durbin_watson(residuals)
print(f"Durbin-Watson Statistic: {dw_value}")



# from statsmodels.stats.diagnostic import het_breuschpagan


# Perform Breusch-Pagan test
bp_test = het_breuschpagan(residuals, model.model.exog)
bp_pvalue = bp_test[1]

print(f"Breusch-Pagan Test P-value: {bp_pvalue}")


# ------------------------ Trend Analysis --------------------------- #

import matplotlib.pyplot as plt

# STEP 1: Visualize the time series
# Plot SEM Brand Leads and New Properties (SEM Brand and SEO)
plt.figure(figsize=(14, 6))

# SEM Brand Leads
plt.subplot(1, 2, 1)
plt.plot(output_df['iso_yr_week'], output_df['sem_brand_new_leads'], label='SEM Brand Leads', color='blue')
plt.plot(output_df['iso_yr_week'], output_df['sem_brand_new_properties'], label='SEM Brand New Properties', color='green')
plt.title('SEM Brand Leads and New Properties Over Time')
plt.xlabel('ISO Week')
plt.ylabel('Count')
plt.xticks(rotation=45)
plt.legend()

# SEO New Properties
plt.subplot(1, 2, 2)
plt.plot(output_df['iso_yr_week'], output_df['seo_new_properties'], label='SEO New Properties', color='purple')
plt.title('SEO New Properties Over Time')
plt.xlabel('ISO Week')
plt.ylabel('Count')
plt.xticks(rotation=45)
plt.legend()

# SEO New Properties
plt.subplot(1, 2, 2)
plt.plot(output_df['iso_yr_week'], output_df['direct_new_properties'], label='Direct New Properties', color='red')
plt.title('Direct New Properties Over Time')
plt.xlabel('ISO Week')
plt.ylabel('Count')
plt.xticks(rotation=45)
plt.legend()

plt.tight_layout()
plt.show()


# STEP 2: Augmented Dickey Fuller (ADF) Test for Stationarity
from statsmodels.tsa.stattools import adfuller

# ADF Test on SEM Brand Leads
adf_test_leads = adfuller(filtered_df['sem_brand_new_leads'])
print(f'SEM Brand Leads ADF Test p-value: {adf_test_leads[1]}')

# ADF Test on SEM Brand New Properties
adf_test_sem_brand_props = adfuller(filtered_df['sem_brand_new_properties'])
print(f'SEM Brand New Properties ADF Test p-value: {adf_test_sem_brand_props[1]}')

# ADF Test on SEO New Properties
adf_test_seo_props = adfuller(filtered_df['seo_new_properties'])
print(f'SEO New Properties ADF Test p-value: {adf_test_seo_props[1]}')

# ADF Test on Direct New Properties
adf_test_direct_props = adfuller(filtered_df['direct_new_properties'])
print(f'Direct New Properties ADF Test p-value: {adf_test_direct_props[1]}')


# STEP 3: Apply Differencing (if needed)
# Make an explicit copy to avoid SettingWithCopyWarning
filtered_df = filtered_df.copy()

# Apply first-order differencing
filtered_df['sem_brand_new_properties_diff'] = filtered_df['sem_brand_new_properties'].diff()
filtered_df['seo_new_properties_diff'] = filtered_df['seo_new_properties'].diff()
# filtered_df['direct_new_properties_diff'] = filtered_df['direct_new_properties'].diff()

# Drop the first row (due to NaN created by differencing)
filtered_df.dropna(inplace=True)


# RE-TEST ADF
# ADF Test on SEM Brand Leads
# ADF Test on differenced SEM Brand New Properties
adf_test_sem_brand_props_diff = adfuller(filtered_df['sem_brand_new_properties_diff'])
print(f'SEM Brand New Properties ADF Test p-value: {adf_test_sem_brand_props_diff[1]}')

# ADF Test on differenced SEO New Properties
adf_test_seo_props_diff = adfuller(filtered_df['seo_new_properties_diff'])
print(f'SEO New Properties ADF Test p-value: {adf_test_seo_props_diff[1]}')

# ADF Test on differenced Direct New Properties
# adf_test_direct_props_diff = adfuller(filtered_df['direct_new_properties_diff'])
# print(f'Direct New Properties ADF Test p-value: {adf_test_direct_props_diff[1]}')


# ------------------- LAG LENGTH SELECTION ----------------------- #

# import pandas as pd
# import statsmodels.api as sm
# from statsmodels.tsa.stattools import adfuller

# Create lagged versions of SEM Brand Leads and Impression Share (up to 12 lags)
# max_lag = 8
# lagged_data = pd.DataFrame()

# # Generate lagged variables for SEM Brand Leads and Impression Share
# for lag in range(1, max_lag + 1):
#     lagged_data[f'sem_brand_leads_lag_{lag}'] = filtered_df['sem_brand_new_leads'].shift(lag)
#     lagged_data[f'sem_brand_impression_share_lag_{lag}'] = filtered_df['sem_brand_impression_share'].shift(lag)

# # Remove missing values due to lagging
# lagged_data.dropna(inplace=True)

# # Add the dependent variable (SEM Brand New Properties or SEO New Properties)
# lagged_data['sem_brand_new_properties'] = filtered_df['sem_brand_new_properties'].iloc[max_lag:].values
# lagged_data['seo_new_properties'] = filtered_df['seo_new_properties'].iloc[max_lag:].values

# # Define the independent variables (including lagged versions of SEM Brand Leads and Impression Share)
# X = lagged_data.drop(['sem_brand_new_properties', 'seo_new_properties'], axis=1)

# # Add a constant (intercept) to the model
# X = sm.add_constant(X)

# # Define the dependent variable (e.g., SEM Brand New Properties or SEO New Properties)
# Y_sem_brand = lagged_data['sem_brand_new_properties']
# Y_seo = lagged_data['seo_new_properties']

# # Fit the model and select the lag length based on AIC/BIC
# model_sem_brand = sm.OLS(Y_sem_brand, X).fit()
# model_seo = sm.OLS(Y_seo, X).fit()

# # Display AIC/BIC for the models
# print(f"SEM Brand New Properties AIC: {model_sem_brand.aic}")
# print(f"SEM Brand New Properties BIC: {model_sem_brand.bic}")

# print(f"SEO New Properties AIC: {model_seo.aic}")
# print(f"SEO New Properties BIC: {model_seo.bic}")

# # Display the summary of the SEM Brand model
# print(model_sem_brand.summary())

# # Display the summary of the SEO model
# print(model_seo.summary())


# # -------- Testing Step-wise Model ---------------- #

# from mlxtend.feature_selection import SequentialFeatureSelector as SFS
# from sklearn.linear_model import LinearRegression
# # import statsmodels.api as sm

# # Define a basic linear regression model
# lr = LinearRegression()

# # Perform stepwise selection based on AIC
# sfs = SFS(lr, 
#           k_features='best', 
#           forward=True, 
#           floating=False, 
#           scoring='neg_mean_squared_error', 
#           cv=0)

# # Fit the stepwise selector on the lagged data
# sfs = sfs.fit(X, Y_sem_brand)

# # Get the selected variables
# print("Selected variables for SEM Brand New Properties:")
# print(sfs.k_feature_names_)

# # Repeat for SEO New Properties
# sfs = sfs.fit(X, Y_seo)
# print("Selected variables for SEO New Properties:")
# print(sfs.k_feature_names_)


# --------------------- FINAL MODEL ----------------------------- #

# import statsmodels.api as sm
# import pandas as pd

# Step 1: Apply Differencing for Trend Adjustment
# Assuming `output_df` is your main DataFrame containing the raw data
# Create differenced variables for SEM Brand and SEO New Properties
filtered_df['d_sem_brand_new_properties'] = filtered_df['sem_brand_new_properties'].diff()
filtered_df['d_seo_new_properties'] = filtered_df['seo_new_properties'].diff()
filtered_df['d_direct_new_properties'] = filtered_df['direct_new_properties'].diff()

# Drop missing values from differencing
differenced_df = filtered_df.dropna()

# Step 2: Create the Interaction Term
differenced_df['seo_direct_interaction'] = differenced_df['d_seo_new_properties'] * differenced_df['d_direct_new_properties']

# Step 3: Define Independent Variables (Lagged Leads, Impression Share, Interaction Term)
X = differenced_df[['d_seo_new_properties', 'd_direct_new_properties', 'seo_direct_interaction']]

# Add a constant (intercept) to the model
X = sm.add_constant(X)

# Step 4: Define Dependent Variable (Differenced SEM Brand New Properties)
Y = differenced_df['d_sem_brand_new_properties']

# Step 5: Fit the Multiple Regression Model
model = sm.OLS(Y, X).fit()

# Step 6: View the Regression Summary (Includes p-values for Hypothesis Testing)
print(model.summary())

# Step 7: Hypothesis Testing
# You can manually check the p-values from the summary or create your own tests based on the coefficients



# -------------------------------------- SEM Brand New Properties ---------------------------------------- #
# Independent variables, including d_direct_new_properties from the second model
X_sem = differenced_df[['sem_brand_leads_lag_6', 'sem_brand_impression_share']] # , 'd_direct_new_properties'

# Dependent variable (SEM Brand New Properties)
Y_sem = differenced_df['sem_brand_new_properties']

# Add a constant to the model
X_sem = sm.add_constant(X_sem)

# Fit the model
model_sem = sm.OLS(Y_sem, X_sem).fit()

# Summary of regression results
print(model_sem.summary())


# ------------------ Test for SEO New Properties

# Independent variables
X_seo = differenced_df[['sem_brand_leads_lag_7', 'd_direct_new_properties']] # , 'sem_brand_impression_share'

# Dependent variable (SEO New Properties)
Y_seo = differenced_df['seo_new_properties']

# Add a constant to the model
X_seo = sm.add_constant(X_seo)

# Fit the model
model_seo = sm.OLS(Y_seo, X_seo).fit()

# Summary of regression results
print(model_seo.summary())



# ---------------- Test ACF & PACF -------------------- # 
# import pandas as pd
# import statsmodels.api as sm
# from statsmodels.graphics.tsaplots import plot_acf, plot_pacf
# import matplotlib.pyplot as plt

# # Step 1: Difference the data (if needed)
# differenced_data = filtered_df['seo_new_properties'].diff().dropna()

# # Step 2: Plot ACF and PACF
# fig, axes = plt.subplots(1, 2, figsize=(16, 6))
# plot_acf(differenced_data, ax=axes[0])  # ACF plot (for q)
# plot_pacf(differenced_data, ax=axes[1])  # PACF plot (for p)
# plt.show()

# Step 3: Interpretation



# import matplotlib.pyplot as plt
import seaborn as sns

# -------------- Final Visualization for SEM Brand New Properties --------------- #

# Predictions for SEM Brand New Properties
y_pred_sem = model_sem.predict(X_sem)

# Prediction vs Actual for SEM Brand New Properties
plt.figure(figsize=(10, 6))
plt.scatter(differenced_df['sem_brand_new_properties'], y_pred_sem, color='blue', label='Predicted', alpha=0.6)
plt.plot(differenced_df['sem_brand_new_properties'], differenced_df['sem_brand_new_properties'], color='black', label='Actual', linestyle='--')
plt.title('SEM Brand New Properties: Predicted vs Actual')
plt.xlabel('Actual SEM Brand New Properties')
plt.ylabel('Predicted SEM Brand New Properties')
plt.legend()
plt.show()

# Residuals Plot for SEM Brand New Properties
residuals_sem = differenced_df['sem_brand_new_properties'] - y_pred_sem
plt.figure(figsize=(10, 6))
sns.histplot(residuals_sem, kde=True, color='purple')
plt.title('SEM Brand New Properties: Residuals Distribution')
plt.xlabel('Residuals')
plt.ylabel('Frequency')
plt.show()

# Residuals Scatter Plot for SEM Brand New Properties
plt.figure(figsize=(10, 6))
plt.scatter(y_pred_sem, residuals_sem, color='blue', alpha=0.6)
plt.axhline(y=0, color='black', linestyle='--')
plt.title('SEM Brand New Properties: Residuals vs Predicted')
plt.xlabel('Predicted SEM Brand New Properties')
plt.ylabel('Residuals')
plt.show()


# -------------- Final Visualization for SEO New Properties --------------- #

# Predictions for SEO New Properties
y_pred_seo = model_seo.predict(X_seo)

# Prediction vs Actual for SEO New Properties
plt.figure(figsize=(10, 6))
plt.scatter(differenced_df['seo_new_properties'], y_pred_seo, color='green', label='Predicted', alpha=0.6)
plt.plot(differenced_df['seo_new_properties'], differenced_df['seo_new_properties'], color='black', label='Actual', linestyle='--')
plt.title('SEO New Properties: Predicted vs Actual')
plt.xlabel('Actual SEO New Properties')
plt.ylabel('Predicted SEO New Properties')
plt.legend()
plt.show()

# Residuals Plot for SEO New Properties
residuals_seo = differenced_df['seo_new_properties'] - y_pred_seo
plt.figure(figsize=(10, 6))
sns.histplot(residuals_seo, kde=True, color='green')
plt.title('SEO New Properties: Residuals Distribution')
plt.xlabel('Residuals')
plt.ylabel('Frequency')
plt.show()

# Residuals Scatter Plot for SEO New Properties
plt.figure(figsize=(10, 6))
plt.scatter(y_pred_seo, residuals_seo, color='green', alpha=0.6)
plt.axhline(y=0, color='black', linestyle='--')
plt.title('SEO New Properties: Residuals vs Predicted')
plt.xlabel('Predicted SEO New Properties')
plt.ylabel('Residuals')
plt.show()




# --------------------------------------------------------------------------------------- #
# ------------------- Export to Excel for Final Visualizations -------------------------- #
# --------------------------------------------------------------------------------------- #

# Copy the original df
excel_df = output_df.copy()

# Select relevant columns for the output
excel_df = excel_df[['iso_yr_week', 'sem_brand_new_leads', 'seo_new_leads', 'direct_new_leads',
                'sem_brand_impression_share', 'sem_brand_new_properties', 'seo_new_properties',
                'direct_new_properties', 'sem_brand_leads_lag_3', 'sem_brand_leads_lag_4', 
                'sem_brand_leads_lag_5', 'sem_brand_leads_lag_6', 'sem_brand_leads_lag_7', 'sem_brand_leads_lag_8']]

# Aggregate to the Year and week
excel_df.groupby('iso_yr_week').sum().reset_index()

# import pandas as pd
# import numpy as np

# Create an Excel writer
output_file = 'analysis/output/sem-brand-seo-direct-data-visualization_data.xlsx'
writer = pd.ExcelWriter(output_file, engine='xlsxwriter')

excel_df.to_excel(writer, sheet_name='excel_df', index=False)

# Relationship Visualization: Line Charts with Highlighted Lag Effects
# We need SEM Brand Leads, SEM Brand New Properties (lagged 6 weeks), and SEO New Properties (lagged 7 weeks)

# Assuming `df` contains the original data
relationship_df = pd.DataFrame({
    'Week': excel_df['iso_yr_week'],  # Time axis
    'SEM Brand Leads': excel_df['sem_brand_new_leads'],
    'SEM Brand New Properties (Lag 6 Weeks)': excel_df['sem_brand_new_properties'].shift(-6),
    'SEO New Properties (Lag 7 Weeks)': excel_df['seo_new_properties'].shift(-7)
})

# Drop missing values caused by the lag
relationship_df.dropna(inplace=True)
relationship_df.to_excel(writer, sheet_name='Relationship_Visualization', index=False)

# Contribution Analysis: Waterfall chart to show the contribution of each channel
# Ensure only the correct numeric columns are being summed
contribution_df = pd.DataFrame({
    'Week': excel_df['iso_yr_week'],
    'SEM Brand Contribution': pd.to_numeric(excel_df['sem_brand_new_properties'], errors='coerce'),
    'SEO Contribution': pd.to_numeric(excel_df['seo_new_properties'], errors='coerce'),
    'Direct Contribution': pd.to_numeric(excel_df['direct_new_properties'], errors='coerce')
})

# Calculate the total new properties
contribution_df['Total New Properties'] = contribution_df[['SEM Brand Contribution', 'SEO Contribution', 'Direct Contribution']].sum(axis=1)

# Drop any rows with missing or non-numeric data
contribution_df.dropna(inplace=True)

# Write to Excel
contribution_df.to_excel(writer, sheet_name='Contribution_Analysis', index=False)

# Trend Analysis: Trend line chart showing SEM Brand Leads, SEO New Properties, and Direct New Properties over time
# No lag needed, just the raw data for these metrics over time

trend_df = pd.DataFrame({
    'Week': excel_df['iso_yr_week'],
    'SEM Brand Leads': excel_df['sem_brand_new_leads'],
    'SEO New Properties': excel_df['seo_new_properties'],
    'Direct New Properties': excel_df['direct_new_properties']
})

trend_df.to_excel(writer, sheet_name='Trend_Analysis', index=False)

# Channel Synergies Visualization: Heatmap to show the strength of correlations between channels
# Calculate correlations for SEM Brand Leads, SEO New Properties, and Direct New Properties

heatmap_data = excel_df[['sem_brand_impression_share', 'sem_brand_new_leads', 'sem_brand_new_properties', 'seo_new_properties', 'direct_new_properties', 
                         'sem_brand_leads_lag_3', 'sem_brand_leads_lag_4', 'sem_brand_leads_lag_5', 'sem_brand_leads_lag_6', 'sem_brand_leads_lag_7',
                         'sem_brand_leads_lag_8']].corr()

# Convert correlation matrix to a DataFrame to export
heatmap_df = pd.DataFrame(heatmap_data)
heatmap_df.to_excel(writer, sheet_name='Channel_Synergies_Heatmap')

# Save the Excel file
writer._save()
print(f"Data exported successfully to {output_file}")


