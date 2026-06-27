"""
analysis_activity_trend.py
--------------------------
Answers: "What has the team been actively running, and how is that
changing month over month from January through March 2026?"

Defines "active" as a keyword that generated at least one impression
in a given month -- not simply ENABLED status, which can be misleading
when bids are too low or budgets are throttled.

Produces an Excel workbook with five sheets:
    1. Summary              -- headline numbers across all four analyses
    2. Monthly active       -- active keyword counts by platform + match type
    3. Keyword lifecycle    -- each keyword classified into one of four states
    4. Spend migration      -- spend and conversions lost from paused keywords
    5. Platform comparison  -- Google vs Microsoft side-by-side by month

All metrics use device='ALL' and geo_state='ALL' rows to avoid
double-counting across device segments.

Usage:
    conda activate paid-search
    python analysis/analysis_activity_trend.py

Output:
    analysis/output/activity_trend_home_insurance.xlsx
"""

import sqlite3
import os
import pandas as pd
from pathlib import Path
from datetime import date
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PATH    = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
OUTPUT_DIR = Path(r"C:\Projects\google-ads-ops\analysis\output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_FILE = OUTPUT_DIR / "activity_trend_home_insurance.xlsx"

# Analysis months -- the three months the team has been actively managing
MONTHS = ["2026-01", "2026-02", "2026-03"]
MONTH_LABELS = {
    "2026-01": "January 2026",
    "2026-02": "February 2026",
    "2026-03": "March 2026",
}

# Date boundaries for each month
MONTH_BOUNDS = {
    "2026-01": ("2026-01-01", "2026-01-31"),
    "2026-02": ("2026-02-01", "2026-02-28"),
    "2026-03": ("2026-03-01", "2026-03-24"),   # partial month
}

GOOGLE_ACCOUNT_ID    = "8886502147"
MICROSOFT_ACCOUNT_ID = "F149SN63"

# Minimum impressions to be considered "active" in a month
# 1 impression = the keyword entered at least one auction
ACTIVE_IMPRESSION_THRESHOLD = 1


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Core data fetch
# ---------------------------------------------------------------------------

def fetch_keyword_monthly_activity(conn, platform_account_id: str,
                                   platform: str) -> pd.DataFrame:
    """
    Returns one row per keyword instance per month showing whether the
    keyword was active (impressions >= threshold) and its performance.

    Columns: criterion_id, identity_id, keyword_text, match_type,
             campaign_name, status, month, impressions, clicks,
             spend, conversions, revenue, is_active
    """
    rows = []
    cursor = conn.cursor()

    # Get account_id
    acct = cursor.execute(
        "SELECT id FROM accounts WHERE platform_account_id = ? AND platform = ?",
        (platform_account_id, platform)
    ).fetchone()
    if not acct:
        return pd.DataFrame()
    account_id = acct["id"]

    for month in MONTHS:
        start, end = MONTH_BOUNDS[month]
        results = cursor.execute("""
            SELECT
                ki.id            AS instance_id,
                ki.criterion_id,
                ki.identity_id,
                ki.keyword_text,
                ki.match_type,
                ki.campaign_name,
                ki.status,
                kd.normalized_text,
                COALESCE(SUM(kpd.impressions), 0)        AS impressions,
                COALESCE(SUM(kpd.clicks), 0)             AS clicks,
                COALESCE(SUM(kpd.cost_micros), 0) / 1e6  AS spend,
                COALESCE(SUM(kpd.primary_conv_count), 0) AS conversions,
                COALESCE(SUM(kpd.primary_conv_value), 0) AS revenue
            FROM keyword_instances ki
            JOIN accounts a ON ki.account_id = a.id
            JOIN keyword_identities kd ON ki.identity_id = kd.id
            LEFT JOIN keyword_performance_daily kpd
                ON kpd.instance_id  = ki.id
                AND kpd.device      = 'ALL'
                AND kpd.geo_state   = 'ALL'
                AND kpd.report_date BETWEEN ? AND ?
            WHERE a.id = ?
            GROUP BY ki.id
        """, (start, end, account_id)).fetchall()

        for r in results:
            rows.append({
                "platform":       platform,
                "month":          month,
                "month_label":    MONTH_LABELS[month],
                "instance_id":    r["instance_id"],
                "criterion_id":   r["criterion_id"],
                "identity_id":    r["identity_id"],
                "keyword_text":   r["keyword_text"],
                "normalized_text": r["normalized_text"],
                "match_type":     r["match_type"],
                "campaign_name":  r["campaign_name"],
                "status":         r["status"],
                "impressions":    r["impressions"],
                "clicks":         r["clicks"],
                "spend":          r["spend"],
                "conversions":    r["conversions"],
                "revenue":        r["revenue"],
                "is_active":      r["impressions"] >= ACTIVE_IMPRESSION_THRESHOLD,
            })

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Analysis 1: Monthly active keyword counts
# ---------------------------------------------------------------------------

def build_monthly_active(google_df: pd.DataFrame,
                         ms_df: pd.DataFrame) -> pd.DataFrame:
    """
    Counts active keywords by month, platform, and match type.
    Also shows total keywords in account regardless of activity.
    """
    rows = []
    for platform, df in [("Google", google_df), ("Microsoft", ms_df)]:
        for month in MONTHS:
            mdf = df[df["month"] == month]
            total     = len(mdf)
            active    = mdf["is_active"].sum()
            inactive  = total - active
            pct       = active / total * 100 if total > 0 else 0

            # Match type breakdown of active keywords
            for mt in ["EXACT", "PHRASE", "BROAD"]:
                mt_mask = mdf["match_type"] == mt
                mt_active = (mdf[mt_mask]["is_active"]).sum()
                mt_spend  = mdf[mt_mask & mdf["is_active"]]["spend"].sum()
                mt_convs  = mdf[mt_mask & mdf["is_active"]]["conversions"].sum()
                if mt_mask.sum() > 0:
                    rows.append({
                        "Platform":         platform,
                        "Month":            MONTH_LABELS[month],
                        "Match type":       mt,
                        "Total keywords":   int(mt_mask.sum()),
                        "Active keywords":  int(mt_active),
                        "Inactive keywords": int(mt_mask.sum() - mt_active),
                        "Active %":         round(mt_active / mt_mask.sum() * 100, 1)
                                            if mt_mask.sum() > 0 else 0,
                        "Active spend ($)": round(mt_spend, 2),
                        "Active conversions": round(mt_convs, 1),
                    })

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Analysis 2: Keyword lifecycle classification
# ---------------------------------------------------------------------------

def build_lifecycle(google_df: pd.DataFrame,
                    ms_df: pd.DataFrame) -> pd.DataFrame:
    """
    Classifies each keyword into one of four lifecycle states based on
    which months it was active (had impressions >= threshold).

    States:
        Always active    -- impressions in all 3 months
        Declining        -- impressions in Jan + Feb, not Mar
        Newly paused     -- impressions in Jan only
        Late paused      -- impressions in Jan + Feb, gone in Mar
        New in period    -- no Jan impressions, active in Feb or Mar
        Intermittent     -- active in some but not consecutive months
        Never active     -- zero impressions across all 3 months
    """
    rows = []
    for platform, df in [("Google", google_df), ("Microsoft", ms_df)]:
        # Pivot: one row per keyword, columns = month activity flags
        pivot = df.pivot_table(
            index=["criterion_id", "keyword_text", "match_type",
                   "campaign_name", "status"],
            columns="month",
            values=["is_active", "impressions", "spend", "conversions"],
            aggfunc="first"
        ).reset_index()

        # Flatten column names
        pivot.columns = [
            "_".join(str(c) for c in col).strip("_")
            if isinstance(col, tuple) else col
            for col in pivot.columns
        ]

        for _, row in pivot.iterrows():
            jan = bool(row.get("is_active_2026-01", False))
            feb = bool(row.get("is_active_2026-02", False))
            mar = bool(row.get("is_active_2026-03", False))

            if jan and feb and mar:
                lifecycle = "Always active"
            elif jan and feb and not mar:
                lifecycle = "Paused in March"
            elif jan and not feb and not mar:
                lifecycle = "Paused in February"
            elif jan and not feb and mar:
                lifecycle = "Intermittent"
            elif not jan and feb and mar:
                lifecycle = "New in February"
            elif not jan and not feb and mar:
                lifecycle = "New in March"
            elif not jan and feb and not mar:
                lifecycle = "New then paused"
            else:
                lifecycle = "Never active"

            jan_spend = row.get("spend_2026-01", 0) or 0
            feb_spend = row.get("spend_2026-02", 0) or 0
            mar_spend = row.get("spend_2026-03", 0) or 0
            jan_convs = row.get("conversions_2026-01", 0) or 0
            feb_convs = row.get("conversions_2026-02", 0) or 0
            mar_convs = row.get("conversions_2026-03", 0) or 0
            jan_imps  = row.get("impressions_2026-01", 0) or 0
            feb_imps  = row.get("impressions_2026-02", 0) or 0
            mar_imps  = row.get("impressions_2026-03", 0) or 0

            rows.append({
                "Platform":          platform,
                "Lifecycle":         lifecycle,
                "Keyword":           row["keyword_text"],
                "Match type":        row["match_type"],
                "Campaign":          row["campaign_name"],
                "Current status":    row["status"],
                "Jan active":        "Yes" if jan else "No",
                "Feb active":        "Yes" if feb else "No",
                "Mar active":        "Yes" if mar else "No",
                "Jan impressions":   int(jan_imps),
                "Feb impressions":   int(feb_imps),
                "Mar impressions":   int(mar_imps),
                "Jan spend ($)":     round(jan_spend, 2),
                "Feb spend ($)":     round(feb_spend, 2),
                "Mar spend ($)":     round(mar_spend, 2),
                "Jan conversions":   round(jan_convs, 1),
                "Feb conversions":   round(feb_convs, 1),
                "Mar conversions":   round(mar_convs, 1),
                "Total spend ($)":   round(jan_spend + feb_spend + mar_spend, 2),
                "Total conversions": round(jan_convs + feb_convs + mar_convs, 1),
            })

    df_out = pd.DataFrame(rows)
    # Sort: show paused keywords with highest prior spend first
    df_out = df_out.sort_values(
        ["Platform", "Lifecycle", "Total spend ($)"],
        ascending=[True, True, False]
    ).reset_index(drop=True)
    return df_out


# ---------------------------------------------------------------------------
# Analysis 3: Spend migration
# ---------------------------------------------------------------------------

def build_spend_migration(lifecycle_df: pd.DataFrame) -> pd.DataFrame:
    """
    Focuses on keywords that were active in January but became inactive
    by March -- the 'Paused in February', 'Paused in March', and
    'Paused in February' states. Shows what spend and conversions the
    team cut, and whether those cuts look like good optimization
    (low converting keywords) or over-cutting (high converting keywords).
    """
    paused_states = {
        "Paused in February", "Paused in March", "New then paused"
    }
    paused = lifecycle_df[
        lifecycle_df["Lifecycle"].isin(paused_states)
    ].copy()

    if paused.empty:
        return pd.DataFrame(columns=[
            "Platform", "Keyword", "Match type", "Lifecycle",
            "Last active month", "Prior spend ($)", "Prior conversions",
            "CPL ($)", "Assessment"
        ])

    # Add assessment column: was this a good cut or risky cut?
    def assess_cut(row):
        spend  = row["Total spend ($)"]
        convs  = row["Total conversions"]
        if spend == 0:
            return "No spend — clean cut"
        cpl = spend / convs if convs > 0 else float("inf")
        if convs == 0:
            return "Zero conversions — good cut"
        elif cpl > 200:
            return "High CPL — good cut"
        elif cpl > 50:
            return "Moderate CPL — review"
        else:
            return "Low CPL — risky cut"

    paused["CPL ($)"] = paused.apply(
        lambda r: round(r["Total spend ($)"] / r["Total conversions"], 2)
        if r["Total conversions"] > 0 else None,
        axis=1
    )
    paused["Assessment"] = paused.apply(assess_cut, axis=1)

    out = paused[[
        "Platform", "Keyword", "Match type", "Lifecycle",
        "Jan spend ($)", "Feb spend ($)", "Mar spend ($)",
        "Jan conversions", "Feb conversions", "Mar conversions",
        "Total spend ($)", "Total conversions", "CPL ($)", "Assessment",
        "Campaign", "Current status",
    ]].copy()

    return out.sort_values(
        ["Platform", "Assessment", "Total spend ($)"],
        ascending=[True, True, False]
    ).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Analysis 4: Platform comparison
# ---------------------------------------------------------------------------

def build_platform_comparison(google_df: pd.DataFrame,
                               ms_df: pd.DataFrame) -> pd.DataFrame:
    """
    Side-by-side Google vs Microsoft summary by month.
    Shows the rate of contraction or expansion on each platform.
    """
    rows = []
    for month in MONTHS:
        g_month = google_df[google_df["month"] == month]
        m_month = ms_df[ms_df["month"] == month]

        g_total   = len(g_month)
        g_active  = g_month["is_active"].sum()
        g_spend   = g_month[g_month["is_active"]]["spend"].sum()
        g_clicks  = g_month[g_month["is_active"]]["clicks"].sum()
        g_convs   = g_month[g_month["is_active"]]["conversions"].sum()
        g_revenue = g_month[g_month["is_active"]]["revenue"].sum()
        g_imps    = g_month[g_month["is_active"]]["impressions"].sum()

        m_total   = len(m_month)
        m_active  = m_month["is_active"].sum()
        m_spend   = m_month[m_month["is_active"]]["spend"].sum()
        m_clicks  = m_month[m_month["is_active"]]["clicks"].sum()
        m_convs   = m_month[m_month["is_active"]]["conversions"].sum()
        m_revenue = m_month[m_month["is_active"]]["revenue"].sum()
        m_imps    = m_month[m_month["is_active"]]["impressions"].sum()

        g_cpl  = round(g_spend / g_convs, 2) if g_convs > 0 else None
        m_cpl  = round(m_spend / m_convs, 2) if m_convs > 0 else None
        g_cpc  = round(g_spend / g_clicks, 2) if g_clicks > 0 else None
        m_cpc  = round(m_spend / m_clicks, 2) if m_clicks > 0 else None
        g_roas = round(g_revenue / g_spend, 2) if g_spend > 0 and g_revenue > 0 else None
        m_roas = round(m_revenue / m_spend, 2) if m_spend > 0 and m_revenue > 0 else None

        rows.append({
            "Month":                          MONTH_LABELS[month],
            "Google total keywords":          int(g_total),
            "Google active keywords":         int(g_active),
            "Google active %":                round(g_active / g_total * 100, 1) if g_total > 0 else 0,
            "Google impressions":             int(g_imps),
            "Google clicks":                  int(g_clicks),
            "Google spend ($)":               round(g_spend, 2),
            "Google CPC ($)":                 g_cpc,
            "Google offline convs":           round(g_convs, 1),
            "Google CPL ($)":                 g_cpl,
            "Google offline revenue ($)":     round(g_revenue, 2),
            "Google ROAS":                    g_roas,
            "Microsoft total keywords":       int(m_total),
            "Microsoft active keywords":      int(m_active),
            "Microsoft active %":             round(m_active / m_total * 100, 1) if m_total > 0 else 0,
            "Microsoft impressions":          int(m_imps),
            "Microsoft clicks":               int(m_clicks),
            "Microsoft spend ($)":            round(m_spend, 2),
            "Microsoft CPC ($)":              m_cpc,
            "Microsoft offline convs":        round(m_convs, 1),
            "Microsoft CPL ($)":              m_cpl,
            "Microsoft offline revenue ($)":  round(m_revenue, 2),
            "Microsoft ROAS":                 m_roas,
        })

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Summary sheet
# ---------------------------------------------------------------------------

def build_summary(lifecycle_df: pd.DataFrame,
                  platform_df: pd.DataFrame,
                  migration_df: pd.DataFrame) -> pd.DataFrame:
    """Builds the executive summary sheet."""

    rows = {"Metric": [], "Value": []}

    def add(metric, value):
        rows["Metric"].append(metric)
        rows["Value"].append(value)

    def blank():
        add("", "")

    add("Analysis window", "January 2026 — March 2026 (YTD)")
    add("Active = impressions >= 1 in that month", "")
    blank()

    for platform in ["Google", "Microsoft"]:
        add(f"--- {platform} ---", "")
        ldf = lifecycle_df[lifecycle_df["Platform"] == platform]
        for state, label in [
            ("Always active",    "Always active (all 3 months)"),
            ("Paused in March",  "Paused in March"),
            ("Paused in February", "Paused in February"),
            ("New in February",  "New in February"),
            ("New in March",     "New in March"),
            ("Intermittent",     "Intermittent (skipped a month)"),
            ("Never active",     "Never active (0 impressions, all months)"),
        ]:
            mask = ldf["Lifecycle"] == state
            count = mask.sum()
            spend = ldf[mask]["Total spend ($)"].sum()
            convs = ldf[mask]["Total conversions"].sum()
            if count > 0:
                add(f"  {label} — keywords", f"{count:,}")
                add(f"  {label} — spend ($)", f"${spend:,.2f}")
                if convs > 0:
                    add(f"  {label} — conversions", f"{convs:,.1f}")
        blank()

    add("--- Spend migration (paused keywords) ---", "")
    for platform in ["Google", "Microsoft"]:
        mdf = migration_df[migration_df["Platform"] == platform]
        if not mdf.empty:
            risky = (mdf["Assessment"] == "Low CPL — risky cut").sum()
            good  = mdf["Assessment"].str.contains("good cut|clean cut").sum()
            total_spend = mdf["Total spend ($)"].sum()
            total_convs = mdf["Total conversions"].sum()
            add(f"  {platform} paused keywords", f"{len(mdf):,}")
            add(f"  {platform} spend removed ($)", f"${total_spend:,.2f}")
            add(f"  {platform} conversions removed", f"{total_convs:,.1f}")
            add(f"  {platform} risky cuts (low CPL, paused)", f"{risky:,}")
            add(f"  {platform} clean cuts (zero/high CPL)", f"{good:,}")
        blank()

    add("--- Platform comparison (March vs January) ---", "")
    if len(platform_df) >= 2:
        jan = platform_df.iloc[0]
        mar = platform_df.iloc[-1]
        for platform in ["Google", "Microsoft"]:
            j_act = jan[f"{platform} active keywords"]
            m_act = mar[f"{platform} active keywords"]
            change = m_act - j_act
            pct    = change / j_act * 100 if j_act > 0 else 0
            sign   = "+" if change >= 0 else ""
            add(f"  {platform} active keyword change Jan→Mar",
                f"{sign}{change:,} ({sign}{pct:.1f}%)")
            j_sp = jan[f"{platform} spend ($)"]
            m_sp = mar[f"{platform} spend ($)"]
            sp_change = m_sp - j_sp
            sign = "+" if sp_change >= 0 else ""
            add(f"  {platform} spend change Jan→Mar ($)",
                f"{sign}${sp_change:,.2f}")

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Excel formatting
# ---------------------------------------------------------------------------

def format_workbook(path: Path):
    wb = load_workbook(path)

    header_fills = {
        "Summary":            "1F4E79",
        "Monthly active":     "375623",
        "Keyword lifecycle":  "4A235A",
        "Spend migration":    "7B3F00",
        "Platform comparison": "1F4E79",
    }

    # Lifecycle state colors for the keyword lifecycle sheet
    lifecycle_colors = {
        "Always active":     "E2EFDA",   # light green
        "Paused in March":   "FFF2CC",   # light yellow
        "Paused in February": "FCE4D6",  # light orange
        "Never active":      "F2F2F2",   # light gray
        "New in February":   "DEEAF1",   # light blue
        "New in March":      "DEEAF1",   # light blue
        "New then paused":   "FCE4D6",   # light orange
        "Intermittent":      "FFF2CC",   # light yellow
    }

    white_font = Font(color="FFFFFF", bold=True, size=11)

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        fill_hex = header_fills.get(sheet_name, "1F4E79")
        header_fill = PatternFill("solid", fgColor=fill_hex)

        for cell in ws[1]:
            cell.font = white_font
            cell.fill = header_fill
            cell.alignment = Alignment(
                horizontal="center", vertical="center", wrap_text=True
            )
        ws.row_dimensions[1].height = 32

        # Auto-fit columns
        for col in ws.columns:
            max_len = 0
            col_letter = get_column_letter(col[0].column)
            for cell in col:
                try:
                    val_len = len(str(cell.value)) if cell.value is not None else 0
                    max_len = max(max_len, val_len)
                except Exception:
                    pass
            ws.column_dimensions[col_letter].width = min(max_len + 4, 42)

        # Number formatting
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                header = str(ws.cell(1, cell.column).value or "").lower()
                if isinstance(cell.value, float):
                    if "spend" in header or "revenue" in header or                        "cpl" in header or "cpc" in header:
                        cell.number_format = '$#,##0.00'
                    elif "roas" in header:
                        cell.number_format = '0.00'
                    elif "%" in header:
                        cell.number_format = '0.0'
                    elif "conv" in header or "impression" in header or                          "click" in header:
                        cell.number_format = '#,##0.0'

        # Color lifecycle rows by state
        if sheet_name == "Keyword lifecycle":
            lifecycle_col = None
            for cell in ws[1]:
                if cell.value == "Lifecycle":
                    lifecycle_col = cell.column
                    break
            if lifecycle_col:
                for row in ws.iter_rows(min_row=2):
                    state = row[lifecycle_col - 1].value
                    if state in lifecycle_colors:
                        fill = PatternFill("solid",
                                           fgColor=lifecycle_colors[state])
                        for cell in row:
                            cell.fill = fill

        ws.freeze_panes = "A2"

    wb.save(path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("Activity Trend Analysis — Home Insurance")
    print(f"Months: {', '.join(MONTH_LABELS.values())}")
    print("=" * 60)

    conn = get_conn()

    print("\nFetching Google monthly activity...")
    google_df = fetch_keyword_monthly_activity(conn, GOOGLE_ACCOUNT_ID, "google")
    print(f"  {len(google_df):,} keyword-month rows")

    print("Fetching Microsoft monthly activity...")
    ms_df = fetch_keyword_monthly_activity(conn, MICROSOFT_ACCOUNT_ID, "microsoft")
    print(f"  {len(ms_df):,} keyword-month rows")

    conn.close()

    print("\nBuilding monthly active counts...")
    monthly_df = build_monthly_active(google_df, ms_df)

    print("Building keyword lifecycle classification...")
    lifecycle_df = build_lifecycle(google_df, ms_df)

    print("Building spend migration analysis...")
    migration_df = build_spend_migration(lifecycle_df)

    print("Building platform comparison...")
    platform_df = build_platform_comparison(google_df, ms_df)

    print("Building summary...")
    summary_df = build_summary(lifecycle_df, platform_df, migration_df)

    # Terminal preview
    print("\n--- Platform comparison ---")
    for _, row in platform_df.iterrows():
        print(f"\n  {row['Month']}")
        print(f"    Google    active={row['Google active keywords']:>5,}  "
              f"({row['Google active %']:>5.1f}%)  "
              f"spend=${row['Google spend ($)']:>10,.2f}  "
              f"offline_convs={row['Google offline convs']:>7,.1f}  "
              f"CPL=${row['Google CPL ($)'] or 0:>7.2f}  "
              f"ROAS={row['Google ROAS'] or 0:.2f}x  "
              f"revenue=${row['Google offline revenue ($)']:>10,.2f}")
        print(f"    Microsoft active={row['Microsoft active keywords']:>5,}  "
              f"({row['Microsoft active %']:>5.1f}%)  "
              f"spend=${row['Microsoft spend ($)']:>10,.2f}  "
              f"offline_convs={row['Microsoft offline convs']:>7,.1f}  "
              f"CPL=${row['Microsoft CPL ($)'] or 0:>7.2f}  "
              f"ROAS={row['Microsoft ROAS'] or 0:.2f}x  "
              f"revenue=${row['Microsoft offline revenue ($)']:>10,.2f}")

    print("\n--- Lifecycle summary ---")
    for platform in ["Google", "Microsoft"]:
        ldf = lifecycle_df[lifecycle_df["Platform"] == platform]
        print(f"\n  {platform}:")
        for state in ldf["Lifecycle"].value_counts().index:
            count = (ldf["Lifecycle"] == state).sum()
            spend = ldf[ldf["Lifecycle"] == state]["Total spend ($)"].sum()
            print(f"    {state:<25} {count:>5,} keywords  ${spend:>10,.2f}")

    print("\nWriting Excel...")
    with pd.ExcelWriter(OUTPUT_FILE, engine="openpyxl") as writer:
        summary_df.to_excel(writer, sheet_name="Summary", index=False)
        monthly_df.to_excel(writer, sheet_name="Monthly active", index=False)
        lifecycle_df.to_excel(writer, sheet_name="Keyword lifecycle", index=False)
        migration_df.to_excel(writer, sheet_name="Spend migration", index=False)
        platform_df.to_excel(writer, sheet_name="Platform comparison", index=False)

    print("Applying formatting...")
    format_workbook(OUTPUT_FILE)

    size_kb = os.path.getsize(OUTPUT_FILE) / 1024
    print(f"\nOutput saved: {OUTPUT_FILE}")
    print(f"File size   : {size_kb:.1f} KB")
    print("\nDone.")


if __name__ == "__main__":
    main()
