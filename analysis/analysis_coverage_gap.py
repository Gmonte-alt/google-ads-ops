"""
analysis_coverage_gap.py
------------------------
Answers: "Are we missing keyword coverage on one platform?"

Produces an Excel workbook with four sheets:
    1. Summary          -- high-level coverage stats and spend at risk
    2. Google only      -- keywords on Google not on Microsoft, with performance
    3. Microsoft only   -- keywords on Microsoft not on Google, with performance
    4. Both platforms   -- keywords running on both, side-by-side metrics

All performance metrics use the last 90 days of unsegmented daily totals
(device='ALL', geo_state='ALL') to give a recent, comparable view.
Cost is converted from micros to dollars in the output.

Usage:
    conda activate paid-search
    python analysis/analysis_coverage_gap.py

Output:
    analysis/output/coverage_gap_home_insurance.xlsx
"""

import sqlite3
import os
import pandas as pd
from pathlib import Path
from datetime import date, timedelta
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PATH    = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
OUTPUT_DIR = Path(r"C:\Projects\google-ads-ops\analysis\output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

OUTPUT_FILE = OUTPUT_DIR / "coverage_gap_home_insurance.xlsx"

# Performance window.
# START_DATE is set to 2026-01-01 -- the earliest date both platforms
# have complete data. Microsoft exports start January 1, 2026.
# Google backfill covers March 2025 onward.
# Using a shared start date ensures spend and conversion comparisons
# are apples-to-apples across platforms.
#
# To revert to a rolling 90-day window, comment out the explicit dates
# and uncomment the DAYS_BACK lines below.
#
# DAYS_BACK   = 90
# END_DATE    = date.today() - timedelta(days=1)
# START_DATE  = END_DATE - timedelta(days=DAYS_BACK)
END_DATE    = date.today() - timedelta(days=1)
START_DATE  = date(2026, 1, 1)
DAYS_BACK   = (END_DATE - START_DATE).days

# Revenue comparison window -- Microsoft offline uploads began 2026-01-13.
# Traffic and coverage metrics use START_DATE (Jan 1).
# CPL and ROAS comparisons use REVENUE_START_DATE (Jan 13) for fairness.
# Note: some pre-Jan-13 offline credits exist due to correct click-date
# attribution (partner uploaded Jan 13, credits backdated to click dates).
# These are legitimate and included in the full window analysis.
REVENUE_START_DATE = date(2026, 1, 13)

# Account identifiers
GOOGLE_ACCOUNT_ID   = "8886502147"
MICROSOFT_ACCOUNT_ID = "F149SN63"

# Minimum Google spend threshold to flag as a priority gap
# Keywords below this spend level in the window are lower priority to add
PRIORITY_SPEND_THRESHOLD = 10.00   # dollars


# ---------------------------------------------------------------------------
# Database connection
# ---------------------------------------------------------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Core queries
# ---------------------------------------------------------------------------

def get_google_keyword_performance(conn) -> pd.DataFrame:
    """
    Returns all Google Home Insurance keyword identities with their
    aggregated performance over the analysis window.

    Uses 1 AS on_platform to mark keyword membership based on existence
    in keyword_instances -- not on whether clicks > 0 in the window.
    This ensures keywords present in the account but with no traffic
    in the analysis window are still correctly classified as "on Google".

    COALESCE(SUM(...), 0) converts NULL (no kpd rows) to 0 so all
    performance columns are numeric and comparable.
    """
    query = """
        SELECT
            ki.identity_id,
            kd.normalized_text,
            kd.match_type,
            kd.identity_key,
            1                                        AS on_platform,
            COALESCE(SUM(kpd.clicks), 0)             AS clicks,
            COALESCE(SUM(kpd.impressions), 0)        AS impressions,
            COALESCE(SUM(kpd.cost_micros), 0) / 1e6  AS spend,
            COALESCE(SUM(kpd.primary_conv_count), 0) AS conversions,
            COALESCE(SUM(kpd.primary_conv_value), 0) AS revenue,
            COUNT(DISTINCT ki.campaign_name)          AS campaign_count,
            COUNT(DISTINCT ki.id)                     AS ad_group_count,
            GROUP_CONCAT(DISTINCT ki.status)          AS statuses
        FROM keyword_instances ki
        JOIN accounts a
            ON ki.account_id = a.id
        JOIN keyword_identities kd
            ON ki.identity_id = kd.id
        LEFT JOIN keyword_performance_daily kpd
            ON kpd.instance_id  = ki.id
            AND kpd.device      = 'ALL'
            AND kpd.geo_state   = 'ALL'
            AND kpd.report_date BETWEEN ? AND ?
        WHERE a.platform_account_id = ?
          AND a.platform            = 'google'
        GROUP BY ki.identity_id, kd.normalized_text, kd.match_type, kd.identity_key
    """
    df = pd.read_sql_query(
        query, conn,
        params=(START_DATE.isoformat(), END_DATE.isoformat(), GOOGLE_ACCOUNT_ID)
    )
    return df


def get_microsoft_keyword_performance(conn) -> pd.DataFrame:
    """
    Returns all Microsoft Home Insurance keyword identities with their
    aggregated performance over the analysis window.

    Same pattern as get_google_keyword_performance -- uses on_platform = 1
    to mark membership by existence, COALESCE to convert NULL to 0.
    """
    query = """
        SELECT
            ki.identity_id,
            kd.normalized_text,
            kd.match_type,
            kd.identity_key,
            1                                        AS on_platform,
            COALESCE(SUM(kpd.clicks), 0)             AS clicks,
            COALESCE(SUM(kpd.impressions), 0)        AS impressions,
            COALESCE(SUM(kpd.cost_micros), 0) / 1e6  AS spend,
            COALESCE(SUM(kpd.primary_conv_count), 0) AS conversions,
            COALESCE(SUM(kpd.primary_conv_value), 0) AS revenue,
            COUNT(DISTINCT ki.campaign_name)          AS campaign_count,
            COUNT(DISTINCT ki.id)                     AS ad_group_count,
            GROUP_CONCAT(DISTINCT ki.status)          AS statuses
        FROM keyword_instances ki
        JOIN accounts a
            ON ki.account_id = a.id
        JOIN keyword_identities kd
            ON ki.identity_id = kd.id
        LEFT JOIN keyword_performance_daily kpd
            ON kpd.instance_id  = ki.id
            AND kpd.device      = 'ALL'
            AND kpd.geo_state   = 'ALL'
            AND kpd.report_date BETWEEN ? AND ?
        WHERE a.platform_account_id = ?
          AND a.platform            = 'microsoft'
        GROUP BY ki.identity_id, kd.normalized_text, kd.match_type, kd.identity_key
    """
    df = pd.read_sql_query(
        query, conn,
        params=(START_DATE.isoformat(), END_DATE.isoformat(), MICROSOFT_ACCOUNT_ID)
    )
    return df


# ---------------------------------------------------------------------------
# March activity lookup
# ---------------------------------------------------------------------------

def get_march_activity(conn) -> tuple[set, set]:
    """
    Returns two sets of identity_ids that were active (impressions >= 1)
    in March 2026 -- one for Google, one for Microsoft.

    Used to stamp "Active in March" on the coverage gap sheets so readers
    can immediately distinguish live keywords from historical ones.
    March is the most current month available and reflects the account
    state as of the analysis date.
    """
    cursor = conn.cursor()

    def active_identities(platform_account_id: str, platform: str) -> set:
        rows = cursor.execute("""
            SELECT DISTINCT ki.identity_id
            FROM keyword_performance_daily kpd
            JOIN keyword_instances ki ON kpd.instance_id = ki.id
            JOIN accounts a ON ki.account_id = a.id
            WHERE a.platform_account_id = ?
              AND a.platform            = ?
              AND kpd.device            = 'ALL'
              AND kpd.geo_state         = 'ALL'
              AND kpd.report_date BETWEEN '2026-03-01' AND '2026-03-31'
              AND kpd.impressions       >= 1
        """, (platform_account_id, platform)).fetchall()
        return {r[0] for r in rows}

    google_active = active_identities(GOOGLE_ACCOUNT_ID, "google")
    ms_active     = active_identities(MICROSOFT_ACCOUNT_ID, "microsoft")
    return google_active, ms_active


# ---------------------------------------------------------------------------
# Coverage classification
# ---------------------------------------------------------------------------

def build_coverage_dataframes(
    google_df: pd.DataFrame,
    ms_df: pd.DataFrame,
    google_active_ids: set,
    ms_active_ids: set,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Joins Google and Microsoft keyword sets on identity_key and classifies
    each keyword into one of four coverage states.

    google_active_ids / ms_active_ids: sets of identity_ids that had
    at least one impression in March 2026 -- used to stamp "Active in March"
    on each output sheet so readers can distinguish live keywords from
    historical ones.

    Returns: (summary_df, google_only_df, ms_only_df, both_df)
    """
    # Suffix columns for merge
    google_df = google_df.add_suffix("_g").rename(
        columns={"identity_id_g": "identity_id",
                 "normalized_text_g": "keyword",
                 "match_type_g": "match_type",
                 "identity_key_g": "identity_key"}
    )
    ms_df = ms_df.add_suffix("_ms").rename(
        columns={"identity_id_ms": "identity_id_ms",
                 "normalized_text_ms": "keyword_ms",
                 "match_type_ms": "match_type_ms",
                 "identity_key_ms": "identity_key_ms"}
    )

    # Full outer join on identity_key
    merged = pd.merge(
        google_df,
        ms_df,
        left_on="identity_key",
        right_on="identity_key_ms",
        how="outer",
    )

    # Resolve keyword text and match type from whichever side has data
    merged["keyword"] = merged["keyword"].fillna(merged["keyword_ms"])
    merged["match_type"] = merged["match_type"].fillna(merged["match_type_ms"])
    merged["identity_key"] = merged["identity_key"].fillna(merged["identity_key_ms"])

    # Classify coverage
    # Use on_platform flags (1 = keyword exists on that platform) rather
    # than clicks to classify membership. This correctly handles keywords
    # that exist in the account but had zero traffic in the analysis window,
    # and keywords where SUM(clicks) is NULL due to no kpd rows.
    on_google    = merged["on_platform_g"].notna()
    on_microsoft = merged["on_platform_ms"].notna()

    google_only = merged[on_google & ~on_microsoft].copy()
    ms_only     = merged[~on_google & on_microsoft].copy()
    both        = merged[on_google & on_microsoft].copy()

    # ---------------------------------------------------------------------------
    # Google-only sheet
    # ---------------------------------------------------------------------------
    google_only_out = google_only[[
        "keyword", "match_type",
        "clicks_g", "impressions_g", "spend_g",
        "conversions_g", "revenue_g",
        "campaign_count_g", "ad_group_count_g", "statuses_g",
    ]].copy()
    google_only_out.columns = [
        "Keyword", "Match type",
        "Google clicks", "Google impressions", "Google spend ($)",
        "Google conversions", "Google revenue ($)",
        "Google campaigns", "Google ad groups", "Google statuses",
    ]
    # Fill NaN metrics with 0 (keywords with no activity in window)
    for col in ["Google clicks", "Google impressions", "Google spend ($)",
                "Google conversions", "Google revenue ($)"]:
        google_only_out[col] = google_only_out[col].fillna(0)

    # Active in March flag -- is this keyword currently generating traffic?
    google_only_out["Active in March (Google)"] = google_only[
        "identity_id"
    ].isin(google_active_ids).values
    google_only_out["Active in March (Google)"] = google_only_out[
        "Active in March (Google)"
    ].map({True: "Yes", False: "No"})

    # Priority flag -- high spend keywords missing from Microsoft
    google_only_out["Priority gap"] = google_only_out["Google spend ($)"].apply(
        lambda x: "HIGH" if x >= PRIORITY_SPEND_THRESHOLD else "low"
    )
    google_only_out = google_only_out.sort_values(
        ["Active in March (Google)", "Google spend ($)"],
        ascending=[False, False]
    ).reset_index(drop=True)

    # ---------------------------------------------------------------------------
    # Microsoft-only sheet
    # ---------------------------------------------------------------------------
    ms_only_out = ms_only[[
        "keyword", "match_type",
        "clicks_ms", "impressions_ms", "spend_ms",
        "conversions_ms", "revenue_ms",
        "campaign_count_ms", "ad_group_count_ms", "statuses_ms",
    ]].copy()
    ms_only_out.columns = [
        "Keyword", "Match type",
        "Microsoft clicks", "Microsoft impressions", "Microsoft spend ($)",
        "Microsoft conversions", "Microsoft revenue ($)",
        "Microsoft campaigns", "Microsoft ad groups", "Microsoft statuses",
    ]
    for col in ["Microsoft clicks", "Microsoft impressions", "Microsoft spend ($)",
                "Microsoft conversions", "Microsoft revenue ($)"]:
        ms_only_out[col] = ms_only_out[col].fillna(0)

    # Active in March flag
    ms_only_out["Active in March (Microsoft)"] = ms_only[
        "identity_id_ms"
    ].isin(ms_active_ids).values
    ms_only_out["Active in March (Microsoft)"] = ms_only_out[
        "Active in March (Microsoft)"
    ].map({True: "Yes", False: "No"})

    ms_only_out = ms_only_out.sort_values(
        ["Active in March (Microsoft)", "Microsoft spend ($)"],
        ascending=[False, False]
    ).reset_index(drop=True)

    # ---------------------------------------------------------------------------
    # Both platforms sheet
    # ---------------------------------------------------------------------------
    both_out = both[[
        "keyword", "match_type",
        "clicks_g", "spend_g", "conversions_g", "revenue_g",
        "clicks_ms", "spend_ms", "conversions_ms", "revenue_ms",
    ]].copy()
    both_out.columns = [
        "Keyword", "Match type",
        "Google clicks", "Google spend ($)",
        "Google offline convs", "Google offline revenue ($)",
        "Microsoft clicks", "Microsoft spend ($)",
        "Microsoft offline convs", "Microsoft offline revenue ($)",
    ]
    for col in both_out.columns[2:]:
        both_out[col] = both_out[col].fillna(0)

    # Add a spend ratio column -- Google spend / Microsoft spend
    # Shows where Google is spending heavily but Microsoft is not, and vice versa
    both_out["Google/MS spend ratio"] = both_out.apply(
        lambda r: round(r["Google spend ($)"] / r["Microsoft spend ($)"], 1)
        if r["Microsoft spend ($)"] > 0
        else None,
        axis=1,
    )

    # Google CPL and ROAS
    both_out["Google CPL ($)"] = both_out.apply(
        lambda r: round(r["Google spend ($)"] / r["Google offline convs"], 2)
        if r["Google offline convs"] > 0 else None,
        axis=1,
    )
    both_out["Google ROAS"] = both_out.apply(
        lambda r: round(r["Google offline revenue ($)"] / r["Google spend ($)"], 2)
        if r["Google spend ($)"] > 0 else None,
        axis=1,
    )
    # Microsoft CPL and ROAS
    both_out["Microsoft CPL ($)"] = both_out.apply(
        lambda r: round(r["Microsoft spend ($)"] / r["Microsoft offline convs"], 2)
        if r["Microsoft offline convs"] > 0 else None,
        axis=1,
    )
    both_out["Microsoft ROAS"] = both_out.apply(
        lambda r: round(r["Microsoft offline revenue ($)"] / r["Microsoft spend ($)"], 2)
        if r["Microsoft spend ($)"] > 0 else None,
        axis=1,
    )

    # Active in March flags for both platforms
    both_out["Active in March (Google)"] = both[
        "identity_id"
    ].isin(google_active_ids).values
    both_out["Active in March (Google)"] = both_out[
        "Active in March (Google)"
    ].map({True: "Yes", False: "No"})

    both_out["Active in March (Microsoft)"] = both[
        "identity_id"
    ].isin(ms_active_ids).values
    both_out["Active in March (Microsoft)"] = both_out[
        "Active in March (Microsoft)"
    ].map({True: "Yes", False: "No"})

    # Composite activity status -- most actionable combinations first
    def activity_status(g_active, ms_active):
        if g_active == "Yes" and ms_active == "Yes":
            return "Active on both"
        elif g_active == "Yes" and ms_active == "No":
            return "Google only — MS paused"
        elif g_active == "No" and ms_active == "Yes":
            return "Microsoft only — Google paused"
        else:
            return "Paused on both"

    both_out["Activity status"] = both_out.apply(
        lambda r: activity_status(
            r["Active in March (Google)"],
            r["Active in March (Microsoft)"]
        ), axis=1
    )

    both_out = both_out.sort_values(
        ["Activity status", "Google spend ($)"],
        ascending=[True, False]
    ).reset_index(drop=True)

    # ---------------------------------------------------------------------------
    # Summary sheet
    # ---------------------------------------------------------------------------
    g_total_keywords  = len(google_df)
    ms_total_keywords = len(ms_df)
    overlap_count     = len(both)
    g_only_count      = len(google_only)
    ms_only_count     = len(ms_only)

    g_only_spend  = google_only_out["Google spend ($)"].sum()
    ms_only_spend = ms_only_out["Microsoft spend ($)"].sum()
    both_g_spend  = both_out["Google spend ($)"].sum()
    both_ms_spend = both_out["Microsoft spend ($)"].sum()

    priority_gaps = (google_only_out["Priority gap"] == "HIGH").sum()

    g_only_convs  = google_only_out["Google conversions"].sum()
    ms_only_convs = ms_only_out["Microsoft conversions"].sum()
    both_g_convs  = both_out["Google offline convs"].sum()

    # Efficiency values are placeholders -- injected by main() after
    # the platform_efficiency() queries run against the database.
    # The sentinel string "TBD" is replaced in main() before writing Excel.
    summary_data = {
        "Metric": [
            "Analysis window",
            "",
            "--- Google Home Insurance ---",
            "Total keyword identities on Google",
            "Google-only keywords (not on Microsoft)",
            "Google-only spend in window ($)",
            "Google-only conversions in window",
            "Priority gaps (Google spend >= $10, not on Microsoft)",
            "",
            "--- Microsoft Home Insurance ---",
            "Total keyword identities on Microsoft",
            "Microsoft-only keywords (not on Google)",
            "Microsoft-only spend in window ($)",
            "Microsoft-only conversions in window",
            "",
            "--- Overlap ---",
            "Keywords on BOTH platforms",
            "Combined Google spend on shared keywords ($)",
            "Combined Microsoft spend on shared keywords ($)",
            "Combined Google offline conversions on shared keywords",
            "Combined Microsoft offline conversions on shared keywords",
            "Combined Google offline revenue on shared keywords ($)",
            "Combined Microsoft offline revenue on shared keywords ($)",
            "",
            "--- Efficiency (Jan 13 onward, fair revenue window) ---",
            "Google CPL (offline convs)",
            "Google ROAS (offline revenue / spend)",
            "Microsoft CPL (offline convs)",
            "Microsoft ROAS (offline revenue / spend)",
            "Note",
            "",
            "--- March activity status ---",
            "Google-only keywords active in March",
            "Google-only keywords paused/inactive in March",
            "Microsoft-only keywords active in March",
            "Microsoft-only keywords paused/inactive in March",
            "Both platform keywords: active on both",
            "Both platform keywords: Google active, MS paused",
            "Both platform keywords: MS active, Google paused",
            "Both platform keywords: paused on both",
        ],
        "Value": [
            f"{START_DATE.isoformat()} to {END_DATE.isoformat()} ({DAYS_BACK} days)",
            "",
            "",
            f"{g_total_keywords:,}",
            f"{g_only_count:,}",
            f"${g_only_spend:,.2f}",
            f"{g_only_convs:,.1f}",
            f"{priority_gaps:,}",
            "",
            "",
            f"{ms_total_keywords:,}",
            f"{ms_only_count:,}",
            f"${ms_only_spend:,.2f}",
            f"{ms_only_convs:,.1f}",
            "",
            "",
            f"{overlap_count:,}",
            f"${both_g_spend:,.2f}",
            f"${both_ms_spend:,.2f}",
            f"{both_g_convs:,.1f}",
            f"{both_out['Microsoft offline convs'].sum():,.1f}",
            f"${both_out['Google offline revenue ($)'].sum():,.2f}",
            f"${both_out['Microsoft offline revenue ($)'].sum():,.2f}",
            "",
            "",
            "TBD",   # Google CPL -- injected in main()
            "TBD",   # Google ROAS -- injected in main()
            "TBD",   # Microsoft CPL -- injected in main()
            "TBD",   # Microsoft ROAS -- injected in main()
            "TBD",   # Note -- injected in main()
            "",
            "",
            f"{(google_only_out['Active in March (Google)'] == 'Yes').sum():,}",
            f"{(google_only_out['Active in March (Google)'] == 'No').sum():,}",
            f"{(ms_only_out['Active in March (Microsoft)'] == 'Yes').sum():,}",
            f"{(ms_only_out['Active in March (Microsoft)'] == 'No').sum():,}",
            f"{(both_out['Activity status'] == 'Active on both').sum():,}",
            f"{(both_out['Activity status'] == 'Google only — MS paused').sum():,}",
            f"{(both_out['Activity status'] == 'Microsoft only — Google paused').sum():,}",
            f"{(both_out['Activity status'] == 'Paused on both').sum():,}",
        ]
    }
    summary_df = pd.DataFrame(summary_data)

    # ---------------------------------------------------------------------------
    # Text overlap sheet (any match type)
    # Keywords where the normalized TEXT exists on both platforms regardless
    # of match type -- answers "is this search intent covered on both?"
    # ---------------------------------------------------------------------------

    # Get unique normalized texts per platform
    google_texts = set(
        google_df["keyword"].str.lower().str.strip()
    )
    ms_texts = set(
        ms_df["keyword_ms"].str.lower().str.strip()
    )
    text_overlap_set = google_texts & ms_texts

    # Build text overlap output -- one row per unique keyword text
    # showing best performance from each platform
    g_text_perf = (
        google_df[google_df["keyword"].str.lower().str.strip().isin(text_overlap_set)]
        .groupby(google_df["keyword"].str.lower().str.strip(), as_index=False)
        .agg(
            keyword=("keyword", "first"),
            g_match_types=("match_type", lambda x: ", ".join(sorted(set(x)))),
            g_clicks=("clicks_g", "sum"),
            g_spend=("spend_g", "sum"),
            g_conversions=("conversions_g", "sum"),
            g_revenue=("revenue_g", "sum"),
        )
    )
    ms_text_perf = (
        ms_df[ms_df["keyword_ms"].str.lower().str.strip().isin(text_overlap_set)]
        .groupby(ms_df["keyword_ms"].str.lower().str.strip(), as_index=False)
        .agg(
            keyword_ms=("keyword_ms", "first"),
            ms_match_types=("match_type_ms", lambda x: ", ".join(sorted(set(x)))),
            ms_clicks=("clicks_ms", "sum"),
            ms_spend=("spend_ms", "sum"),
            ms_conversions=("conversions_ms", "sum"),
            ms_revenue=("revenue_ms", "sum"),
        )
    )

    text_overlap_out = pd.merge(
        g_text_perf, ms_text_perf,
        left_on="keyword", right_on="keyword_ms",
        how="inner"
    )[[
        "keyword",
        "g_match_types", "g_clicks", "g_spend", "g_conversions", "g_revenue",
        "ms_match_types", "ms_clicks", "ms_spend", "ms_conversions", "ms_revenue",
    ]].copy()
    text_overlap_out.columns = [
        "Keyword",
        "Google match types", "Google clicks", "Google spend ($)",
        "Google offline convs", "Google offline revenue ($)",
        "Microsoft match types", "Microsoft clicks", "Microsoft spend ($)",
        "Microsoft offline convs", "Microsoft offline revenue ($)",
    ]
    for col in text_overlap_out.columns[2:]:
        if "match types" not in col:
            text_overlap_out[col] = text_overlap_out[col].fillna(0)
    text_overlap_out["Google/MS spend ratio"] = text_overlap_out.apply(
        lambda r: round(r["Google spend ($)"] / r["Microsoft spend ($)"], 1)
        if r["Microsoft spend ($)"] > 0 else None,
        axis=1,
    )
    text_overlap_out = text_overlap_out.sort_values(
        "Google spend ($)", ascending=False
    ).reset_index(drop=True)

    # ---------------------------------------------------------------------------
    # Match type breakdown for summary
    # ---------------------------------------------------------------------------
    def match_type_summary(df, spend_col, conv_col, match_col, platform_label):
        """Returns list of (metric, value) tuples for match type breakdown."""
        rows = []
        for mt in ["EXACT", "PHRASE", "BROAD"]:
            mask = df[match_col] == mt
            spend = df.loc[mask, spend_col].sum()
            convs = df.loc[mask, conv_col].sum()
            kws   = mask.sum()
            if kws > 0:
                rows.append((
                    f"  {platform_label} {mt} -- keywords",
                    f"{kws:,}"
                ))
                rows.append((
                    f"  {platform_label} {mt} -- spend ($)",
                    f"${spend:,.2f}"
                ))
                rows.append((
                    f"  {platform_label} {mt} -- conversions",
                    f"{convs:,.1f}"
                ))
        return rows

    g_mt_rows = match_type_summary(
        google_df, "spend_g", "conversions_g", "match_type", "Google"
    )
    ms_mt_rows = match_type_summary(
        ms_df, "spend_ms", "conversions_ms", "match_type_ms", "Microsoft"
    )

    # Extend summary with match type sections
    summary_data["Metric"] += [
        "",
        "--- Google spend & conversions by match type ---",
    ] + [r[0] for r in g_mt_rows] + [
        "",
        "--- Microsoft spend & conversions by match type ---",
    ] + [r[0] for r in ms_mt_rows] + [
        "",
        "--- Text overlap (any match type) ---",
        "Keyword texts on both platforms (any match type)",
        "Google-only keyword texts (no MS equivalent)",
        "Microsoft-only keyword texts (no Google equivalent)",
    ]
    summary_data["Value"] += [
        "",
        "",
    ] + [r[1] for r in g_mt_rows] + [
        "",
        "",
    ] + [r[1] for r in ms_mt_rows] + [
        "",
        "",
        f"{len(text_overlap_set):,}",
        f"{len(google_texts - ms_texts):,}",
        f"{len(ms_texts - google_texts):,}",
    ]

    summary_df = pd.DataFrame(summary_data)

    return summary_df, google_only_out, ms_only_out, both_out, text_overlap_out


# ---------------------------------------------------------------------------
# Excel formatting
# ---------------------------------------------------------------------------

def format_workbook(path: Path):
    """
    Applies consistent formatting to the output workbook:
    - Bold header row with colored fill per sheet
    - Auto-fitted column widths
    - Number formatting on metric columns
    - Highlights HIGH priority gap rows in orange
    """
    wb = load_workbook(path)

    # Color scheme per sheet
    header_fills = {
        "Summary":                  "1F4E79",   # dark blue
        "Google only":              "375623",   # dark green
        "Microsoft only":           "7B3F00",   # dark brown
        "Both platforms":           "4A235A",   # dark purple
        "Text overlap (any match)": "7B3F00",   # teal
    }
    white_font = Font(color="FFFFFF", bold=True, size=11)
    header_font = Font(bold=True, size=11)
    priority_fill = PatternFill("solid", fgColor="FCE4D6")  # light orange

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        fill_hex = header_fills.get(sheet_name, "1F4E79")
        header_fill = PatternFill("solid", fgColor=fill_hex)

        # Format header row
        for cell in ws[1]:
            cell.font = white_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

        ws.row_dimensions[1].height = 30

        # Auto-fit column widths
        for col in ws.columns:
            max_len = 0
            col_letter = get_column_letter(col[0].column)
            for cell in col:
                try:
                    val_len = len(str(cell.value)) if cell.value is not None else 0
                    max_len = max(max_len, val_len)
                except Exception:
                    pass
            ws.column_dimensions[col_letter].width = min(max_len + 4, 40)

        # Number formatting on spend / revenue columns
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                if cell.column_letter != "A" and isinstance(cell.value, float):
                    if "spend" in str(ws.cell(1, cell.column).value or "").lower() or \
                       "revenue" in str(ws.cell(1, cell.column).value or "").lower():
                        cell.number_format = '$#,##0.00'
                    elif "ratio" in str(ws.cell(1, cell.column).value or "").lower():
                        cell.number_format = '0.0'
                    else:
                        cell.number_format = '#,##0'

        # Highlight HIGH priority rows on Google only sheet
        if sheet_name == "Google only":
            priority_col = None
            for cell in ws[1]:
                if cell.value == "Priority gap":
                    priority_col = cell.column
                    break
            if priority_col:
                for row in ws.iter_rows(min_row=2):
                    if row[priority_col - 1].value == "HIGH":
                        for cell in row:
                            cell.fill = priority_fill

        # Highlight active/inactive status columns
        active_cols = []
        for cell in ws[1]:
            val = str(cell.value or "")
            if "Active in March" in val or val == "Activity status":
                active_cols.append(cell.column)

        if active_cols:
            active_yes_fill  = PatternFill("solid", fgColor="E2EFDA")  # light green
            active_no_fill   = PatternFill("solid", fgColor="FCE4D6")  # light orange
            both_fill        = PatternFill("solid", fgColor="E2EFDA")  # green
            g_only_fill      = PatternFill("solid", fgColor="FFF2CC")  # yellow
            ms_only_fill     = PatternFill("solid", fgColor="DEEAF1")  # blue
            paused_both_fill = PatternFill("solid", fgColor="F2F2F2")  # gray

            for row in ws.iter_rows(min_row=2):
                for col_idx in active_cols:
                    cell = row[col_idx - 1]
                    val  = str(cell.value or "")
                    if val == "Yes":
                        cell.fill = active_yes_fill
                    elif val == "No":
                        cell.fill = active_no_fill
                    elif val == "Active on both":
                        cell.fill = both_fill
                    elif val == "Google only — MS paused":
                        cell.fill = g_only_fill
                    elif val == "Microsoft only — Google paused":
                        cell.fill = ms_only_fill
                    elif val == "Paused on both":
                        cell.fill = paused_both_fill

        # Freeze header row
        ws.freeze_panes = "A2"

    wb.save(path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("Coverage Gap Analysis — Home Insurance")
    print(f"Window : {START_DATE} to {END_DATE} ({DAYS_BACK} days)")
    print(f"Google : {GOOGLE_ACCOUNT_ID}")
    print(f"Microsoft: {MICROSOFT_ACCOUNT_ID}")
    print("=" * 60)

    conn = get_conn()

    print("\nFetching Google keyword performance...")
    google_df = get_google_keyword_performance(conn)
    print(f"  {len(google_df):,} keyword identities on Google")

    print("Fetching Microsoft keyword performance...")
    ms_df = get_microsoft_keyword_performance(conn)
    print(f"  {len(ms_df):,} keyword identities on Microsoft")

    print("\nFetching March activity status...")
    google_active_ids, ms_active_ids = get_march_activity(conn)
    print(f"  Google identities active in March  : {len(google_active_ids):,}")
    print(f"  Microsoft identities active in March: {len(ms_active_ids):,}")

    print("\nBuilding coverage classification...")
    summary_df, google_only_df, ms_only_df, both_df, text_overlap_df = build_coverage_dataframes(
        google_df, ms_df, google_active_ids, ms_active_ids
    )

    # ------------------------------------------------------------------
    # Compute cross-platform efficiency metrics using the fair revenue
    # window (Jan 13 onward -- when Microsoft offline uploads began)
    # ------------------------------------------------------------------
    cursor = conn.cursor()

    def platform_efficiency(platform_account_id, platform):
        row = cursor.execute("""
            SELECT COALESCE(SUM(kpd.primary_conv_count), 0)  AS convs,
                   COALESCE(SUM(kpd.primary_conv_value), 0)  AS revenue,
                   COALESCE(SUM(kpd.cost_micros), 0) / 1e6   AS spend
            FROM keyword_performance_daily kpd
            JOIN keyword_instances ki ON kpd.instance_id = ki.id
            JOIN accounts a ON ki.account_id = a.id
            WHERE a.platform_account_id = ?
              AND a.platform            = ?
              AND kpd.device            = 'ALL'
              AND kpd.geo_state         = 'ALL'
              AND kpd.report_date       >= ?
        """, (platform_account_id, platform,
              REVENUE_START_DATE.isoformat())).fetchone()
        convs   = row["convs"]
        revenue = row["revenue"]
        spend   = row["spend"]
        cpl     = round(spend / convs, 2) if convs > 0 else None
        roas    = round(revenue / spend, 2) if spend > 0 else None
        return convs, revenue, spend, cpl, roas

    g_convs, g_rev, g_spend, g_cpl, g_roas = platform_efficiency(
        GOOGLE_ACCOUNT_ID, "google"
    )
    m_convs, m_rev, m_spend, m_cpl, m_roas = platform_efficiency(
        MICROSOFT_ACCOUNT_ID, "microsoft"
    )

    # Inject efficiency values into summary_df
    # Find the placeholder rows we added and replace their values
    eff_values = {
        "Google CPL (offline convs)":            f"${g_cpl:,.2f}" if g_cpl else "N/A",
        "Google ROAS (offline revenue / spend)":  f"{g_roas:.2f}x" if g_roas else "N/A",
        "Microsoft CPL (offline convs)":          f"${m_cpl:,.2f}" if m_cpl else "N/A",
        "Microsoft ROAS (offline revenue / spend)": f"{m_roas:.2f}x" if m_roas else "N/A",
        "Note": f"Revenue window: {REVENUE_START_DATE.isoformat()} to {END_DATE.isoformat()} "
                f"(Microsoft offline uploads began {REVENUE_START_DATE.isoformat()})",
    }
    for metric, value in eff_values.items():
        mask = summary_df["Metric"].str.strip() == metric
        if mask.any():
            summary_df.loc[mask, "Value"] = value

    print(f"\n--- Cross-platform efficiency ({REVENUE_START_DATE} to {END_DATE}) ---")
    print(f"  Google    CPL=${g_cpl:.2f}  ROAS={g_roas:.2f}x  "
          f"spend=${g_spend:,.0f}  revenue=${g_rev:,.0f}")
    print(f"  Microsoft CPL=${m_cpl:.2f}  ROAS={m_roas:.2f}x  "
          f"spend=${m_spend:,.0f}  revenue=${m_rev:,.0f}")

    # Print terminal summary
    print("\n--- Coverage Summary ---")
    for _, row in summary_df.iterrows():
        if row["Metric"] and row["Value"]:
            print(f"  {row['Metric']:<55} {row['Value']}")

    conn.close()

    # Write to Excel
    print(f"\nWriting Excel output...")
    with pd.ExcelWriter(OUTPUT_FILE, engine="openpyxl") as writer:
        summary_df.to_excel(writer, sheet_name="Summary", index=False)
        google_only_df.to_excel(writer, sheet_name="Google only", index=False)
        ms_only_df.to_excel(writer, sheet_name="Microsoft only", index=False)
        both_df.to_excel(writer, sheet_name="Both platforms", index=False)
        text_overlap_df.to_excel(writer, sheet_name="Text overlap (any match)", index=False)

    print("Applying formatting...")
    format_workbook(OUTPUT_FILE)

    size_kb = os.path.getsize(OUTPUT_FILE) / 1024
    print(f"\nOutput saved: {OUTPUT_FILE}")
    print(f"File size   : {size_kb:.1f} KB")
    print("\nDone.")


if __name__ == "__main__":
    main()
