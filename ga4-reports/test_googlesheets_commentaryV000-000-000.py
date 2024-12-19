# File name: ga4-reports/test_googlesheets_commentary.py
# version: V000-000-000
# output:
# Notes: Testing the set-up for the growth marketing cross channel reporting

import gspread
from google.oauth2.service_account import Credentials

# Path to your service account JSON key file
SERVICE_ACCOUNT_FILE = 'authentication/green-carrier-423923-j0-ebd01cd30b56.json' #'path_to_your_service_account_file.json'

# Define the scopes required for the Google Sheets API
SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']

# Create a credentials object using the service account file
creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)

# Authenticate and initialize the gspread client
client = gspread.authorize(creds)

# Open the Google Sheet by its name or URL
sheet = client.open('FF - Growth - Weekly Commentary').sheet1

# Example: Read the values from the first row
row_values = sheet.row_values(1)
print(row_values)

# # Example: Update a cell (e.g., A2) with a new value
# sheet.update('A2', 'New Value')

# # Example: Add a new row
# new_row = ["Value1", "Value2", "Value3"]
# sheet.append_row(new_row)
