"""
verify_csv_impressions.py  (v2 - uses same header detection as loader 09)
========================================================================
Reads the Microsoft campaign performance CSV directly, filters to the
target date range, sums impressions and clicks by network and device,
and compares to UI and DB values to determine where any missing
impressions are.

If CSV totals match UI: loader is dropping rows on ingest.
If CSV totals match DB: loader is correct; CSV/export is the problem.
If CSV matches neither: three-way mismatch needs deeper investigation.

Run:
    python verify_csv_impressions.py
"""

import pandas as pd
from pathlib import Path

# Path to the most recent campaign performance CSV
CSV_PATH = Path(
    r"C:\Projects\google-ads-ops\campaign_db_scripts\input\microsoft-campaign-daily"
    r"\microsoft-home-camp-perf-20260510.csv"
)
START = "2026-05-04"
END = "2026-05-10"

# UI reference numbers (from your message)
UI_SEARCH_IMP = 51_445
UI_AUDIENCE_IMP = 718_693

# DB values (from your diagnose_microsoft_impressions.py output)
DB_SEARCH_IMP = 42_374
DB_AUDIENCE_IMP = 12_842


def read_ms_csv(filepath: Path) -> pd.DataFrame:
    """
    Reads a Microsoft Ads Reports CSV export, handling the metadata
    header section and footer rows the same way loader 09 does.
    """
    if not filepath.exists():
        raise FileNotFoundError(f"CSV not found: {filepath}")

    with open(filepath, encoding="utf-8-sig") as f:
        lines = f.readlines()

    # Find the header row by scanning for "Account name"
    header_idx = None
    for i, line in enumerate(lines):
        stripped = line.lstrip("\ufeff").lstrip()
        if stripped.startswith('"Account name"') or stripped.startswith("Account name"):
            header_idx = i
            break

    if header_idx is None:
        raise ValueError(f"Could not find 'Account name' header row in {filepath.name}")

    header_line = lines[header_idx]
    # Microsoft sometimes exports as tab-separated despite the .csv extension
    sep = "\t" if header_line.count("\t") > header_line.count(",") else ","

    df = pd.read_csv(
        filepath,
        skiprows=header_idx,
        sep=sep,
        encoding="utf-8-sig",
        dtype=str,
        thousands=",",
    )

    # Strip the footer rows (Total, ©copyright, empty)
    first_col = df.columns[0]
    df = df[df[first_col].notna()]
    df = df[df[first_col] != "Total"]
    df = df[~df[first_col].astype(str).str.startswith("©")]

    return df


def main():
    print("=" * 90)
    print(f"CSV-side impression check  |  {CSV_PATH.name}")
    print(f"Filter: dates {START} to {END}")
    print("=" * 90)
    print()

    df = read_ms_csv(CSV_PATH)
    print(f"  Raw rows after header/footer strip: {len(df):,}")
    print(f"  Columns ({len(df.columns)}): {list(df.columns)}")
    print()

    # Detect relevant columns flexibly
    def col_match(candidates):
        for c in candidates:
            if c in df.columns:
                return c
        return None

    date_col = col_match(["Date", "Gregorian date"])
    net_col = col_match(["Network", "Network type"])
    dev_col = col_match(["Device type", "Device"])
    imp_col = col_match(["Impressions", "Impr."])
    clk_col = col_match(["Clicks", "Gross clicks"])
    spend_col = col_match(["Spend", "Cost"])

    print(f"  Detected columns:")
    print(f"    date        : {date_col}")
    print(f"    network     : {net_col}")
    print(f"    device      : {dev_col}")
    print(f"    impressions : {imp_col}")
    print(f"    clicks      : {clk_col}")
    print(f"    spend       : {spend_col}")
    print()

    missing = [name for name, c in [
        ("date", date_col), ("network", net_col), ("device", dev_col),
        ("impressions", imp_col), ("clicks", clk_col),
    ] if c is None]
    if missing:
        print(f"ERROR: Could not detect required columns: {missing}")
        return 1

    # Filter to date range
    df = df.dropna(subset=[date_col])
    df = df[df[date_col].str.match(r"^\d{4}-\d{2}-\d{2}$", na=False)]
    df_window = df[(df[date_col] >= START) & (df[date_col] <= END)].copy()
    print(f"  Rows in window {START} to {END}: {len(df_window):,}")
    print()

    # Convert numeric columns
    for col in [imp_col, clk_col, spend_col]:
        if col:
            df_window[col] = pd.to_numeric(df_window[col], errors="coerce").fillna(0)

    # ── By network ──
    print(f"  CSV totals by network (window only, all devices):")
    print(f"  {'Network':<40} {'Rows':>8} {'Impressions':>14} {'Clicks':>10} "
          f"{'Spend':>12}")
    print(f"  {'-'*40} {'-'*8} {'-'*14} {'-'*10} {'-'*12}")

    for net, group in df_window.groupby(net_col):
        imp = group[imp_col].sum()
        clk = group[clk_col].sum()
        spend = group[spend_col].sum() if spend_col else 0
        print(f"  {(str(net) or 'NULL'):<40} {len(group):>8,} {int(imp):>14,} "
              f"{int(clk):>10,} ${spend:>11,.2f}")
    print()

    # ── By network × device ──
    print(f"  CSV totals by network x device:")
    print(f"  {'Network':<36} {'Device':<10} {'Rows':>8} {'Impressions':>14} "
          f"{'Clicks':>10}")
    print(f"  {'-'*36} {'-'*10} {'-'*8} {'-'*14} {'-'*10}")
    for (net, dev), group in df_window.groupby([net_col, dev_col]):
        imp = group[imp_col].sum()
        clk = group[clk_col].sum()
        print(f"  {(str(net) or 'NULL'):<36} {(str(dev) or 'NULL'):<10} "
              f"{len(group):>8,} {int(imp):>14,} {int(clk):>10,}")
    print()

    # ── Verdict ──
    audience_csv_imp = int(df_window[df_window[net_col] == "Audience"][imp_col].sum())
    search_nets = ["Microsoft sites and select traffic", "AOL search", "Bing and web"]
    search_csv_imp = int(df_window[df_window[net_col].isin(search_nets)][imp_col].sum())

    print("=" * 90)
    print("VERDICT")
    print("=" * 90)
    print()
    print(f"  Search impressions ({', '.join(search_nets)}):")
    print(f"    UI says        : {UI_SEARCH_IMP:>10,}")
    print(f"    DB has         : {DB_SEARCH_IMP:>10,}")
    print(f"    CSV has        : {search_csv_imp:>10,}")
    print()
    print(f"  Audience impressions:")
    print(f"    UI says        : {UI_AUDIENCE_IMP:>10,}")
    print(f"    DB has         : {DB_AUDIENCE_IMP:>10,}")
    print(f"    CSV has        : {audience_csv_imp:>10,}")
    print()

    audience_csv_matches_ui = abs(audience_csv_imp - UI_AUDIENCE_IMP) < 5000
    audience_csv_matches_db = abs(audience_csv_imp - DB_AUDIENCE_IMP) < 1000

    if audience_csv_matches_ui and not audience_csv_matches_db:
        print("  -> CSV matches UI. Loader IS dropping impression rows.")
        print("     Action: investigate loader 09's impression handling.")
    elif audience_csv_matches_db and not audience_csv_matches_ui:
        print("  -> CSV matches DB. Loader is correctly importing what the CSV has.")
        print("  -> The export itself is undercounting impressions.")
        print("     Action: re-export from Microsoft Ads UI with correct column")
        print("     selection. Most likely the Impressions column being exported")
        print("     is a filtered variant or the report has implicit row filters.")
    elif audience_csv_matches_ui and audience_csv_matches_db:
        print("  -> CSV matches both. (This shouldn't be possible -- UI and DB differ.)")
    else:
        print("  -> CSV matches neither. Three-way mismatch -- needs investigation.")


if __name__ == "__main__":
    raise SystemExit(main() or 0)
