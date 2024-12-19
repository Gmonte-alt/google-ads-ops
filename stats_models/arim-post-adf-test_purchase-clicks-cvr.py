# file name: stats_models/arim-post-adf-test_purchase-clicks-cvr.py 
# version: V000-000-000
# Notes:
#

import pandas as pd
from statsmodels.tsa.stattools import adfuller
from statsmodels.tsa.arima.model import ARIMA
from statsmodels.graphics.tsaplots import plot_acf, plot_pacf
import numpy as np
import matplotlib.pyplot as plt

# Load your data into a pandas DataFrame
# For example, if you have a CSV file:
# data = pd.read_csv('your_coversion_data.csv')

# Assume 'conversion_rates' is a pandas Series containing your conversion rate data
conversion_rates = pd.Series([0.0016,0.0040,0.0038,0.0039,0.0047,0.0035,0.0018,0.0019,0.0016,0.0020,0.0034,0.0036,0.0043,0.0047,0.0065,0.0056,0.0056,0.0052,0.0037,0.0054,0.0031,0.0023,0.0032,0.0035,0.0037,0.0021,0.0054,0.0022,0.0027,0.0028,0.0023,0.0061,0.0047,0.0055,0.0034,0.0017,0.0046,0.0019,0.0064,0.0035,0.0052,0.0033,0.0032,0.0045,0.0042,0.0046,0.0071,0.0051,0.0042,0.0040,0.0042,0.0045,0.0045,0.0057,0.0058,0.0047,0.0067,0.0066,0.0053,0.0070,0.0060,0.0071,0.0078,0.0071,0.0092,0.0027,0.0094,0.0077,0.0108,0.0076,0.0049,0.0069,0.0071,0.0074,0.0084,0.0093,0.0070,0.0078,0.0082,0.0087,0.0087,0.0091,0.0094,0.0038,0.0044,0.0080,0.0088])

# Check for zeros or negative values
print("Contains zeros or negative values:", (conversion_rates <= 0).any())

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


#--------------------------------FIRST-ORDER DIFFERENCING--------------------------------#
differenced_series = conversion_rates.diff().dropna()

# Perform ADF test on differenced series
result_diff = adfuller(differenced_series)
# Print the results
print(f'ADF Statistics (Difference): {result_diff[0]}')
print(f'p-value (Difference): {result_diff[1]}')
print('Critical Values (Difference):')
for key, value in result_diff[4].items():
    print(f' {key}: {value}')



#--------------------------------LOG TRANSFORMATION & DIFFERENCING--------------------------------#

# Adding a small constant to avoid log of zero
small_constant = 1e-6
adjusted_series = conversion_rates + small_constant

# Log transformation and differencing
log_diff_series = np.log(adjusted_series).diff().dropna()

# Perform ADF test on log-differenced series
result_log_diff = adfuller(log_diff_series)
# Print the results
print(f'ADF Statistics (Log-Differenced): {result_log_diff[0]}')
print(f'p-value (Log-Differenced): {result_log_diff[1]}')
print('Critical Values (Difference):')
for key, value in result_log_diff[4].items():
    print(f' {key}: {value}')



#--------------------------------FIT AN ARIMA MODEL--------------------------------#

# fit an ARIM model (p,d,q) - here (1, 0, 1) as an example
model = ARIMA(log_diff_series, order=(1,0,1))
model_fit = model.fit()

# Summary of the model
print(model_fit.summary())

# Plot the residuals
residuals = model_fit.resid
plt.figure(figsize=(10,6))
plt.plot(residuals)
plt.title('Residuals of ARIMA Model')
plt.show()

# ACF and PACF plots to check resudals for white noise
from statsmodels.graphics.tsaplots import plot_acf, plot_pacf

plot_acf(residuals)
plt.show()
plot_pacf(residuals)
plt.show()



#--------------------------------log_diff_series visualization--------------------------------#

# Plot the log-differenced series
plt.figure(figsize=(12, 6))
plt.plot(log_diff_series, marker='o', linestyle='-')
plt.title('Log-Differenced Series')
plt.xlabel('Time')
plt.ylabel('Log Difference')
plt.grid(True)
plt.show()

# Plot ACF and PACF
plt.figure(figsize=(12, 6))
plt.subplot(121)
plot_acf(log_diff_series, lags=20, ax=plt.gca())
plt.title('PACF of Log-Differenced Series')

plt.tight_layout()
plt.show()