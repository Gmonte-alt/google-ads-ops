# ad_assets/symbol_sitelinks.py
# file version: V000-000-000

import pandas as pd

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

# Add "AAPL " in front of Link Text values
df["Link Text"] = "AAPL " + df["Link Text"]

# Replace "V" in Final URL with "AAPL"
df["Final URL"] = df["Final URL"].str.replace("/V/", "/AAPL/")

# Print the modified DataFrame
print(df)
