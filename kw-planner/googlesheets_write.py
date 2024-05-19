# referencing this resource: https://erikrood.com/Posts/py_gsheets.html

import pygsheets
import pandas as pd
#authorization
gc = pygsheets.authorize(service_file='reporting/tensile-talent-420617-c84adaa50e8e.json') # /Users/erikrood/desktop/QS_Model/creds.json

# Create empty dataframe
df = pd.DataFrame()

# Create a column
df['name'] = ['John', 'Steve', 'Sarah']

#open the google spreadsheet (where 'PY to Gsheet Test' is the name of my sheet)
sh = gc.open('Google Ads Hourly Pacer')

#select the first sheet 
wks = sh[0]

#update the first sheet with df, starting at cell B2. 
wks.set_dataframe(df,(1,1))