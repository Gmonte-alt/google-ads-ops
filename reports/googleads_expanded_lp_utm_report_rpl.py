"""
googleads_expanded_lp_utm_report.py  [V003]
-------------------------------------------
Pulls the EXPANDED landing page URL (post tracking-template / ad-param
substitution) from the Google Ads API and explodes every query-string
parameter (utm_campaign, utm_source, gad_campaignid, ...) into columns.

CHANGELOG V002 -> V003
    * Pull now includes metrics.conversions + metrics.conversions_value
      (Google-NATIVE primary, click-date attributed -- same caveats as
      the geo view: reconciles ~1.5% vs routed-primary truth).
    * Per-click RPL: converted URL rows (conversions > 0) get
      Value/Conv = conversions_value / conversions. Because expanded
      URLs are ~click-grain in this account, this approximates the
      per-lead payout of each converted click.
    * New sheets: "RPL Band Distribution" (Month x band, weighted by
      conversions) and "Converted Clicks" (row-level detail).
    * RPL_BANDS constant defines band edges -- adjust to taste.

CHANGELOG V001 -> V002
    * Parse each DISTINCT URL once (2.2M) instead of every row (3.3M),
      via a single-pass loop + from_records — replaces the very slow
      .apply(pd.Series) pattern.
    * Excel row-limit safe: full daily x URL data no longer goes to
      xlsx (3.3M rows exceeds Excel's 1,048,576-row cap and would
      crash the write). It's now an opt-in CSV (--write-full-csv).
    * New "Param Summary" sheet: per-parameter fill rate + distinct
      value count, to identify which param is driving URL cardinality.
    * Daily sheet is now aggregated to Date x Campaign x utm_* params
      only (high-cardinality non-utm params excluded from grouping),
      keeping it small and Excel-safe.
    * Row-limit guard on every sheet: spills to CSV with a warning
      instead of crashing.

Auth follows 03_sync_keyword_performance.py exactly:
    .env -> GOOGLE_ADS_CONFIGURATION_FILE_PATH -> load_from_storage()
    per-account customer IDs from .env (GOOGLE_ADS_CUSTOMER_ID_*)

Output: reports/output/expanded_lp_utm_report_<account>.xlsx
    Sheet 1  "UTM Campaign History"  Campaign x utm_campaign value with
                                     first/last served date + clicks
    Sheet 2  "Daily UTM Data"        Date x Campaign x utm_* params,
                                     clicks/impressions/cost summed
    Sheet 3  "Param Summary"         every query param: fill rate,
                                     distinct values, example value
Optional: --write-full-csv  full row-level exploded data as CSV

Usage:
    conda activate paid-search
    python googleads_expanded_lp_utm_report.py --account home --start-date 2024-04-01
"""

import os
import sys
import argparse
from pathlib import Path
from datetime import date, timedelta
from urllib.parse import urlsplit, parse_qs
from dateutil.relativedelta import relativedelta

import pandas as pd
from dotenv import load_dotenv
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException


# ---------------------------------------------------------------------------
# Configuration  (mirrors 03_sync_keyword_performance.py)
# ---------------------------------------------------------------------------

load_dotenv()
_cfg_path = os.getenv("GOOGLE_ADS_CONFIGURATION_FILE_PATH")
if not _cfg_path:
    sys.exit("ERROR: GOOGLE_ADS_CONFIGURATION_FILE_PATH not set in .env "
             "(same variable 03_sync_keyword_performance.py uses).")
os.environ["GOOGLE_ADS_CONFIGURATION_FILE_PATH"] = _cfg_path

# Leave as None to use the client library's default API version (matches 03).
# Set to e.g. "v25" to pin a specific major version.
API_VERSION = None

OUTPUT_DIR      = Path("reports/output")
TODAY           = date.today()
DEFAULT_MONTHS  = 12
EXCEL_MAX_ROWS  = 1_048_575   # Excel sheet cap minus header row

# Per-conversion value band edges (USD). Last band is open-ended.
RPL_BANDS  = [0, 25, 40, 55, 70, 85, 100]
RPL_LABELS = ["$0-25", "$25-40", "$40-55", "$55-70", "$70-85", "$85-100", "$100+"]

ACCOUNT_MAP = {
    "home":       {
        "customer_id_env":       "GOOGLE_ADS_CUSTOMER_ID_HOME",
        "login_customer_id_env": None,
        "display_name":          "Home Insurance",
    },
    "medicare":   {
        "customer_id_env":       "GOOGLE_ADS_CUSTOMER_ID_MEDICARE",
        "login_customer_id_env": None,
        "display_name":          "Medicare",
    },
    "auto":       {
        "customer_id_env":       "GOOGLE_ADS_CUSTOMER_ID_AUTO",
        "login_customer_id_env": None,
        "display_name":          "Auto Insurance (autoinsurance.org)",
    },
    "term_life":  {
        "customer_id_env":       "GOOGLE_ADS_CUSTOMER_ID_TERM_LIFE",
        "login_customer_id_env": None,
        "display_name":          "Term Life",
    },
    "auto_quote": {
        "customer_id_env":       "GOOGLE_ADS_CUSTOMER_ID_AUTO_QUOTE",
        "login_customer_id_env": "GOOGLE_ADS_LOGIN_CUSTOMER_ID_AUTO_QUOTE",
        "display_name":          "Auto Insurance (auto.quote.com)",
    },
}


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Expanded landing page URL + query-param report."
    )
    parser.add_argument(
        "--account", required=True, choices=list(ACCOUNT_MAP.keys()),
        help="Account to pull.",
    )
    parser.add_argument(
        "--start-date", default=None, metavar="YYYY-MM-DD",
        help=f"Start of pull window (default: {DEFAULT_MONTHS} months back, "
             "first of month).",
    )
    parser.add_argument(
        "--end-date", default=None, metavar="YYYY-MM-DD",
        help="End of pull window (default: yesterday).",
    )
    parser.add_argument(
        "--campaign-ids", default=None, metavar="ID,ID,...",
        help="Restrict the pull to these campaign ids (comma-separated).",
    )
    parser.add_argument(
        "--write-full-csv", action="store_true",
        help="Also write the full row-level exploded data (Date x Campaign x "
             "URL x all params) as a CSV. Can be large (hundreds of MB).",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Google Ads client  (mirrors 03)
# ---------------------------------------------------------------------------

def get_google_ads_client(login_customer_id: str | None = None) -> GoogleAdsClient:
    client = GoogleAdsClient.load_from_storage()
    if login_customer_id:
        client.login_customer_id = login_customer_id
    return client


def get_ga_service(client: GoogleAdsClient):
    if API_VERSION:
        return client.get_service("GoogleAdsService", version=API_VERSION)
    return client.get_service("GoogleAdsService")


# ---------------------------------------------------------------------------
# Date chunking  (monthly, mirrors 03's approach)
# ---------------------------------------------------------------------------

def get_monthly_chunks(start_date: date, end_date: date):
    chunks = []
    chunk_start = start_date
    while chunk_start <= end_date:
        next_month  = (chunk_start.replace(day=1) + relativedelta(months=1))
        chunk_end   = min(next_month - timedelta(days=1), end_date)
        chunks.append((chunk_start.isoformat(), chunk_end.isoformat()))
        chunk_start = next_month
    return chunks


# ---------------------------------------------------------------------------
# API query
# ---------------------------------------------------------------------------

def fetch_expanded_lp_rows(ga_service, customer_id, start_date, end_date,
                           campaign_clause=""):
    query = f"""
        SELECT
            segments.date,
            campaign.id,
            campaign.name,
            expanded_landing_page_view.expanded_final_url,
            metrics.clicks,
            metrics.impressions,
            metrics.cost_micros,
            metrics.conversions,
            metrics.conversions_value
        FROM expanded_landing_page_view
        WHERE
            segments.date BETWEEN '{start_date}' AND '{end_date}'
            {campaign_clause}
    """
    rows = []
    response = ga_service.search_stream(customer_id=customer_id, query=query)
    for batch in response:
        for row in batch.results:
            rows.append({
                "Date":          row.segments.date,
                "Campaign ID":   row.campaign.id,
                "Campaign Name": row.campaign.name,
                "Expanded URL":  row.expanded_landing_page_view.expanded_final_url,
                "Clicks":        row.metrics.clicks,
                "Impressions":   row.metrics.impressions,
                "Cost":          row.metrics.cost_micros / 1e6,
                "Conversions":   row.metrics.conversions,
                "Conv Value":    row.metrics.conversions_value,
            })
    return rows


# ---------------------------------------------------------------------------
# URL parsing  (V002: parse each DISTINCT URL exactly once)
# ---------------------------------------------------------------------------

def parse_unique_urls(unique_urls) -> pd.DataFrame:
    """
    One record per distinct URL: Base URL + one key per query parameter.
    Repeated params (rare) are joined with ' | '.
    """
    records = []
    for url in unique_urls:
        parts = urlsplit(url)
        rec = {
            "Expanded URL": url,
            "Base URL":     f"{parts.scheme}://{parts.netloc}{parts.path}",
        }
        for key, values in parse_qs(parts.query, keep_blank_values=True).items():
            rec[key] = " | ".join(values)
        records.append(rec)
    parsed = pd.DataFrame.from_records(records)

    param_cols = [c for c in parsed.columns
                  if c not in ("Expanded URL", "Base URL")]
    utm_cols   = sorted(c for c in param_cols if c.lower().startswith("utm_"))
    other_cols = sorted(c for c in param_cols if not c.lower().startswith("utm_"))
    return parsed[["Expanded URL", "Base URL"] + utm_cols + other_cols]


def build_param_summary(parsed: pd.DataFrame, merged: pd.DataFrame) -> pd.DataFrame:
    """
    Per query parameter: how often it appears (URL-level and click-weighted)
    and how many distinct values it takes. High 'Distinct Values' = the
    param driving URL cardinality.
    """
    param_cols   = [c for c in parsed.columns
                    if c not in ("Expanded URL", "Base URL")]
    n_urls       = len(parsed)
    total_clicks = merged["Clicks"].sum()
    rows = []
    for col in param_cols:
        present = parsed[col].notna()
        clicks  = merged.loc[merged[col].notna(), "Clicks"].sum()
        example = parsed.loc[present, col].iloc[0] if present.any() else ""
        rows.append({
            "Parameter":        col,
            "% of URLs":        round(100 * present.sum() / n_urls, 1),
            "% of Clicks":      round(100 * clicks / total_clicks, 1) if total_clicks else 0,
            "Distinct Values":  parsed.loc[present, col].nunique(),
            "Example Value":    str(example)[:120],
        })
    return (pd.DataFrame(rows)
              .sort_values("Distinct Values", ascending=False)
              .reset_index(drop=True))


def build_daily_utm_data(merged: pd.DataFrame, utm_cols: list) -> pd.DataFrame:
    """
    Date x Campaign x utm_* params, metrics summed. Non-utm params are
    excluded from grouping so high-cardinality dynamic params don't
    explode the row count.
    """
    keys = ["Date", "Campaign ID", "Campaign Name"] + utm_cols
    return (
        merged.groupby(keys, dropna=False)
              .agg(**{
                  "Clicks":      ("Clicks", "sum"),
                  "Impressions": ("Impressions", "sum"),
                  "Cost":        ("Cost", "sum"),
                  "Conversions": ("Conversions", "sum"),
                  "Conv Value":  ("Conv Value", "sum"),
              })
              .reset_index()
              .sort_values(["Date", "Campaign Name"])
    )


def build_utm_campaign_history(merged: pd.DataFrame) -> pd.DataFrame:
    if "utm_campaign" not in merged.columns:
        return pd.DataFrame(
            columns=["Campaign ID", "Campaign Name", "utm_campaign",
                     "First Date", "Last Date", "Days Seen", "Clicks", "Cost"]
        )
    return (
        merged.groupby(["Campaign ID", "Campaign Name", "utm_campaign"],
                       dropna=False)
              .agg(**{
                  "First Date": ("Date", "min"),
                  "Last Date":  ("Date", "max"),
                  "Days Seen":  ("Date", "nunique"),
                  "Clicks":     ("Clicks", "sum"),
                  "Cost":       ("Cost", "sum"),
                  "Conversions": ("Conversions", "sum"),
                  "Conv Value":  ("Conv Value", "sum"),
              })
              .reset_index()
              .sort_values(["Campaign Name", "First Date"])
    )


def build_converted_clicks(merged: pd.DataFrame, utm_cols: list) -> pd.DataFrame:
    """
    Row-level detail for every converted URL row (~click grain):
    per-conversion value + band assignment.
    """
    conv = merged.loc[merged["Conversions"] > 0].copy()
    if conv.empty:
        return conv
    conv["Value/Conv"] = conv["Conv Value"] / conv["Conversions"]
    conv["RPL Band"]   = pd.cut(
        conv["Value/Conv"],
        bins=RPL_BANDS + [float("inf")],
        labels=RPL_LABELS,
        right=False,
        include_lowest=True,
    )
    conv["Month"] = conv["Date"].str[:7]
    keep = (["Month", "Date", "Campaign ID", "Campaign Name"] + utm_cols +
            ["Clicks", "Conversions", "Conv Value", "Value/Conv", "RPL Band",
             "Expanded URL"])
    keep = [c for c in keep if c in conv.columns]
    return conv[keep].sort_values(["Date", "Campaign Name"])


def build_rpl_band_distribution(conv: pd.DataFrame) -> pd.DataFrame:
    """
    Month x RPL band, weighted by conversions (not row count, so DDA
    fractional credit is handled correctly). Share = % of that month's
    conversions falling in the band.
    """
    if conv.empty:
        return pd.DataFrame(
            columns=["Month", "RPL Band", "Converted Rows", "Conversions",
                     "Conv Value", "Avg Value/Conv", "% of Month Convs"]
        )
    dist = (
        conv.groupby(["Month", "RPL Band"], observed=False)
            .agg(**{
                "Converted Rows": ("Value/Conv", "size"),
                "Conversions":    ("Conversions", "sum"),
                "Conv Value":     ("Conv Value", "sum"),
            })
            .reset_index()
    )
    dist["Avg Value/Conv"] = (dist["Conv Value"] / dist["Conversions"]).round(2)
    month_tot = dist.groupby("Month")["Conversions"].transform("sum")
    dist["% of Month Convs"] = (100 * dist["Conversions"] / month_tot).round(1)
    return dist.sort_values(["Month", "RPL Band"])


# ---------------------------------------------------------------------------
# Excel-safe writer
# ---------------------------------------------------------------------------

def write_sheet_or_csv(writer, df: pd.DataFrame, sheet_name: str,
                       csv_fallback: Path):
    if len(df) > EXCEL_MAX_ROWS:
        df.to_csv(csv_fallback, index=False)
        print(f"  WARNING: '{sheet_name}' has {len(df):,} rows "
              f"(> Excel cap {EXCEL_MAX_ROWS:,}); written to "
              f"{csv_fallback} instead.")
        return
    df.to_excel(writer, sheet_name=sheet_name, index=False)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args        = parse_args()
    account_cfg = ACCOUNT_MAP[args.account]

    customer_id = os.getenv(account_cfg["customer_id_env"])
    if not customer_id:
        raise ValueError(f"{account_cfg['customer_id_env']} not set in .env")

    login_customer_id = None
    if account_cfg["login_customer_id_env"]:
        login_customer_id = os.getenv(account_cfg["login_customer_id_env"])

    if args.start_date:
        start_date = date.fromisoformat(args.start_date)
    else:
        start_date = (TODAY - relativedelta(months=DEFAULT_MONTHS)).replace(day=1)
    end_date = date.fromisoformat(args.end_date) if args.end_date \
               else TODAY - timedelta(days=1)
    if end_date < start_date:
        print(f"  ERROR: end date {end_date} is before start {start_date}.")
        sys.exit(1)

    campaign_clause = ""
    if args.campaign_ids:
        ids = [c.strip() for c in args.campaign_ids.split(",") if c.strip()]
        campaign_clause = "AND campaign.id IN (" + ", ".join(ids) + ")"

    date_chunks = get_monthly_chunks(start_date, end_date)

    print("=" * 65)
    print("googleads_expanded_lp_utm_report.py  [V003]")
    print(f"Account  : {account_cfg['display_name']} ({customer_id})")
    print(f"Range    : {start_date.isoformat()} to {end_date.isoformat()}")
    print(f"Chunks   : {len(date_chunks)} month(s)")
    if campaign_clause:
        print(f"Scope    : {args.campaign_ids}")
    print("=" * 65)

    print("\nConnecting to Google Ads API...")
    client     = get_google_ads_client(login_customer_id)
    ga_service = get_ga_service(client)
    print("  Connected.")

    all_rows = []
    for i, (chunk_start, chunk_end) in enumerate(date_chunks, 1):
        print(f"[{i}/{len(date_chunks)}] {chunk_start} -> {chunk_end}...",
              end=" ", flush=True)
        try:
            rows = fetch_expanded_lp_rows(
                ga_service, customer_id, chunk_start, chunk_end, campaign_clause
            )
        except GoogleAdsException as ex:
            print(f"\n  Request failed with status {ex.error.code().name}:")
            for error in ex.failure.errors:
                print(f"\tError with message {error.message}.")
                if error.location:
                    for fpe in error.location.field_path_elements:
                        print(f"\t\tOn field: {fpe.field_name}")
            sys.exit(1)
        print(f"{len(rows):,} rows")
        all_rows.extend(rows)

    if not all_rows:
        print("\nNo rows returned for this window. Nothing to write.")
        sys.exit(0)

    df = pd.DataFrame(all_rows)
    del all_rows
    unique_urls = df["Expanded URL"].unique()
    print(f"\nTotal rows            : {len(df):,}")
    print(f"Distinct expanded URLs: {len(unique_urls):,}")

    print("Parsing distinct URLs...", end=" ", flush=True)
    parsed = parse_unique_urls(unique_urls)
    print("done.")

    print("Joining params back to daily rows...", end=" ", flush=True)
    merged = df.merge(parsed, on="Expanded URL", how="left")
    del df
    print("done.")

    param_cols = [c for c in parsed.columns
                  if c not in ("Expanded URL", "Base URL")]
    utm_cols   = [c for c in param_cols if c.lower().startswith("utm_")]

    if "utm_campaign" in merged.columns:
        print(f"Distinct utm_campaign : "
              f"{merged['utm_campaign'].nunique(dropna=True):,}")
    else:
        print("WARNING: no utm_campaign parameter found in any expanded URL.")

    history    = build_utm_campaign_history(merged)
    daily_utm  = build_daily_utm_data(merged, utm_cols)
    param_summ = build_param_summary(parsed, merged)
    conv_rows  = build_converted_clicks(merged, utm_cols)
    rpl_dist   = build_rpl_band_distribution(conv_rows)

    print(f"\nConverted URL rows    : {len(conv_rows):,} "
          f"({conv_rows['Conversions'].sum():,.1f} conversions, "
          f"${conv_rows['Conv Value'].sum():,.0f} value)" if len(conv_rows)
          else "\nNo converted rows in window.")
    if len(rpl_dist):
        pivot = rpl_dist.pivot(index="RPL Band", columns="Month",
                               values="% of Month Convs").fillna(0)
        recent = [c for c in pivot.columns][-6:]
        print("\nRPL band mix, % of monthly conversions (last 6 months):")
        print(pivot[recent].to_string())

    print("\nParam summary (distinct values -> cardinality driver at top):")
    print(param_summ.to_string(index=False))

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"expanded_lp_utm_report_{args.account}.xlsx"
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        write_sheet_or_csv(writer, history, "UTM Campaign History",
                           OUTPUT_DIR / f"utm_history_{args.account}.csv")
        write_sheet_or_csv(writer, daily_utm, "Daily UTM Data",
                           OUTPUT_DIR / f"daily_utm_{args.account}.csv")
        write_sheet_or_csv(writer, param_summ, "Param Summary",
                           OUTPUT_DIR / f"param_summary_{args.account}.csv")
        write_sheet_or_csv(writer, rpl_dist, "RPL Band Distribution",
                           OUTPUT_DIR / f"rpl_band_dist_{args.account}.csv")
        write_sheet_or_csv(writer, conv_rows, "Converted Clicks",
                           OUTPUT_DIR / f"converted_clicks_{args.account}.csv")
        for ws in writer.book.worksheets:
            for cell in ws[1]:
                cell.font = cell.font.copy(bold=True)
            ws.freeze_panes = "A2"

    if args.write_full_csv:
        full_path = OUTPUT_DIR / f"expanded_lp_full_{args.account}.csv"
        print(f"Writing full row-level CSV ({len(merged):,} rows)...",
              end=" ", flush=True)
        merged.to_csv(full_path, index=False)
        print(f"done -> {full_path}")

    print(f"\nDone. Output: {out_path}")


if __name__ == "__main__":
    main()
