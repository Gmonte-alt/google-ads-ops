from datetime import datetime, timedelta


today = datetime.today()
yesterday = today - timedelta(days=1)
yesterday_str = yesterday.strftime('%Y-%m-%d')
day_before_yesterday = yesterday - timedelta(days=1)
day_before_yesterday_str = day_before_yesterday.strftime('%Y-%m-%d')
last_week_same_day = yesterday - timedelta(days=7)
last_week_same_day_str = last_week_same_day.strftime('%Y-%m-%d')
two_weeks_ago_same_day = yesterday - timedelta(days=14)
two_weeks_ago_same_day_str = two_weeks_ago_same_day.strftime('%Y-%m-%d')

# Get the ISO calendar week and weekday for yesterday
iso_year, iso_week, iso_weekday = yesterday.isocalendar()

# Calculate the same day of the same ISO week from the previous year
last_year_same_iso_week = datetime.strptime(f'{iso_year-1}-W{iso_week}-{iso_weekday}', "%G-W%V-%u")
last_year_same_iso_week_str = last_year_same_iso_week.strftime('%Y-%m-%d')

start_of_last_year_iso_week = datetime.strptime(f'{iso_year-1}-W{iso_week}-1', "%G-W%V-%u")
start_of_last_year_iso_week_str = start_of_last_year_iso_week.strftime('%Y-%m-%d')

print(today)
print(yesterday)
print(yesterday_str)
print(day_before_yesterday)
print(day_before_yesterday_str)
print(last_week_same_day)
print(last_week_same_day_str)
print(two_weeks_ago_same_day)
print(two_weeks_ago_same_day_str)
print(iso_year, iso_week, iso_weekday)
print(last_year_same_iso_week)
print("last_year_same_iso_week_str: " + last_year_same_iso_week_str)
print("start_of_last_year_iso_week_str: " + start_of_last_year_iso_week_str)
print("iso weekday: ", iso_weekday)


# Function to calculate the start of last month
def start_of_last_month(date=None):
    if date is None:
        date = datetime.today()
    first_day_of_this_month = date.replace(day=1)
    last_day_of_last_month = first_day_of_this_month - timedelta(days=1)
    start_of_last_month = last_day_of_last_month.replace(day=1)
    return start_of_last_month

# Function to calculate the start of the month prior to last month
def start_of_month_before_last(date=None):
    if date is None:
        date = datetime.today()
    start_of_last_month_date = start_of_last_month(date)
    last_day_of_month_before_last = start_of_last_month_date - timedelta(days=1)
    start_of_month_before_last = last_day_of_month_before_last.replace(day=1)
    return start_of_month_before_last


# Calculate start of last month
start_of_last_month_date = start_of_last_month(yesterday)
start_of_last_month_str = start_of_last_month_date.strftime('%Y-%m-%d')

# Calculate equivalent day of last month
last_month_same_day = start_of_last_month_date.replace(day=yesterday.day)
last_month_same_day_str = last_month_same_day.strftime('%Y-%m-%d')

# Calculate start of the month prior to last month
start_of_month_before_last_date = start_of_month_before_last(yesterday)
start_of_month_before_last_str = start_of_month_before_last_date.strftime('%Y-%m-%d')

# Calculate equivalent day of the month before last month
month_before_last_same_day = start_of_month_before_last_date.replace(day=yesterday.day)
month_before_last_same_day_str = month_before_last_same_day.strftime('%Y-%m-%d')

print("Start of last month:", start_of_last_month_str)
print("Equivalent day of last month:", last_month_same_day_str)
print("Start of month before last:", start_of_month_before_last_str)
print("Equivalent day of month before last:", month_before_last_same_day_str)

