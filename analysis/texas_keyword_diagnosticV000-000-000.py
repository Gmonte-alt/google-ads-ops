"""
Texas Home Insurance campaign — keyword-level CPA volatility diagnostic.

Tests whether week-to-week conversion-volume swings (Mar 23 onward) are driven
by a rotating subset of keyword identities — consistent with the post-Mar-9
tROAS change rationing impressions to a less stable mix.

Outputs an Excel workbook with six sheets:
  1_concentration              Weekly top-10 share + Herfindahl, with pre/post summary
  1b_top_keywords_by_week      Long-form top-10 identities per week
  2_churn                      Week-over-week dropouts/new entries/median change
  3_pre_post_troas             Pre vs post Mar 9 comparison, top 30 movers
  3b_pre_post_excl_transition  Same as 3, with the Mar 9 transition week dropped
  4_recent_volatility          CV of weekly conversions, Mar 23 onward

Run:
  python analysis/texas_keyword_diagnostic.py            # writes workbook
  python analysis/texas_keyword_diagnostic.py --dry-run  # prints queries + row counts only
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

DB_PATH = r"C:\Projects\google-ads-ops\db\paid_search.db"
OUTPUT_DIR = r"C:\Projects\google-ads-ops\analysis\output"

ACCOUNT_ID = 1
CAMPAIGN_ID = "23394808297"

WINDOW_START = "2026-02-09"
WINDOW_END = "2026-05-03"

TROAS_CHANGE_DATE = "2026-03-09"   # post period starts on/after this date
PRE_END = "2026-03-08"             # pre period ends on/before this date
RECENT_START = "2026-03-23"        # recent 6-week window starts here

MIN_CONV_PRE_POST = 5              # threshold for Analysis 3
MIN_CONV_RECENT = 10               # threshold for Analysis 4
TOP_N_MOVERS = 30                  # rows in Analysis 3 output
TOP_N_KEYWORDS_PER_WEEK = 10       # rows per week in Analysis 1b


# --------------------------------------------------------------------------
# SQL — single canonical pull, all analyses derive from this DataFrame
# --------------------------------------------------------------------------

# Monday-anchored week_start. SQLite strftime('%w') returns 0=Sun..6=Sat,
# so days_back = (dow + 6) % 7 gives the offset to the prior Monday.
WEEK_START_EXPR = (
    "date(kpd.report_date, "
    "'-' || ((CAST(strftime('%w', kpd.report_date) AS INTEGER) + 6) % 7) "
    "|| ' days')"
)

DAILY_KEYWORD_SQL = f"""
SELECT
    {WEEK_START_EXPR}            AS week_start,
    kpd.report_date              AS report_date,
    kid.id                       AS identity_id,
    kid.normalized_text          AS keyword,
    SUM(kpd.primary_conv_count)  AS conv,
    SUM(kpd.primary_conv_value)  AS revenue,
    SUM(kpd.cost_micros) / 1e6   AS cost
FROM keyword_performance_daily kpd
JOIN keyword_instances  ki  ON kpd.instance_id = ki.id
JOIN keyword_identities kid ON ki.identity_id  = kid.id
JOIN campaigns          c   ON ki.campaign_id  = c.campaign_id
WHERE c.campaign_id   = ?
  AND c.account_id    = ?
  AND kpd.device      = 'ALL'
  AND kpd.geo_state   = 'ALL'
  AND kpd.report_date BETWEEN ? AND ?
GROUP BY week_start, kpd.report_date, kid.id, kid.normalized_text
"""


def fetch_daily(conn: sqlite3.Connection) -> pd.DataFrame:
    """Pull daily identity-level rows for the full window. Single source of truth."""
    df = pd.read_sql_query(
        DAILY_KEYWORD_SQL,
        conn,
        params=(CAMPAIGN_ID, ACCOUNT_ID, WINDOW_START, WINDOW_END),
        parse_dates=["week_start", "report_date"],
    )
    return df


def aggregate_weekly_identity(df: pd.DataFrame) -> pd.DataFrame:
    """Roll daily rows up to (week_start, identity_id) grain."""
    return (
        df.groupby(["week_start", "identity_id", "keyword"], as_index=False)
        .agg(conv=("conv", "sum"), cost=("cost", "sum"), revenue=("revenue", "sum"))
    )


# --------------------------------------------------------------------------
# Analysis 1 — Weekly keyword concentration
# --------------------------------------------------------------------------

def analysis_1_concentration(weekly: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Returns:
      summary:         one row per week (Herfindahl, top10_share, total_conv)
      pre_post_block:  pre-Mar-9 avg vs post-Mar-9 avg for the summary metrics
      top_keywords:    long-form top-N keywords per week
    """
    rows = []
    top_kw_rows = []

    for week, grp in weekly.groupby("week_start"):
        # Only count identities with positive conversions for shares.
        active = grp[grp["conv"] > 0].copy()
        total_conv = active["conv"].sum()

        if total_conv == 0:
            rows.append({
                "week_start": week,
                "total_conv": 0,
                "active_identities": 0,
                "top10_share": float("nan"),
                "top1_share": float("nan"),
                "herfindahl": float("nan"),
            })
            continue

        active["share"] = active["conv"] / total_conv
        active = active.sort_values("conv", ascending=False)

        herfindahl = (active["share"] ** 2).sum()
        top10_share = active.head(10)["share"].sum()
        top1_share = active.iloc[0]["share"]

        rows.append({
            "week_start": week,
            "total_conv": int(total_conv),
            "active_identities": len(active),
            "top10_share": round(top10_share, 4),
            "top1_share": round(top1_share, 4),
            "herfindahl": round(herfindahl, 4),
        })

        for rank, (_, r) in enumerate(active.head(TOP_N_KEYWORDS_PER_WEEK).iterrows(), start=1):
            top_kw_rows.append({
                "week_start": week,
                "rank": rank,
                "identity_id": r["identity_id"],
                "keyword": r["keyword"],
                "conv": int(r["conv"]),
                "share": round(r["share"], 4),
                "cost": round(r["cost"], 2),
            })

    summary = pd.DataFrame(rows).sort_values("week_start").reset_index(drop=True)
    top_keywords = pd.DataFrame(top_kw_rows).sort_values(["week_start", "rank"]).reset_index(drop=True)

    # Pre/post block — direct test of the rationing hypothesis.
    troas_dt = pd.Timestamp(TROAS_CHANGE_DATE)
    pre_mask = summary["week_start"] < troas_dt
    post_mask = summary["week_start"] >= troas_dt

    pre_post_block = pd.DataFrame([
        {
            "metric": "avg_herfindahl",
            "pre_mar9": round(summary.loc[pre_mask, "herfindahl"].mean(), 4),
            "post_mar9": round(summary.loc[post_mask, "herfindahl"].mean(), 4),
        },
        {
            "metric": "avg_top10_share",
            "pre_mar9": round(summary.loc[pre_mask, "top10_share"].mean(), 4),
            "post_mar9": round(summary.loc[post_mask, "top10_share"].mean(), 4),
        },
        {
            "metric": "avg_top1_share",
            "pre_mar9": round(summary.loc[pre_mask, "top1_share"].mean(), 4),
            "post_mar9": round(summary.loc[post_mask, "top1_share"].mean(), 4),
        },
        {
            "metric": "avg_active_identities",
            "pre_mar9": round(summary.loc[pre_mask, "active_identities"].mean(), 1),
            "post_mar9": round(summary.loc[post_mask, "active_identities"].mean(), 1),
        },
    ])
    pre_post_block["delta"] = pre_post_block["post_mar9"] - pre_post_block["pre_mar9"]

    return summary, pre_post_block, top_keywords


# --------------------------------------------------------------------------
# Analysis 2 — Keyword churn between consecutive weeks
# --------------------------------------------------------------------------

def analysis_2_churn(weekly: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Returns:
      churn:          one row per consecutive week pair
      pre_post_block: pre vs post averages for churn metrics
    """
    weeks = sorted(weekly["week_start"].unique())

    # identity → conv map per week, restricted to identities with >=1 conv
    by_week: dict = {}
    for w in weeks:
        wgrp = weekly[(weekly["week_start"] == w) & (weekly["conv"] > 0)]
        by_week[w] = dict(zip(wgrp["identity_id"], wgrp["conv"]))

    rows = []
    for w_n, w_n1 in zip(weeks[:-1], weeks[1:]):
        ids_n = set(by_week[w_n].keys())
        ids_n1 = set(by_week[w_n1].keys())

        dropouts = ids_n - ids_n1
        new_entries = ids_n1 - ids_n
        retained = ids_n & ids_n1

        if retained:
            changes = [by_week[w_n1][i] - by_week[w_n][i] for i in retained]
            median_change = float(pd.Series(changes).median())
            mean_change = float(pd.Series(changes).mean())
        else:
            median_change = float("nan")
            mean_change = float("nan")

        rows.append({
            "week_n": w_n,
            "week_n1": w_n1,
            "week_pair": f"{pd.Timestamp(w_n).date()} → {pd.Timestamp(w_n1).date()}",
            "ids_week_n": len(ids_n),
            "ids_week_n1": len(ids_n1),
            "dropouts": len(dropouts),
            "new_entries": len(new_entries),
            "retained": len(retained),
            "churn_rate": round(
                (len(dropouts) + len(new_entries)) / max(len(ids_n | ids_n1), 1), 4
            ),
            "median_change": round(median_change, 2) if pd.notna(median_change) else None,
            "mean_change": round(mean_change, 2) if pd.notna(mean_change) else None,
        })

    churn = pd.DataFrame(rows)

    # Pre/post block — pair labeled by its starting week (week_n).
    troas_dt = pd.Timestamp(TROAS_CHANGE_DATE)
    pre_mask = churn["week_n"] < troas_dt
    post_mask = churn["week_n"] >= troas_dt

    pre_post_block = pd.DataFrame([
        {
            "metric": "avg_dropouts",
            "pre_mar9": round(churn.loc[pre_mask, "dropouts"].mean(), 1),
            "post_mar9": round(churn.loc[post_mask, "dropouts"].mean(), 1),
        },
        {
            "metric": "avg_new_entries",
            "pre_mar9": round(churn.loc[pre_mask, "new_entries"].mean(), 1),
            "post_mar9": round(churn.loc[post_mask, "new_entries"].mean(), 1),
        },
        {
            "metric": "avg_churn_rate",
            "pre_mar9": round(churn.loc[pre_mask, "churn_rate"].mean(), 4),
            "post_mar9": round(churn.loc[post_mask, "churn_rate"].mean(), 4),
        },
        {
            "metric": "avg_median_change",
            "pre_mar9": round(churn.loc[pre_mask, "median_change"].mean(), 2),
            "post_mar9": round(churn.loc[post_mask, "median_change"].mean(), 2),
        },
    ])
    pre_post_block["delta"] = pre_post_block["post_mar9"] - pre_post_block["pre_mar9"]

    return churn, pre_post_block


# --------------------------------------------------------------------------
# Analysis 3 — Pre/post tROAS comparison
# --------------------------------------------------------------------------

def _pre_post_movers(daily: pd.DataFrame, pre_start: str, pre_end: str,
                     post_start: str, post_end: str) -> pd.DataFrame:
    """Per-identity pre vs post comparison. pct_change_conv uses weekly rates
    to control for asymmetric window length."""
    pre_weeks = max((pd.Timestamp(pre_end) - pd.Timestamp(pre_start)).days / 7, 1e-9)
    post_weeks = max((pd.Timestamp(post_end) - pd.Timestamp(post_start)).days / 7, 1e-9)

    pre = daily[(daily["report_date"] >= pre_start) & (daily["report_date"] <= pre_end)]
    post = daily[(daily["report_date"] >= post_start) & (daily["report_date"] <= post_end)]

    pre_agg = pre.groupby(["identity_id", "keyword"], as_index=False).agg(
        pre_conv=("conv", "sum"), pre_cost=("cost", "sum")
    )
    post_agg = post.groupby(["identity_id", "keyword"], as_index=False).agg(
        post_conv=("conv", "sum"), post_cost=("cost", "sum")
    )

    merged = pd.merge(pre_agg, post_agg, on=["identity_id", "keyword"], how="outer").fillna(0)

    # Threshold: >= MIN_CONV_PRE_POST in *either* period.
    merged = merged[(merged["pre_conv"] >= MIN_CONV_PRE_POST)
                    | (merged["post_conv"] >= MIN_CONV_PRE_POST)].copy()

    merged["pre_conv_per_week"] = merged["pre_conv"] / pre_weeks
    merged["post_conv_per_week"] = merged["post_conv"] / post_weeks

    # CPA: NaN where conv is 0 (don't divide by zero, don't fabricate "infinite CPA").
    merged["pre_cpa"] = merged.apply(
        lambda r: r["pre_cost"] / r["pre_conv"] if r["pre_conv"] > 0 else float("nan"), axis=1
    )
    merged["post_cpa"] = merged.apply(
        lambda r: r["post_cost"] / r["post_conv"] if r["post_conv"] > 0 else float("nan"), axis=1
    )

    # Pct change on weekly rates. NaN if pre rate is 0 (new entrant) — flag separately.
    def pct(a, b):
        if b == 0:
            return float("nan")
        return (a - b) / b

    merged["pct_change_conv"] = merged.apply(
        lambda r: pct(r["post_conv_per_week"], r["pre_conv_per_week"]), axis=1
    )
    merged["pct_change_cpa"] = merged.apply(
        lambda r: pct(r["post_cpa"], r["pre_cpa"]) if pd.notna(r["pre_cpa"]) and pd.notna(r["post_cpa"]) else float("nan"),
        axis=1,
    )

    # Sort by absolute change in weekly conv rate. New entrants (NaN pct) sink to bottom;
    # surface them with a tag so the user sees them explicitly.
    merged["status"] = "compared"
    merged.loc[merged["pre_conv"] == 0, "status"] = "new_entrant_post"
    merged.loc[merged["post_conv"] == 0, "status"] = "dropped_out_post"

    merged["abs_pct_change_conv"] = merged["pct_change_conv"].abs()
    merged = merged.sort_values(
        ["abs_pct_change_conv", "post_conv"],
        ascending=[False, False],
        na_position="last",
    )

    # Round for display.
    for col in ["pre_conv", "post_conv", "pre_cost", "post_cost",
                "pre_conv_per_week", "post_conv_per_week",
                "pre_cpa", "post_cpa", "pct_change_conv", "pct_change_cpa"]:
        merged[col] = merged[col].round(2)

    return merged.head(TOP_N_MOVERS).drop(columns=["abs_pct_change_conv"]).reset_index(drop=True)


def analysis_3_pre_post(daily: pd.DataFrame) -> pd.DataFrame:
    return _pre_post_movers(daily, WINDOW_START, PRE_END, TROAS_CHANGE_DATE, WINDOW_END)


def analysis_3b_pre_post_excl_transition(daily: pd.DataFrame) -> pd.DataFrame:
    """Same as 3 but the Mar 9 transition week (Mar 9-15) is excluded from post."""
    transition_start = "2026-03-09"
    transition_end = "2026-03-15"
    excluded = daily[~((daily["report_date"] >= transition_start)
                       & (daily["report_date"] <= transition_end))]
    return _pre_post_movers(excluded, WINDOW_START, PRE_END, "2026-03-16", WINDOW_END)


# --------------------------------------------------------------------------
# Analysis 4 — Recent 6-week volatility
# --------------------------------------------------------------------------

def analysis_4_volatility(weekly: pd.DataFrame) -> pd.DataFrame:
    recent = weekly[weekly["week_start"] >= pd.Timestamp(RECENT_START)].copy()

    # Build full identity × week matrix so absent weeks count as 0.
    weeks = sorted(recent["week_start"].unique())
    identities = recent.groupby(["identity_id", "keyword"], as_index=False)["conv"].sum()
    identities = identities[identities["conv"] >= MIN_CONV_RECENT]

    rows = []
    for _, ident in identities.iterrows():
        per_week = recent[recent["identity_id"] == ident["identity_id"]].set_index("week_start")["conv"]
        # Reindex to all weeks, fill missing with 0.
        series = per_week.reindex(weeks, fill_value=0)
        mean = series.mean()
        std = series.std(ddof=0)  # population stdev — we have the full window, not a sample
        cv = std / mean if mean > 0 else float("nan")

        rows.append({
            "identity_id": ident["identity_id"],
            "keyword": ident["keyword"],
            "total_conv_6w": int(series.sum()),
            "weeks_with_conv": int((series > 0).sum()),
            "mean_weekly": round(mean, 2),
            "std_weekly": round(std, 2),
            "cv": round(cv, 3) if pd.notna(cv) else None,
            "min_week": int(series.min()),
            "max_week": int(series.max()),
        })

    df = pd.DataFrame(rows).sort_values("cv", ascending=False, na_position="last").reset_index(drop=True)
    return df


# --------------------------------------------------------------------------
# Excel output
# --------------------------------------------------------------------------

SHEET_HEADERS = {
    "1_concentration": (
        "Weekly keyword concentration. Herfindahl = sum of squared identity shares "
        "of weekly conversions (full distribution, not just top 10). Higher = more "
        "concentrated. Pre/post block at top tests the rationing hypothesis directly."
    ),
    "1b_top_keywords_by_week": (
        f"Top {TOP_N_KEYWORDS_PER_WEEK} keyword identities by conversions, per week. "
        "Use to eyeball which keywords are concentrated in spike weeks."
    ),
    "2_churn": (
        "Week-over-week churn at identity level. dropouts = had conv in week N, none in N+1. "
        "new_entries = vice versa. churn_rate = (dropouts + new_entries) / total unique "
        "identities across the pair. Pre/post block at top."
    ),
    "3_pre_post_troas": (
        f"Pre (Feb 9 - Mar 8, 4 wks) vs post (Mar 9 - May 3, 8 wks) tROAS comparison. "
        f"Threshold: >= {MIN_CONV_PRE_POST} conv in either period. pct_change_conv is "
        "computed on WEEKLY RATES (per-week conv) to control for asymmetric window length. "
        f"Top {TOP_N_MOVERS} by |pct_change_conv|. status flags new_entrant_post and dropped_out_post."
    ),
    "3b_pre_post_excl_transition": (
        "Same as 3_pre_post_troas, but the Mar 9-15 transition week is excluded from the "
        "post period. Mar 9 was the worst-profit week (237 conv at $72 CPA, $-5,043 profit), "
        "likely a transition artifact as the new tROAS took effect mid-week. This sheet "
        "removes that distortion. Post period here is Mar 16 - May 3 (7 weeks)."
    ),
    "4_recent_volatility": (
        f"Coefficient of variation (stdev/mean) of weekly conversions, Mar 23 onward "
        f"(6 weeks). Filtered to identities with >= {MIN_CONV_RECENT} total conv in the period. "
        "Zero-conv weeks counted as zero. Top of list = identities most responsible for "
        "campaign-level week-to-week volume swings."
    ),
}


def write_workbook(path: str, sheets: dict[str, pd.DataFrame], pre_post_blocks: dict[str, pd.DataFrame]):
    """Each sheet: row 1 = description, row 2 blank, then optional pre/post block, then data."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment
    from openpyxl.utils.dataframe import dataframe_to_rows

    wb = Workbook()
    wb.remove(wb.active)  # drop default sheet

    for sheet_name, df in sheets.items():
        ws = wb.create_sheet(title=sheet_name)
        header = SHEET_HEADERS.get(sheet_name, "")

        ws.cell(row=1, column=1, value=header).font = Font(italic=True)
        ws.cell(row=1, column=1).alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[1].height = 60
        ws.column_dimensions["A"].width = 28
        for col in "BCDEFGHIJKLMN":
            ws.column_dimensions[col].width = 16

        current_row = 3

        # If there's a pre/post summary block, write it first
        if sheet_name in pre_post_blocks:
            ws.cell(row=current_row, column=1, value="Pre vs post Mar 9 summary").font = Font(bold=True)
            current_row += 1
            block_df = pre_post_blocks[sheet_name]
            for r in dataframe_to_rows(block_df, index=False, header=True):
                for c, val in enumerate(r, start=1):
                    ws.cell(row=current_row, column=c, value=val)
                current_row += 1
            current_row += 2  # blank line before main table
            ws.cell(row=current_row, column=1, value="Detail").font = Font(bold=True)
            current_row += 1

        # Main table
        for r in dataframe_to_rows(df, index=False, header=True):
            for c, val in enumerate(r, start=1):
                # Convert pandas timestamps to date strings for Excel readability
                if isinstance(val, pd.Timestamp):
                    val = val.strftime("%Y-%m-%d")
                ws.cell(row=current_row, column=c, value=val)
            current_row += 1

        # Bold header row of the main table (the row right after "Detail" label or blank)
        # We tracked it as `current_row - len(df) - 1` for the header.
        header_row = current_row - len(df) - 1
        for cell in ws[header_row]:
            cell.font = Font(bold=True)

    wb.save(path)


# --------------------------------------------------------------------------
# Console summary
# --------------------------------------------------------------------------

def print_summary(concentration: pd.DataFrame, conc_pp: pd.DataFrame,
                  churn: pd.DataFrame, churn_pp: pd.DataFrame,
                  movers: pd.DataFrame, volatility: pd.DataFrame,
                  output_path: str):
    print()
    print("=" * 72)
    print("TEXAS HOME INSURANCE — KEYWORD DIAGNOSTIC SUMMARY")
    print("=" * 72)

    # Lead with the rationing test.
    h_pre = conc_pp.loc[conc_pp["metric"] == "avg_herfindahl", "pre_mar9"].iloc[0]
    h_post = conc_pp.loc[conc_pp["metric"] == "avg_herfindahl", "post_mar9"].iloc[0]
    cr_pre = churn_pp.loc[churn_pp["metric"] == "avg_churn_rate", "pre_mar9"].iloc[0]
    cr_post = churn_pp.loc[churn_pp["metric"] == "avg_churn_rate", "post_mar9"].iloc[0]

    print()
    print("RATIONING-HYPOTHESIS TEST (avg pre-Mar-9 vs post-Mar-9):")
    print(f"  Herfindahl:  {h_pre:.4f}  →  {h_post:.4f}   (delta {h_post - h_pre:+.4f})")
    print(f"  Churn rate:  {cr_pre:.4f}  →  {cr_post:.4f}   (delta {cr_post - cr_pre:+.4f})")
    print("  Higher post-period values on either metric support the hypothesis that")
    print("  tROAS is rationing impressions to a rotating subset of keywords.")

    # Concentration extremes
    if not concentration["herfindahl"].isna().all():
        max_h = concentration.loc[concentration["herfindahl"].idxmax()]
        min_h = concentration.loc[concentration["herfindahl"].idxmin()]
        print()
        print("WEEKLY CONCENTRATION:")
        print(f"  Max Herfindahl:  {max_h['herfindahl']:.4f}  week of {pd.Timestamp(max_h['week_start']).date()} "
              f"(top10 share {max_h['top10_share']:.2f}, total conv {max_h['total_conv']})")
        print(f"  Min Herfindahl:  {min_h['herfindahl']:.4f}  week of {pd.Timestamp(min_h['week_start']).date()} "
              f"(top10 share {min_h['top10_share']:.2f}, total conv {min_h['total_conv']})")

    # Churn extremes
    if not churn.empty:
        max_churn = churn.loc[churn["churn_rate"].idxmax()]
        print()
        print("WEEKLY CHURN:")
        print(f"  Max churn rate:  {max_churn['churn_rate']:.4f}  pair {max_churn['week_pair']} "
              f"(dropouts {max_churn['dropouts']}, new {max_churn['new_entries']})")

    # Top movers from Analysis 3
    print()
    print("TOP 5 PRE/POST MOVERS (by |pct change in weekly conv rate|):")
    for _, r in movers.head(5).iterrows():
        pc = r["pct_change_conv"]
        pc_str = f"{pc * 100:+.0f}%" if pd.notna(pc) else r["status"]
        print(f"  {r['keyword'][:50]:50s}  pre {r['pre_conv']:.0f} → post {r['post_conv']:.0f}  ({pc_str})")

    # Top volatile keywords from Analysis 4
    print()
    print("TOP 5 RECENT-6-WEEK VOLATILE IDENTITIES (by CV):")
    for _, r in volatility.head(5).iterrows():
        cv = r["cv"]
        cv_str = f"{cv:.2f}" if cv is not None and pd.notna(cv) else "n/a"
        print(f"  {r['keyword'][:50]:50s}  total {r['total_conv_6w']:3d}  weeks_active "
              f"{r['weeks_with_conv']}/6  cv {cv_str}")

    print()
    print(f"Workbook written to: {output_path}")
    print("=" * 72)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Print queries and row counts without writing the workbook.")
    parser.add_argument("--db", default=DB_PATH, help="Override DB path (debugging).")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, help="Override output dir.")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: database not found at {args.db}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(args.db)
    try:
        if args.dry_run:
            print("DRY RUN — printing main query and row counts only.")
            print("-" * 72)
            print("DAILY_KEYWORD_SQL:")
            print(DAILY_KEYWORD_SQL)
            print("-" * 72)
            print(f"Params: campaign_id={CAMPAIGN_ID}, account_id={ACCOUNT_ID}, "
                  f"window={WINDOW_START}..{WINDOW_END}")
            daily = fetch_daily(conn)
            print(f"Daily rows fetched: {len(daily):,}")
            print(f"Distinct identities: {daily['identity_id'].nunique():,}")
            print(f"Distinct weeks: {daily['week_start'].nunique()}")
            print(f"Date range: {daily['report_date'].min()} to {daily['report_date'].max()}")
            print(f"Total conv: {daily['conv'].sum():,.0f}")
            print(f"Total cost: ${daily['cost'].sum():,.2f}")
            return

        daily = fetch_daily(conn)
        if daily.empty:
            print("ERROR: no rows returned from main query. Check campaign_id, account_id, date window.",
                  file=sys.stderr)
            sys.exit(1)

        weekly = aggregate_weekly_identity(daily)

        # Run analyses
        conc_summary, conc_pp_block, top_kw_by_week = analysis_1_concentration(weekly)
        churn, churn_pp_block = analysis_2_churn(weekly)
        movers = analysis_3_pre_post(daily)
        movers_excl = analysis_3b_pre_post_excl_transition(daily)
        volatility = analysis_4_volatility(weekly)

        # Write workbook
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)
        output_path = os.path.join(
            args.output_dir,
            f"texas_keyword_diagnostic_{date.today().isoformat()}.xlsx",
        )
        sheets = {
            "1_concentration": conc_summary,
            "1b_top_keywords_by_week": top_kw_by_week,
            "2_churn": churn,
            "3_pre_post_troas": movers,
            "3b_pre_post_excl_transition": movers_excl,
            "4_recent_volatility": volatility,
        }
        pre_post_blocks = {
            "1_concentration": conc_pp_block,
            "2_churn": churn_pp_block,
        }
        write_workbook(output_path, sheets, pre_post_blocks)

        print_summary(conc_summary, conc_pp_block, churn, churn_pp_block,
                      movers, volatility, output_path)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
