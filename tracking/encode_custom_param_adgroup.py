# file name: tracking/encode_custom_param_adgroup.py
# version: V000-000-002
# Note: This version preproceses the raw export from Google Ads and properly filters and removes columns

import pandas as pd
import urllib.parse

# Define the input and output file paths
input_file = "tracking/input-data/Ad group performance - Ad Group & Campaign Inventory.csv"
output_file_adgroups = "tracking/output/encoded_custom_params_output_adgroups.csv"
output_file_campaigns = "tracking/output/encoded_custom_params_output_campaigns.csv"

# Read the input CSV file, skip the first two rows, and treat the third row as the header
df = pd.read_csv(input_file, skiprows=2)

# Extract the specified columns
columns_to_keep = [
    "Account name", "Customer ID", "Ad group ID", "Ad group", 
    "Campaign ID", "Campaign", "Campaign state", "Ad group state"#, 
    #"Campaign type", "Campaign subtype"
]
df = df[columns_to_keep]

# Filter rows where "Campaign state" is "Enabled"
df = df[df["Campaign state"] == "Enabled"]

# Function to encode and replace %20 with +
def custom_url_encode(value):
    # Encode value using urllib.parse.quote
    encoded_value = urllib.parse.quote(str(value), safe='')
    # Replace %20 with +
    return encoded_value.replace('%20', '+')

# Create the first output file for ad groups
df_adgroups = df.copy()
df_adgroups["Custom Parameter Key name"] = "_adgroup"
df_adgroups["Custom Parameter Value Encoded"] = df_adgroups["Ad group"].apply(custom_url_encode)

# Save the ad groups DataFrame to a new CSV file
df_adgroups.to_csv(output_file_adgroups, index=False)

# Create the second output file for campaigns
df_campaigns = df.drop(columns=["Ad group ID", "Ad group", "Ad group state"])
df_campaigns = df_campaigns.drop_duplicates(subset=["Customer ID", "Campaign ID"])
df_campaigns["Custom Parameter Key name"] = "_campaign"
df_campaigns["Custom Parameter Value Encoded"] = df_campaigns["Campaign"].apply(custom_url_encode)

# Save the campaigns DataFrame to a new CSV file
df_campaigns.to_csv(output_file_campaigns, index=False)

print(f"Encoded parameters have been saved to {output_file_adgroups} and {output_file_campaigns}")
