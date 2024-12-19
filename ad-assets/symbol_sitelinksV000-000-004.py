# ad_assets/symbol_sitelinks.py
# file version: V000-000-004

import pandas as pd

# Load ticker symbols from CSV file
ticker_df = pd.read_csv("keywordplanner/data/ticker_Campaign_AdgroupMap.csv")

# Extract ticker symbols
ticker_symbols = ticker_df.iloc[:, 0].tolist()

# Create a DataFrame with the provided data
data = {
    "Link Text": [
        "Quant Ratings",
        "Analyst Ratings",
        "Wall St Ratings",
        "Dividend Grades",
        "Dividend Yield",
        "Dividend Growth",
        "Dividend Safety",
        "Valuation Grade",
        "Growth Grade",
        "Profitability Grade",
        "Momentum Grade",
        "Revisions Grade"
    ],
    "Final URL": [
        "https://seekingalpha.com/symbol/V/ratings/quant-ratings",
        "https://seekingalpha.com/symbol/V/ratings/author-ratings",
        "https://seekingalpha.com/symbol/V/ratings/sell-side-ratings",
        "https://seekingalpha.com/symbol/V/dividends/scorecard",
        "https://seekingalpha.com/symbol/V/dividends/yield",
        "https://seekingalpha.com/symbol/V/dividends/dividend-growth",
        "https://seekingalpha.com/symbol/V/dividends/dividend-safety",
        "https://seekingalpha.com/symbol/V/valuation/metrics",
        "https://seekingalpha.com/symbol/V/growth",
        "https://seekingalpha.com/symbol/V/profitability",
        "https://seekingalpha.com/symbol/V/momentum/performance",
        "https://seekingalpha.com/symbol/V/earnings/revisions"
    ]
}

# Create an empty list to store modified DataFrames
modified_dfs = []

# Process each ticker symbol
for symbol in ticker_symbols:
    # Create a copy of the DataFrame to avoid overwriting previous iterations
    temp_df = pd.DataFrame(data)
    
    # Add ticker symbol in front of Link Text values
    temp_df["Link Text"] = symbol + " " + temp_df["Link Text"]
    
    # Replace ticker symbol in Final URL
    temp_df["Final URL"] = temp_df["Final URL"].str.replace("/V/", "/" + symbol + "/")
    
    # Add ticker symbol to the DataFrame
    temp_df["Campaign"] = ticker_df.loc[ticker_df.iloc[:, 0] == symbol, ticker_df.columns[1]].values[0]
    temp_df["Ad Group"] = ticker_df.loc[ticker_df.iloc[:, 0] == symbol, ticker_df.columns[2]].values[0]
    
    # Reorder the columns
    temp_df = temp_df[['Campaign', 'Ad Group', 'Link Text', 'Final URL']]
    
    # Append the modified DataFrame to the list
    modified_dfs.append(temp_df)

# Concatenate all modified DataFrames into one DataFrame
final_df = pd.concat(modified_dfs, ignore_index=True)

# Export the final DataFrame to a CSV file
final_df.to_csv("ad_assets/output/sitelinks_data.csv", index=False)
