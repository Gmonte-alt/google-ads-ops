#!/usr/bin/env python
# Copyright 2019 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""This example generates keyword ideas from a list of seed keywords."""
# File name: ggl_keyword_volume.py
# File version: V000-000-012
# Note: This version builds on V000-000-011. 
#       In this modified version:
#
#       We maintain a mapping (keyword_variations_map) of original keywords to their variations generated with suffixes.
#       We iterate through this mapping and send requests to the Google Ads API only for the keyword variations we've generated.
#       When writing the results to the CSV file, we match each keyword variation with its corresponding original keyword from the mapping and include only those in the output.
# data file path: keywordplanner/data/tickers_list.csv
# output_file='keywordplanner/output/kwplanner_company_suffix.csv'
# insert into prompt: python c:\MyPrograms\workstation\GoogleAdsSearchReportsHrly\keywordplanner\ggl_keyword_volume.py -c 5428037747 -f keywordplanner/data/tickers_list.csv

import argparse
import sys
import time
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
import os
import csv

# Location IDs, language ID, and other constants
_DEFAULT_LOCATION_IDS = ["2840"]
_DEFAULT_LANGUAGE_ID = "1000"
os.environ["GOOGLE_ADS_CONFIGURATION_FILE_PATH"] = "authenticate/google-ads.yaml"

# Words to exclude during preprocessing
exclude_words = ["corporation", "inc", "corp", "llc", "incorporated", "plc", "limited", "ltd", "inc.", "and company", "& co.", "platforms"]

# Function to preprocess keywords
def preprocess_keywords(chunk, exclude_words):
    preprocessed_keywords = []
    additional_data = []
    for keyword, data in chunk:
        # Remove ".com" from keywords
        keyword = keyword.replace(".com", "")
        # Replace commas with spaces
        keyword = keyword.replace(",", " ")
        # Split the keyword into words
        words = keyword.split()
        # Exclude certain words
        words = [word for word in words if word.lower() not in exclude_words]
        # Join the words back into a keyword
        preprocessed_keyword = " ".join(words)
        # Append the preprocessed keyword to the list
        preprocessed_keywords.append(preprocessed_keyword)
        # Store additional data
        additional_data.append(data)
    return preprocessed_keywords, additional_data

# Function to main
def main(client, customer_id, location_ids, language_id, keyword_texts, page_url, output_file):
    keyword_plan_idea_service = client.get_service("KeywordPlanIdeaService")
    keyword_plan_network = client.enums.KeywordPlanNetworkEnum.GOOGLE_SEARCH_AND_PARTNERS
    location_rns = map_locations_ids_to_resource_names(client, location_ids)
    language_rn = client.get_service("GoogleAdsService").language_constant_path(language_id)

    # Write results to a CSV file
    with open(output_file, 'w', newline='', encoding='utf-8') as csvfile:
        fieldnames = ['Keyword', 'AdditionalData', 'Average Monthly Searches', 'Competition']
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()

        # Process keywords in batches of 20
        for i in range(0, len(keyword_texts), 20):
            chunk = keyword_texts[i:i+20]
            
            # Preprocess keywords
            processed_keywords, additional_data = preprocess_keywords(chunk, exclude_words)
            
            # Append "stock" to each keyword
            chunk_with_stock = [keyword + " etf" for keyword in processed_keywords]

            # Initialize a KeywordSeed object and set the "keywords" field
            # to be a list of StringValue objects.
            request = client.get_type("GenerateKeywordIdeasRequest")
            request.customer_id = customer_id
            request.language = language_rn
            request.geo_target_constants = location_rns
            request.include_adult_keywords = False
            request.keyword_plan_network = keyword_plan_network

            request.keyword_seed.keywords.extend(chunk_with_stock)

            # Make API request with retry logic
            for _ in exponential_backoff(retries=3):
                try:
                    keyword_ideas = keyword_plan_idea_service.generate_keyword_ideas(request=request)
                except GoogleAdsException as ex:
                    handle_google_ads_exception(ex)
                else:
                    break

            # Write keyword ideas to CSV
            if keyword_ideas:
                keyword_ideas_dict = {idea.text: idea for idea in keyword_ideas}
                for keyword, data in zip(chunk_with_stock, additional_data):
                    idea = keyword_ideas_dict.get(keyword)
                    if idea:
                        writer.writerow({
                            'Keyword': idea.text,
                            'AdditionalData': data,  # Include additional data
                            'Average Monthly Searches': idea.keyword_idea_metrics.avg_monthly_searches,
                            'Competition': idea.keyword_idea_metrics.competition.name
                        })

            # Add a delay to avoid sending too many requests too quickly
            time.sleep(1)  # 1 second delay between batches

    print(f'Results saved to {output_file}')

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

# Entry point of the script
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generates keyword ideas from a list of seed keywords."
    )

    parser.add_argument(
        "-c",
        "--customer_id",
        type=str,
        required=True,
        default='5428037747',
        help="The Google Ads customer ID.",
    )
    parser.add_argument(
        "-f",
        "--csv_file",
        type=str,
        required=False,
        help="CSV file containing keyword texts and additional data",
    )
    parser.add_argument(
        "-l",
        "--location_ids",
        nargs="+",
        type=str,
        required=False,
        default=_DEFAULT_LOCATION_IDS,
        help="Space-delimited list of location criteria IDs",
    )
    parser.add_argument(
        "-i",
        "--language_id",
        type=str,
        required=False,
        default=_DEFAULT_LANGUAGE_ID,
        help="The language criterion ID.",
    )

    args = parser.parse_args()

    # Read keyword texts and additional data from the CSV file, skipping the header row
    keyword_texts = []
    with open(args.csv_file, 'r', newline='', encoding='utf-8') as csvfile:
        reader = csv.reader(csvfile)
        next(reader)  # Skip the header row
        for idx, row in enumerate(reader):
            if idx >= 3200: # adjust this value to limit the total keywords processed
                break
            if row:  # Check if the row is not empty
                keyword_texts.append((row[1].lower(), row[0]))  # Append the second column value and additional data

    googleads_client = GoogleAdsClient.load_from_storage(version="v16")

    try:
        main(
            googleads_client,
            args.customer_id,
            args.location_ids,
            args.language_id,
            keyword_texts,
            None,  # page_url is not used in this script
            output_file='keywordplanner/output/kwplanner_company_etf.csv',  # Output CSV file
        )
    except GoogleAdsException as ex:
        handle_google_ads_exception(ex)