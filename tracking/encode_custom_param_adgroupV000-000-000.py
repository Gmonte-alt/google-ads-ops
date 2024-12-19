# file name: tracking/encode_custom_param_adgroup.py
# version: V000-000-000

import pandas as pd
import urllib.parse

# Define the input and output file paths
input_file = "tracking/input-data/custom_param_preprocess_Ad-Group_Campaign-Inventory_cleaned_20240607.csv"
output_file = "tracking/output/encoded_custom_params_output.csv"

# Read the CSV file
df = pd.read_csv(input_file)

# Encode the values in the "Custom Parameter Value Unencoded" column
df['Custom Parameter Value Encoded'] = df['Custom Parameter Value Unencoded'].apply(lambda x: urllib.parse.quote(str(x)))

# Save the modified DataFrame to a new CSV file
df.to_csv(output_file, index=False)

print(f"Encoded parameters have been saved to {output_file}")
