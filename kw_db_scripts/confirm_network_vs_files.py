"""
confirm_network_vs_files.py
===========================
The network table is suspected CORRECT (kpd is the inflated one). This confirms
it by reconciling the network table against the SOURCE FILE totals — the ground
truth — rather than against inflated kpd.

For each month, it compares the network table's total spend to the sum the
source keyword file reported. Since the network table is a faithful per-file
write, network-table monthly spend should match the file's monthly spend to
the penny (no instance-duplication, because the network table resolves each
keyword to its current single instance).

You provide the file totals (from each load's "Spend: $X" summary line) OR
this reads them from the CSVs if a folder is given.

Usage:
    # Compare network table months to a folder of source CSVs:
    python confirm_network_vs_files.py "C:\\...\\input\\microsoft-kw"
"""

import sys
import sqlite3
import pandas as pd
from pathlib import Path
import re

DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
FOLDER = Path(sys.argv[1]) if len(sys.argv) > 1 else \
    Path(r"C:\Projects\google-ads-ops\kw_db_scripts\input\microsoft-kw")

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row


def file_spend_by_month(csv):
    """Total spend per month from one source CSV."""
    raw = open(csv, encoding="utf-8-sig").read().splitlines()
    try:
        hdr = next(i for i, l in enumerate(raw) if "Keyword ID" in l and "Ad group ID" in l)
    except StopIteration:
        return {}
    df = pd.read_csv(csv, skiprows=hdr, dtype=str)
    df = df[df.iloc[:, 0] != "Total"]
    df = df[df["Date"].notna() & (df["Date"].astype(str).str.strip() != "-")]
    df["sp"] = pd.to_numeric(
        df["Spend"].astype(str).str.replace(",", "", regex=False).str.replace("$", "", regex=False),
        errors="coerce").fillna(0)
    df["mon"] = df["Date"].astype(str).str[:7]  # YYYY-MM (Date is YYYY-MM-DD)
    return df.groupby("mon")["sp"].sum().to_dict()


# Aggregate file totals by month across all CSVs (last file wins per month,
# matching load semantics — a later full-month file supersedes earlier partials)
files = sorted(FOLDER.glob("microsoft-*-kw-*.csv"))
print(f"Found {len(files)} source CSVs in {FOLDER.name}\\\n")

file_months = {}
for f in files:
    for mon, sp in file_spend_by_month(f).items():
        # keep the max monthly total seen (the most complete file for that month)
        file_months[mon] = max(file_months.get(mon, 0), sp)

# Network table spend by month
net_rows = conn.execute("""
    SELECT substr(report_date,1,7) mon, ROUND(SUM(cost_micros)/1e6,2) spend
    FROM keyword_performance_network_daily WHERE device='ALL'
    GROUP BY mon ORDER BY mon
""").fetchall()
net_months = {r["mon"]: r["spend"] for r in net_rows}

print(f"{'month':<9} {'file_spend':>14} {'network_spend':>15} {'delta':>12} {'match':>7}")
print(f"{'-'*9} {'-'*14} {'-'*15} {'-'*12} {'-'*7}")
all_months = sorted(set(file_months) | set(net_months))
for m in all_months:
    fs = file_months.get(m, 0)
    ns = net_months.get(m, 0)
    d = fs - ns
    match = "OK" if abs(d) < max(1, 0.01 * fs) else "DIFF"
    print(f"{m:<9} ${fs:>12,.2f} ${ns:>13,.2f} ${d:>10,.2f} {match:>7}")

conn.close()
print("\nIf file_spend == network_spend per month, the network table is a faithful")
print("reproduction of the source files (correct). Any DIFF is a month whose file")
print("wasn't loaded, or a partial-vs-full file mismatch.")
