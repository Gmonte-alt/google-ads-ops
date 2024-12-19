# file name: auction-insights/preprocess_auction-insights-csv.py
# version: V000-000-001

import os
import pandas as pd
import glob

# Define the path to the folder containing the CSV files
folder_path = 'auction-insights/data'  # Change this to your folder path

# Define the output CSV file
output_file = 'auction-insights/output/output_auction-insights-report.csv'  # Change this to your output file path

# Initialize a list to hold dataframes
dataframes = []

# Get a list of all CSV files in the folder
csv_files = glob.glob(os.path.join(folder_path, '*.csv'))

# Process each CSV file
for file in csv_files:
    try:
        # Extract the brand name and category from the file name
        file_name = os.path.basename(file)
        parts = file_name.split('_')
        brand_name = parts[0]
        category = parts[1] if len(parts) > 1 else 'Unknown'
        
        if brand_name == 'FF':
            brand_name = 'Furnished Finder'
        elif brand_name == 'TNH':
            brand_name = 'Travel Nurse Housing'
        
        # Read the CSV file
        df = pd.read_csv(file)
        
        # Add the brand name and category as the first two columns
        df.insert(0, 'Category', category)
        df.insert(0, 'Brand Name', brand_name)
        
        # Convert non-integer values in the specified columns
        for col in df.columns[2:]:  # Skipping the first two columns
            df[col] = df[col].replace({'<10%': '9.99%', '--': '0'})
            
            # Function to check if a value can be converted to float
            def convert_to_float(value):
                try:
                    return float(value.rstrip('%'))
                except ValueError:
                    return 0.0 if value == '0' else value  # Convert '0' to 0.0, otherwise return the original value
            
            df[col] = df[col].apply(convert_to_float)
        
        # Append the dataframe to the list
        dataframes.append(df)
    except Exception as e:
        print(f"Error processing file {file_name}: {e}")

# Concatenate all dataframes
if dataframes:
    final_df = pd.concat(dataframes, ignore_index=True)

    # Write the final dataframe to a CSV file
    final_df.to_csv(output_file, index=False)

    print(f'Report generated: {output_file}')
else:
    print('No valid dataframes to concatenate.')
