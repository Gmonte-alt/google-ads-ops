# file name:
# version: V000-000-000

from datetime import datetime, timedelta

# Step 2: Define the date ranges for EOW, WTD, MTD, and QTD
def get_date_ranges(reference_date):
    # Calculate dates
    eow_end_date = reference_date - timedelta(days=reference_date.weekday() + 1)
    eow_start_date = eow_end_date - timedelta(days=6)
    
    wtd_start_date = reference_date.replace(hour=0, minute=0, second=0, microsecond=0)
    wtd_start_date = wtd_start_date - timedelta(days=reference_date.weekday())

    mtd_start_date = reference_date.replace(day=1)
    
    qtd_start_date = (reference_date - timedelta(days=reference_date.day-1)).replace(month=((reference_date.month-1)//3)*3+1, day=1)
    
    return {
    'EOW': (eow_start_date, eow_end_date),
    'WTD': (wtd_start_date, reference_date),
    'MTD': (mtd_start_date, reference_date),
    'QTD': (qtd_start_date, reference_date)
    }

# Get current reference date (for example, last day of ISO week 31)
# reference_date = datetime(2024, 8, 11)  # This can be dynamically set
reference_date = datetime.now() - timedelta(days=2)

date_ranges = get_date_ranges(reference_date)

# Step 3: Function to get previous period dates
def get_previous_period_dates(period_name, start_date, end_date):
    if period_name in ['WTD', 'EOW']:
        # Compare to the same days in the previous week for WoW
        previous_start_date = start_date - timedelta(weeks=1)
        previous_end_date = end_date - timedelta(weeks=1)

        # Calculate YoY based on the same ISO week in the previous year
        previous_yoy_start_date = start_date - timedelta(weeks=52)
        previous_yoy_end_date = end_date - timedelta(weeks=52)
        
        # Adjust if using ISO week logic instead of a fixed 52-week shift:
        start_iso_year, start_iso_week, _ = start_date.isocalendar()
        end_iso_year, end_iso_week, _ = end_date.isocalendar()
        
        previous_yoy_start_date = start_date.replace(year=start_iso_year - 1)
        previous_yoy_end_date = end_date.replace(year=end_iso_year - 1)
        
        previous_yoy_start_date = previous_yoy_start_date - timedelta(days=previous_yoy_start_date.weekday())
        previous_yoy_end_date = previous_yoy_start_date + timedelta(days=6)
        
    elif period_name == 'MTD':
        # Compare to the same days in the previous month
        previous_start_date = start_date.replace(day=1) - timedelta(days=1)
        previous_start_date = previous_start_date.replace(day=start_date.day)
        previous_end_date = end_date.replace(day=1) - timedelta(days=1)
        previous_end_date = previous_end_date.replace(day=end_date.day)
        
        previous_yoy_start_date = start_date.replace(year=start_date.year - 1)
        previous_yoy_end_date = end_date.replace(year=end_date.year - 1)
        
    elif period_name == 'QTD':
        # Determine the current quarter
        current_quarter = (start_date.month - 1) // 3 + 1
        
        # Calculate the start and end dates of the previous quarter
        if current_quarter == 1:
            previous_start_date = datetime(start_date.year - 1, 10, 1)
            previous_end_date = datetime(start_date.year - 1, 12, 31)
        elif current_quarter == 2:
            previous_start_date = datetime(start_date.year, 1, 1)
            previous_end_date = datetime(start_date.year, 3, 31)
        elif current_quarter == 3:
            previous_start_date = datetime(start_date.year, 4, 1)
            previous_end_date = datetime(start_date.year, 6, 30)
        elif current_quarter == 4:
            previous_start_date = datetime(start_date.year, 7, 1)
            previous_end_date = datetime(start_date.year, 9, 30)
        
        # Adjust the previous end date to match the same relative position within the quarter
        previous_end_date = previous_start_date + timedelta(days=(end_date - start_date).days)
        
        # Previous YoY start and end dates
        previous_yoy_start_date = previous_start_date.replace(year=start_date.year - 1)
        previous_yoy_end_date = previous_end_date.replace(year=end_date.year - 1)
        
    else:
        previous_start_date, previous_end_date = None, None
        previous_yoy_start_date, previous_yoy_end_date = None, None
        
    return previous_start_date, previous_end_date, previous_yoy_start_date, previous_yoy_end_date

# Step 4: Expand rows by adding a 'Period' column and printing the data
summary_dfs = []
for period_name, (start_date, end_date) in date_ranges.items():
    previous_start_date, previous_end_date, previous_yoy_start_date, previous_yoy_end_date = get_previous_period_dates(period_name, start_date, end_date)
    
    print(f"{period_name} Period:")
    print(f"  Current Start Date: {start_date}")
    print(f"  Current End Date: {end_date}")
    print(f"  Previous Start Date: {previous_start_date}")
    print(f"  Previous End Date: {previous_end_date}")
    print(f"  Previous YoY Start Date: {previous_yoy_start_date}")
    print(f"  Previous YoY End Date: {previous_yoy_end_date}")
    print()
