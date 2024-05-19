# ad_assets/symbol_sitelinks.py
# file version: V000-000-001

import pandas as pd

# Load ticker symbols from CSV file, skipping the first row
ticker_df = pd.read_csv("keywordplanner/data/ticker_Campaign_AdgroupMap.csv", header=None, skiprows=1)
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

df = pd.DataFrame(data)

# Process each ticker symbol
for symbol in ticker_symbols:
    # Create a copy of the DataFrame to avoid overwriting previous iterations
    temp_df = df.copy()
    
    # Add ticker symbol in front of Link Text values
    temp_df["Link Text"] = symbol + " " + temp_df["Link Text"]
    
    # Replace ticker symbol in Final URL
    temp_df["Final URL"] = temp_df["Final URL"].str.replace("/V/", "/" + symbol + "/")
    
    # Print or do something with the modified DataFrame
    print(temp_df)
