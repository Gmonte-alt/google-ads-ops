from datetime import date, datetime, timedelta

# Step 2: Define the date ranges for EOW, WTD, MTD, and QTD
def get_date_ranges(reference_date):
    # Calculate dates for existing views
    eow_end_date = reference_date - timedelta(days=reference_date.weekday() + 1)
    eow_start_date = eow_end_date - timedelta(days=6)
    
    wtd_start_date = reference_date.replace(hour=0, minute=0, second=0, microsecond=0)
    wtd_start_date = wtd_start_date - timedelta(days=reference_date.weekday())

    mtd_start_date = reference_date.replace(day=1)
    
    qtd_start_date = (reference_date - timedelta(days=reference_date.day-1)).replace(month=((reference_date.month-1)//3)*3+1, day=1)

    # Calculate dates for new views
    eom_end_date = reference_date.replace(day=1) - timedelta(days=1)  # Last day of the previous month
    eom_start_date = eom_end_date.replace(day=1)  # First day of the previous month

    current_quarter = (reference_date.month - 1) // 3 + 1
    # Adjust to last completed quarter
     # Adjust to last completed quarter
    if current_quarter == 1:
        # If in Q1, last completed quarter is Q4 of the previous year
        eoq_end_date = datetime(reference_date.year - 1, 12, 31)
        eoq_start_date = datetime(reference_date.year - 1, 10, 1)
    else:
        # For other quarters, calculate the last completed quarter's end and start dates
        previous_quarter_end_month = (current_quarter - 1) * 3  # Get the last month of the previous quarter
        eoq_end_date = datetime(reference_date.year, previous_quarter_end_month, 1) + timedelta(days=32)
        eoq_end_date = eoq_end_date.replace(day=1) - timedelta(days=1)  # Last day of the previous quarter
        eoq_start_date = datetime(reference_date.year, previous_quarter_end_month - 2, 1)

    ytd_start_date = reference_date.replace(month=1, day=1)

    return {
        'EOW': (eow_start_date, eow_end_date),
        'WTD': (wtd_start_date, reference_date),
        'MTD': (mtd_start_date, reference_date),
        'QTD': (qtd_start_date, reference_date),
        'EOM': (eom_start_date, eom_end_date),
        'EOQ': (eoq_start_date, eoq_end_date),
        'YTD': (ytd_start_date, reference_date)
    }


# Get current reference date (for example, last day of ISO week 31)
# reference_date = datetime(2024, 8, 11)  # This can be dynamically set
# Automate the calculation of the reference_date as the day before yesterday
reference_date = datetime.now() - timedelta(days=2)
reference_date = reference_date.replace(hour=0, minute=0, second=0, microsecond=0)

date_ranges = get_date_ranges(reference_date)

def get_previous_period_dates(period_name, start_date, end_date, available_dates=None):
    # Optional: Provide a list of available dates in your DataFrame
    if available_dates is None:
        available_dates = []
    
    # Set default values for the variables
    previous_start_date, previous_end_date = None, None
    previous_yoy_start_date, previous_yoy_end_date = None, None

    if period_name in ['EOW']:
        # Compare to the same days in the previous week for WoW
        previous_start_date = start_date - timedelta(weeks=1)
        previous_end_date = end_date - timedelta(weeks=1)

        # Calculate YoY based on the same ISO week in the previous year
        start_iso_year, start_iso_week, _ = start_date.isocalendar()
        end_iso_year, end_iso_week, _ = end_date.isocalendar()

        # Find the first day (Monday) of the same ISO week last year
        previous_yoy_start_date = datetime.fromisocalendar(start_iso_year - 1, start_iso_week, 1)
        # Find the last day (Sunday) of the same ISO week last year
        previous_yoy_end_date = previous_yoy_start_date + timedelta(days=6)
        
    if period_name in ['WTD']:
        # Compare to the same days in the previous week for WoW
        previous_start_date = start_date - timedelta(weeks=1)
        previous_end_date = end_date - timedelta(weeks=1)

        # Calculate YoY based on the same ISO week and day of week in the previous year
        start_iso_year, start_iso_week, _ = start_date.isocalendar()
        end_day_of_week = end_date.weekday()  # Get the current day of the week (e.g., Monday=0, Sunday=6)

        # Find the first day (Monday) of the same ISO week last year
        previous_yoy_start_date = datetime.fromisocalendar(start_iso_year - 1, start_iso_week, 1)
    
        # Adjust the previous_yoy_end_date to align with the same day of the week
        previous_yoy_end_date = previous_yoy_start_date + timedelta(days=end_day_of_week)
        
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

        # Calculate the start and end dates of the current quarter (for the current quarter calculation)
        if current_quarter == 1:
            start_date = datetime(start_date.year, 1, 1)
            end_date = datetime(start_date.year, 3, 31)
        elif current_quarter == 2:
            start_date = datetime(start_date.year, 4, 1)
            end_date = datetime(start_date.year, 6, 30)
        elif current_quarter == 3:
            start_date = datetime(start_date.year, 7, 1)
            end_date = datetime(start_date.year, 9, 30)
        elif current_quarter == 4:
            start_date = datetime(start_date.year, 10, 1)
            end_date = datetime(start_date.year, 12, 31)

        # Adjust the end_date to match the current progress in the quarter (QTD)
        end_date = start_date + timedelta(days=(reference_date - start_date).days)

        # Previous Quarter Dates (LW equivalent)
        if current_quarter == 1:
            # Previous quarter is Q4 of last year
            previous_start_date = datetime(start_date.year - 1, 10, 1)
            previous_end_date = datetime(start_date.year - 1, 12, 31)
        elif current_quarter == 2:
            # Previous quarter is Q1 of current year
            previous_start_date = datetime(start_date.year, 1, 1)
            previous_end_date = datetime(start_date.year, 3, 31)
        elif current_quarter == 3:
            # Previous quarter is Q2 of current year
            previous_start_date = datetime(start_date.year, 4, 1)
            previous_end_date = datetime(start_date.year, 6, 30)
        elif current_quarter == 4:
            # Previous quarter is Q3 of current year
            previous_start_date = datetime(start_date.year, 7, 1)
            previous_end_date = datetime(start_date.year, 9, 30)

        # Adjust the previous_end_date to match the same relative position within the previous quarter
        previous_end_date = previous_start_date + timedelta(days=(end_date - start_date).days)

        # Previous YoY start and end dates should reference the same quarter last year
        previous_yoy_start_date = previous_start_date.replace(year=start_date.year - 1)
        previous_yoy_end_date = previous_end_date.replace(year=end_date.year - 1)
        
    elif period_name == 'EOM':
        # Compare to the same days in the previous month for EOM
        previous_start_date = (start_date.replace(day=1) - timedelta(days=1)).replace(day=1)
        previous_end_date = start_date.replace(day=1) - timedelta(days=1)
        
        # Previous YoY for the same month last year
        previous_yoy_start_date = previous_start_date.replace(year=start_date.year - 1)
        previous_yoy_end_date = previous_end_date.replace(year=end_date.year - 1)
    
    elif period_name == 'EOQ':
        # Determine the current quarter
        current_quarter = (start_date.month - 1) // 3 + 1
        previous_end_date = start_date.replace(month=current_quarter * 3, day=1) - timedelta(days=1)
        previous_start_date = previous_end_date.replace(day=1) - timedelta(days=previous_end_date.day - 1)

        # Previous YoY for the same quarter last year
        previous_yoy_start_date = previous_start_date.replace(year=start_date.year - 1)
        previous_yoy_end_date = previous_end_date.replace(year=end_date.year - 1)

    elif period_name == 'YTD':
        # Compare to the same days in the previous year for YTD
        previous_start_date = start_date.replace(year=start_date.year - 1, month=1, day=1)
        previous_end_date = end_date.replace(year=end_date.year - 1)
        
        # Check if data exists for the intended YoY period
        if previous_start_date not in available_dates or previous_end_date not in available_dates:
            print(f"Data not available for the YoY comparison in {period_name} period.")
            previous_yoy_start_date, previous_yoy_end_date = None, None  # Set to None if data is missing
        else:
            # Previous YoY is simply the same period last year
            previous_yoy_start_date = previous_start_date.replace(year=start_date.year - 2)
            previous_yoy_end_date = previous_end_date.replace(year=end_date.year - 2)
        
    else:
        # Default values for undefined period names
        previous_start_date, previous_end_date = None, None
        previous_yoy_start_date, previous_yoy_end_date = None, None
        
    # Check if start and end dates are still None, print a warning message
    if previous_start_date is None or previous_end_date is None:
        print(f"Warning: {period_name} period did not get valid start/end dates.")

    return previous_start_date, previous_end_date, previous_yoy_start_date, previous_yoy_end_date


# Step 4: Expand rows by adding a 'Period' column and printing the data
summary_dfs = []
for period_name, (start_date, end_date) in date_ranges.items():
    print(period_name)
    print(start_date)
    print(end_date)
    previous_start_date, previous_end_date, previous_yoy_start_date, previous_yoy_end_date = get_previous_period_dates(period_name, start_date, end_date)
    
    print(f"Calculating Previous Period Dates for: {period_name}")
    print(f"  Start Date: {start_date} -> Previous Start Date: {previous_start_date}")
    print(f"  End Date: {end_date} -> Previous End Date: {previous_end_date}")
    print(f"  Previous YoY Start Date: {previous_yoy_start_date}")
    print(f"  Previous YoY End Date: {previous_yoy_end_date}")
    print()
