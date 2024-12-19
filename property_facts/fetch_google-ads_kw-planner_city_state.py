# File name: 
# version: V000-000-001
# Notes:

import sqlite3
import csv
import os
import time
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

# Define city-based search terms to append to each city
search_terms = [
    "corporate housing", "furnished rentals", "short term rentals",
    "monthly rentals", "short term apartment rentals", "furnished apartments",
    "sublets", "for rent by owner"
]

# Define output CSV file path
output_file = 'property_facts/output/city_based_search_volume.csv'

# Load processed city-state pairs from existing CSV file, if it exists
processed_cities = set()
if os.path.exists(output_file):
    with open(output_file, 'r', newline='', encoding='utf-8') as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            processed_cities.add((row['City'], row['State Abbreviation']))

# Connect to SQLite database
sqlite_db_path = 'ff_snowflake.db'
conn = sqlite3.connect(sqlite_db_path)
cursor = conn.cursor()

# Query to get city and state abbreviation data, filtering out already processed city-state pairs
city_state_query = """
SELECT DISTINCT p.property_city AS city, s.state_abbreviation
FROM property_listing_data AS p
JOIN state_abbreviations AS s ON p.property_state = s.state_name
LEFT JOIN STR_BAN AS str ON p.property_city = str.city AND p.property_state = str.state
WHERE ((str.city IS NOT NULL AND str.state IS NOT NULL) OR p.property_state = 'Florida')
AND (p.property_city, s.state_abbreviation) NOT IN ({});
""".format(",".join(["('{}', '{}')".format(city, state) for city, state in processed_cities]))

# Execute query
cursor.execute(city_state_query)
city_state_data = cursor.fetchall()

# Initialize Google Ads API client
googleads_client = GoogleAdsClient.load_from_storage("authentication/google-ads.yaml")

# Define Google Ads customer ID and constants
customer_id = "7554573980"
location_ids = ["2840"]  # Adjust location ID for your target
language_id = "1000"  # English

# Function to handle Google Ads API exceptions
def handle_google_ads_exception(ex):
    print(
        f'Request with ID "{ex.request_id}" failed with status '
        f'"{ex.error.code().name}" and includes the following errors:'
    )
    for error in ex.failure.errors:
        print(f'\tError with message "{error.message}".')
        if error.location:
            for field_path_element in error.location.field_path_elements:
                print(f"\t\tOn field: {field_path_element.field_name}")
    sys.exit(1)

# Function to map location IDs to resource names
def map_locations_ids_to_resource_names(client, location_ids):
    build_resource_name = client.get_service("GeoTargetConstantService").geo_target_constant_path
    return [build_resource_name(location_id) for location_id in location_ids]

# Function to implement exponential backoff
def exponential_backoff(retries, delay=1):
    for _ in range(retries):
        yield
        time.sleep(delay)
        delay *= 2  # Exponential backoff

# Open CSV file to write results, ensuring headers are included if the file is new
with open(output_file, 'a', newline='', encoding='utf-8') as csvfile:
    fieldnames = ['City', 'State Abbreviation', 'Keyword', 'Average Monthly Searches', 'Competition']
    writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
    if os.path.getsize(output_file) == 0:
        writer.writeheader()  # Write header only if the file is new

    # Loop through each city and state abbreviation
    for city, state_abbreviation in city_state_data:
        for term in search_terms:
            keyword_text = f"{city} {state_abbreviation} {term}"

            # Set up Keyword Plan request
            keyword_plan_idea_service = googleads_client.get_service("KeywordPlanIdeaService")
            keyword_plan_network = googleads_client.enums.KeywordPlanNetworkEnum.GOOGLE_SEARCH_AND_PARTNERS
            location_rns = map_locations_ids_to_resource_names(googleads_client, location_ids)
            language_rn = googleads_client.get_service("GoogleAdsService").language_constant_path(language_id)

            # Create the GenerateKeywordIdeasRequest
            request = googleads_client.get_type("GenerateKeywordIdeasRequest")
            request.customer_id = customer_id
            request.language = language_rn
            request.geo_target_constants = location_rns
            request.include_adult_keywords = False
            request.keyword_plan_network = keyword_plan_network

            # Add keyword as a seed
            request.keyword_seed.keywords.append(keyword_text)

            # Send request to Google Ads API with retry logic
            for _ in exponential_backoff(retries=3):
                try:
                    keyword_ideas = keyword_plan_idea_service.generate_keyword_ideas(request=request)
                    break
                except GoogleAdsException as ex:
                    handle_google_ads_exception(ex)

            # Write keyword ideas to CSV
            if keyword_ideas:
                for idea in keyword_ideas:
                    writer.writerow({
                        'City': city,
                        'State Abbreviation': state_abbreviation,
                        'Keyword': idea.text,
                        'Average Monthly Searches': idea.keyword_idea_metrics.avg_monthly_searches,
                        'Competition': idea.keyword_idea_metrics.competition.name
                    })

            # Add a delay to avoid rate limits
            time.sleep(1)

# Close database connection
conn.close()
print(f"Results saved to {output_file}")
