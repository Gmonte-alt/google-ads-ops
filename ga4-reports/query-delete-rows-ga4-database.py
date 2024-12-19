# Connect to SQLite database

import sqlite3
from datetime import datetime, timedelta

conn = sqlite3.connect("ga4_data.db")
cursor = conn.cursor()

# Define the start and end dates to delete and backfill
delete_start_date = datetime.strptime("2024-06-17", "%Y-%m-%d")
delete_end_date = datetime.now() - timedelta(days=1)

# Delete existing data from June 20, 2024 to yesterday
cursor.execute("""
DELETE FROM ga4_events
WHERE Date >= ? AND Date <= ?
""", (delete_start_date.strftime("%Y-%m-%d"), delete_end_date.strftime("%Y-%m-%d")))
conn.commit()