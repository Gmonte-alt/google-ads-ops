# file name: stats_models/adf-test_purchas-clicks-cvr.py
# version: V000-000-000
# Notes:
#

import pandas as pd
from statsmodels.tsa.stattools import adfuller

# Load your data into a pandas DataFrame
# For example, if you have a CSV file:
# data = pd.read_csv('your_coversion_data.csv')

# Assume 'conversion_rates' is a pandas Series containing your conversion rate data
conversion_rates = pd.Series([0.0010,0.0016,0.0000,0.0000,0.0040,0.0038,0.0039,0.0047,0.0035,0.0018,0.0019,0.0016,0.0020,0.0034,0.0036,0.0043,0.0047,0.0065,0.0056,0.0056,0.0052,0.0037,0.0054,0.0031,0.0023,0.0032,0.0035,0.0037,0.0021,0.0054,0.0022,0.0027,0.0028,0.0023,0.0061,0.0047,0.0055,0.0034,0.0017,0.0046,0.0019,0.0064,0.0035,0.0052,0.0033,0.0032,0.0045,0.0042,0.0046,0.0071,0.0051,0.0042,0.0040,0.0042,0.0045,0.0045,0.0057,0.0058,0.0047,0.0067,0.0066,0.0053,0.0070,0.0060,0.0071,0.0078,0.0071,0.0092,0.0027,0.0094,0.0077,0.0108,0.0076,0.0049,0.0069,0.0071,0.0074,0.0084,0.0093,0.0070,0.0078,0.0082,0.0087,0.0087,0.0091,0.0094,0.0038,0.0044,0.0080,0.0088])

# Perform the ADF test
result = adfuller(conversion_rates)

# Extract the test statistic and p-value
adf_statistic = result[0]
p_value = result[1]
critical_values = result[4]

# Print the results
print(f'ADF Statistics: {adf_statistic}')
print(f'p-value: {p_value}')
print('Critical Values:')
for key, value in critical_values.items():
    print(f' {key}: {value}')