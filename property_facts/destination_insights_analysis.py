# file name:
# version:
# notes:


import sqlite3
import pandas as pd

# Database path
sqlite_db_path = 'ff_snowflake.db'
output_table_name = "destination_insights"

# Connect to the database
conn = sqlite3.connect(sqlite_db_path)
cursor = conn.cursor()

# Step 0: Drop the destination_insights table if it already exists
cursor.execute(f"DROP TABLE IF EXISTS {output_table_name}")
conn.commit()

# Step 1: Query property_listing_data for unique city-state pairs and count of Listing_IDs
# with the specified filters
city_state_count_query = """
SELECT DISTINCT p.property_city AS City, s.state_abbreviation AS "State Abbreviation", COUNT(p.Listing_ID) AS total_listings
FROM property_listing_data AS p
JOIN state_abbreviations AS s ON p.property_state = s.state_name
LEFT JOIN STR_BAN AS str ON p.property_city = str.city AND p.property_state = str.state
WHERE ((str.city IS NOT NULL AND str.state IS NOT NULL) OR p.property_state = 'Florida')
AND p.ACTIVE_FLAG = 1
AND p.PROPERTY_PAID_FLAG = 1
AND p.UNIT_HIDDEN_FLAG = 0
AND p.HAS_MIN_PHOTO_COUNT_FLAG = 1
AND p.TEST_PROPERTY_FLAG = 0
AND p.property_live_status_flag = TRUE
GROUP BY p.property_city, s.state_abbreviation
"""
city_state_count_df = pd.read_sql_query(city_state_count_query, conn)

# Step 2: Load city_search_volume_fact table and find the highest "Average Monthly Searches" per city-state
search_volume_query = """
SELECT City, "State Abbreviation", Keyword, "Average Monthly Searches"
FROM city_search_volume_fact;
"""
search_volume_df = pd.read_sql_query(search_volume_query, conn)

# Step 3: Merge the two DataFrames on city and state_abbreviation
merged_df = pd.merge(city_state_count_df, search_volume_df, how="left", on=["City", "State Abbreviation"])

# Step 4: Filter for the highest "Average Monthly Searches" for each city-state pair
highest_search_df = merged_df.loc[merged_df.groupby(["City", "State Abbreviation"])['Average Monthly Searches'].idxmax()]

# Step 5: Create columns for specific keyword terms and surface max monthly searches for each
keyword_terms = ["corporate", "furnished", "short term", "month", "sublet", "by owner", "landlord"]

# Initialize columns for each keyword term with default value of None
for term in keyword_terms:
    highest_search_df[f'{term}_monthly_searches'] = None

# Populate columns with the maximum "Average Monthly Searches" for keywords containing the term
for term in keyword_terms:
    term_df = merged_df[merged_df['Keyword'].str.contains(term, case=False, na=False)]
    term_max_searches = term_df.loc[term_df.groupby(['City', 'State Abbreviation'])['Average Monthly Searches'].idxmax()]
    highest_search_df = pd.merge(
        highest_search_df,
        term_max_searches[['City', 'State Abbreviation', 'Average Monthly Searches']],
        how='left',
        on=['City', 'State Abbreviation'],
        suffixes=('', f'_{term}_monthly_searches')
    )

# Step 6: Rename columns for clarity
highest_search_df = highest_search_df.rename(columns={
    "city": "City",
    "state_abbreviation": "State Abbreviation",
    "total_listings": "Total Listings",
    "keyword": "Top Keyword",
    "Average Monthly Searches": "Top Keyword Monthly Searches"
})

# Step 7: Save the final DataFrame to the SQLite table
highest_search_df.to_sql(output_table_name, conn, if_exists="replace", index=False)

# Close the database connection
conn.close()

print(f"Data successfully saved to the {output_table_name} table in {sqlite_db_path}.")
