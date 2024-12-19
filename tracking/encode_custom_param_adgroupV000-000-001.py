# file name: tracking/encode_custom_param_adgroup.py
# version: V000-000-001
# Note: this version is the updated script ensuring that other special characters are percent-encoded and spaces are encoded as +:
# Bug: the ad group id is not properly formatted due to my preprocessing work in Excel. Next version will resolve this.

import pandas as pd
import urllib.parse

# Define the input and output file paths
input_file = "tracking/input-data/custom_param_preprocess_Ad-Group_Campaign-Inventory_cleaned_20240607.csv"
output_file = "tracking/output/encoded_custom_params_output.csv"

# Read the CSV file
df = pd.read_csv(input_file)

# Function to encode and replace %20 with +
def custom_url_encode(value):
    # Encode value using urllib.parse.quote
    encoded_value = urllib.parse.quote(str(value), safe='')
    # Replace %20 with +
    return encoded_value.replace('%20', '+')

# Encode the values in the "Custom Parameter Value Unencoded" column
df['Custom Parameter Value Encoded'] = df['Custom Parameter Value Unencoded'].apply(custom_url_encode)

# Save the modified DataFrame to a new CSV file
df.to_csv(output_file, index=False)

print(f"Encoded parameters have been saved to {output_file}")
