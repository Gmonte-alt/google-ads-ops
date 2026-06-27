"""
make_ggl_test.py
Generates a properly-formatted Google Ads Editor test CSV
(UTF-16, tab-delimited) with the columns Google expects for label imports.
"""

import csv
from pathlib import Path

OUT = Path("analysis/output/test_ggl_auto_label_v4.csv")

headers = [
    "Campaign", "Ad Group", "Keyword", "Criterion Type",
    "Labels", "Status", "Campaign Status", "Ad Group Status",
]

rows = [
    [
        "AUTO INSURANCE-ARIZONA-SEARCH-MOBILE-USA-STE",
        "Auto-NonGeneric-Freeway Insurance-Core-na",
        "freeway insurance",
        "Exact",
        "Brand:Confie",
        "Enabled",
        "Enabled",
        "Enabled",
    ],
    [
        "AUTO INSURANCE-ARIZONA-SEARCH-MOBILE-USA-STE",
        "Auto-NonGeneric-Freeway Insurance-Core-na",
        "freeway insurance quote",
        "Exact",
        "Brand:Confie",
        "Enabled",
        "Enabled",
        "Enabled",
    ],
]

# UTF-16 with BOM + tab delimiter — Google Ads Editor's native export format
with open(OUT, "w", encoding="utf-16", newline="") as f:
    writer = csv.writer(f, delimiter="\t")
    writer.writerow(headers)
    writer.writerows(rows)

print(f"Wrote {OUT.resolve()}")
print(f"Format: UTF-16, tab-delimited (matches Google Ads Editor export format)")