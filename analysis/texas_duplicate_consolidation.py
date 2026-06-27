"""
Match-type duplicate consolidation analysis — Texas Home Insurance.

Identifies normalized keyword phrases that have BOTH a BROAD/PHRASE/BMM identity
AND an EXACT identity active in the campaign, quantifies the volume flowing
through each variant, and produces upside/risk estimates to support a
consolidation decision.

The thesis: split signal across match-type duplicates is fragmenting the data
tROAS uses to optimize bids, contributing to the high week-over-week churn
(50% post-Mar-9) we observed in the diagnostic workbook. Consolidating to one
match type per phrase should reduce churn and stabilize conversion volume.

Outputs an Excel workbook with five sheets:
  1_pair_list                  Every BROAD/EXACT pair in the campaign
  2_aggregate_impact           Total volume at risk and current allocation
  3_volume_risk_by_pair        Per-pair conv that exists ONLY in the BROAD variant
  4_profit_upside_estimate     Bounded upside calculation with stated assumptions
  5_recommendation             Final pair-by-pair recommendation (pause / keep / review)

Run:
  python analysis/texas_duplicate_consolidation.py
  python analysis/texas_duplicate_consolidation.py --dry-run
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pandas as pd

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

DB_PATH = r"C:\Projects\google-ads-ops\db\paid_search.db"
OUTPUT_DIR = r"C:\Projects\google-ads-ops\analysis\output"

ACCOUNT_ID = 1
CAMPAIGN_ID = "23394808297"

# Use the same window as the diagnostic workbook so the numbers tie out.
WINDOW_START = "2026-02-09"
WINDOW_END = "2026-05-03"

# Recent window for assessing whether a pair is currently active.
RECENT_START = "2026-03-23"

EXCLUDE_KEYWORD_PATTERNS = ["renters"]

# A BROAD variant with conv >= this threshold in the recent 6 weeks is treated
# as "actively contributing" — pausing it has higher volume risk and warrants
# review rather than auto-pause.
ACTIVE_CONTRIBUTOR_RECENT_CONV = 5


# --------------------------------------------------------------------------
# SQL — per-identity totals over the window, with match_type
# --------------------------------------------------------------------------

# Note: this is per-identity (rolled up across instances), with match_type
# pulled from keyword_identities directly since identity_key includes match_type.
IDENTITY_TOTALS_SQL = f"""
SELECT
    kid.id                       AS identity_id,
    kid.normalized_text          AS keyword,
    kid.match_type               AS match_type,
    SUM(CASE WHEN kpd.report_date BETWEEN ? AND ?
             THEN kpd.primary_conv_count ELSE 0 END)        AS conv_full,
    SUM(CASE WHEN kpd.report_date BETWEEN ? AND ?
             THEN kpd.primary_conv_value ELSE 0 END)        AS revenue_full,
    SUM(CASE WHEN kpd.report_date BETWEEN ? AND ?
             THEN kpd.cost_micros ELSE 0 END) / 1e6         AS cost_full,
    SUM(CASE WHEN kpd.report_date >= ?
             THEN kpd.primary_conv_count ELSE 0 END)        AS conv_recent,
    SUM(CASE WHEN kpd.report_date >= ?
             THEN kpd.primary_conv_value ELSE 0 END)        AS revenue_recent,
    SUM(CASE WHEN kpd.report_date >= ?
             THEN kpd.cost_micros ELSE 0 END) / 1e6         AS cost_recent
FROM keyword_performance_daily kpd
JOIN keyword_instances  ki  ON kpd.instance_id = ki.id
JOIN keyword_identities kid ON ki.identity_id  = kid.id
JOIN campaigns          c   ON ki.campaign_id  = c.campaign_id
WHERE c.campaign_id = ?
  AND c.account_id  = ?
  AND kpd.device    = 'ALL'
  AND kpd.geo_state = 'ALL'
  AND kpd.report_date BETWEEN ? AND ?
  {{exclusion_clause}}
GROUP BY kid.id, kid.normalized_text, kid.match_type
"""

# Per-identity status from keyword_instances (rolled up: ENABLED if any instance
# is enabled, PAUSED if all are paused, etc.).
IDENTITY_STATUS_SQL = f"""
SELECT
    kid.id                       AS identity_id,
    SUM(CASE WHEN ki.status = 'ENABLED' THEN 1 ELSE 0 END)  AS n_enabled,
    SUM(CASE WHEN ki.status = 'PAUSED'  THEN 1 ELSE 0 END)  AS n_paused,
    COUNT(*)                                                 AS n_total
FROM keyword_instances ki
JOIN keyword_identities kid ON ki.identity_id = kid.id
JOIN campaigns          c   ON ki.campaign_id = c.campaign_id
WHERE c.campaign_id = ?
  AND c.account_id  = ?
  {{exclusion_clause}}
GROUP BY kid.id
"""


def _build_exclusion_clause() -> str:
    if not EXCLUDE_KEYWORD_PATTERNS:
        return ""
    clauses = [f"kid.normalized_text NOT LIKE '%{p}%'" for p in EXCLUDE_KEYWORD_PATTERNS]
    return "AND " + " AND ".join(clauses)


def fetch_identity_totals(conn: sqlite3.Connection) -> pd.DataFrame:
    sql = IDENTITY_TOTALS_SQL.format(exclusion_clause=_build_exclusion_clause())
    return pd.read_sql_query(
        sql, conn,
        params=(WINDOW_START, WINDOW_END,
                WINDOW_START, WINDOW_END,
                WINDOW_START, WINDOW_END,
                RECENT_START, RECENT_START, RECENT_START,
                CAMPAIGN_ID, ACCOUNT_ID,
                WINDOW_START, WINDOW_END),
    )


def fetch_identity_status(conn: sqlite3.Connection) -> pd.DataFrame:
    sql = IDENTITY_STATUS_SQL.format(exclusion_clause=_build_exclusion_clause())
    df = pd.read_sql_query(sql, conn, params=(CAMPAIGN_ID, ACCOUNT_ID))
    df["status"] = df.apply(
        lambda r: "ENABLED" if r["n_enabled"] == r["n_total"]
        else "PAUSED" if r["n_paused"] == r["n_total"]
        else "MIXED",
        axis=1,
    )
    return df[["identity_id", "status"]]


# --------------------------------------------------------------------------
# Pair identification & analyses
# --------------------------------------------------------------------------

def identify_pairs(totals: pd.DataFrame, status: pd.DataFrame) -> pd.DataFrame:
    """
    Find normalized_text values that have both EXACT and at least one
    non-EXACT (BROAD/PHRASE) identity in the campaign.

    Returns one row per (keyword, exact_id, broad_id) combination. If a
    keyword has multiple non-EXACT variants, each gets its own row.
    """
    df = totals.merge(status, on="identity_id", how="left")

    # Compute CPA and ROAS at the identity level for both windows.
    df["cpa_full"] = df.apply(
        lambda r: r["cost_full"] / r["conv_full"] if r["conv_full"] > 0 else None,
        axis=1,
    )
    df["cpa_recent"] = df.apply(
        lambda r: r["cost_recent"] / r["conv_recent"] if r["conv_recent"] > 0 else None,
        axis=1,
    )
    df["roas_full"] = df.apply(
        lambda r: r["revenue_full"] / r["cost_full"] if r["cost_full"] > 0 else None,
        axis=1,
    )
    df["roas_recent"] = df.apply(
        lambda r: r["revenue_recent"] / r["cost_recent"] if r["cost_recent"] > 0 else None,
        axis=1,
    )

    # Split into EXACT vs non-EXACT subsets.
    exact = df[df["match_type"] == "EXACT"].copy()
    other = df[df["match_type"] != "EXACT"].copy()

    # Inner join on keyword text — only keep phrases that exist in BOTH.
    pairs = other.merge(
        exact,
        on="keyword",
        suffixes=("_broad", "_exact"),
    )

    if pairs.empty:
        return pairs

    # Order columns for output readability.
    cols = [
        "keyword",
        "match_type_broad", "identity_id_broad", "status_broad",
        "match_type_exact", "identity_id_exact", "status_exact",
        "conv_full_broad", "conv_full_exact",
        "conv_recent_broad", "conv_recent_exact",
        "cost_full_broad", "cost_full_exact",
        "cost_recent_broad", "cost_recent_exact",
        "cpa_full_broad", "cpa_full_exact",
        "cpa_recent_broad", "cpa_recent_exact",
        "roas_recent_broad", "roas_recent_exact",
        "revenue_recent_broad", "revenue_recent_exact",
    ]
    pairs = pairs[[c for c in cols if c in pairs.columns]]

    # Round numerics for output.
    num_cols = [c for c in pairs.columns
                if any(c.startswith(p) for p in ["conv_", "cost_", "cpa_", "roas_", "revenue_"])]
    for c in num_cols:
        pairs[c] = pairs[c].round(2)

    return pairs.sort_values("conv_recent_broad", ascending=False).reset_index(drop=True)


def aggregate_impact(pairs: pd.DataFrame, totals: pd.DataFrame) -> pd.DataFrame:
    """Total volume flowing through BROAD variants of duplicate pairs."""
    if pairs.empty:
        return pd.DataFrame()

    # Volume flowing through the non-EXACT side of each pair (the "at risk"
    # population if we consolidate by pausing them).
    broad_ids = pairs["identity_id_broad"].unique()
    broad_totals = totals[totals["identity_id"].isin(broad_ids)]

    # Campaign-wide totals for context.
    campaign_conv_full = totals["conv_full"].sum()
    campaign_cost_full = totals["cost_full"].sum()
    campaign_conv_recent = totals["conv_recent"].sum()
    campaign_cost_recent = totals["cost_recent"].sum()

    rows = [
        {
            "metric": "total duplicate pairs",
            "value": len(pairs),
            "context": "BROAD/PHRASE variants with an EXACT twin",
        },
        {
            "metric": "BROAD-side conv (full window)",
            "value": round(broad_totals["conv_full"].sum(), 1),
            "context": f"{broad_totals['conv_full'].sum() / campaign_conv_full * 100:.1f}% of campaign conv",
        },
        {
            "metric": "BROAD-side cost (full window)",
            "value": round(broad_totals["cost_full"].sum(), 2),
            "context": f"{broad_totals['cost_full'].sum() / campaign_cost_full * 100:.1f}% of campaign cost",
        },
        {
            "metric": "BROAD-side conv (recent 6 wks)",
            "value": round(broad_totals["conv_recent"].sum(), 1),
            "context": f"{broad_totals['conv_recent'].sum() / max(campaign_conv_recent, 1) * 100:.1f}% of recent conv",
        },
        {
            "metric": "BROAD-side cost (recent 6 wks)",
            "value": round(broad_totals["cost_recent"].sum(), 2),
            "context": f"{broad_totals['cost_recent'].sum() / max(campaign_cost_recent, 1) * 100:.1f}% of recent cost",
        },
    ]

    # CPA differential — does the BROAD side cost more or less per conv than the EXACT?
    broad_cpa = broad_totals["cost_full"].sum() / max(broad_totals["conv_full"].sum(), 0.001)
    exact_ids = pairs["identity_id_exact"].unique()
    exact_totals = totals[totals["identity_id"].isin(exact_ids)]
    exact_cpa = exact_totals["cost_full"].sum() / max(exact_totals["conv_full"].sum(), 0.001)

    rows.append({
        "metric": "BROAD-side blended CPA",
        "value": round(broad_cpa, 2),
        "context": f"vs EXACT-side blended CPA of ${exact_cpa:.2f}",
    })

    return pd.DataFrame(rows)


def volume_risk_by_pair(pairs: pd.DataFrame) -> pd.DataFrame:
    """
    Per-pair classification of consolidation risk:
      LOW    — BROAD recent conv < ACTIVE_CONTRIBUTOR_RECENT_CONV
      MEDIUM — BROAD recent conv >= threshold AND EXACT also performing well
      HIGH   — BROAD recent conv >= threshold AND EXACT struggling (low conv or poor CPA)
    """
    if pairs.empty:
        return pd.DataFrame()

    rows = []
    for _, p in pairs.iterrows():
        broad_conv = p["conv_recent_broad"]
        exact_conv = p["conv_recent_exact"]
        broad_cpa = p.get("cpa_recent_broad")
        exact_cpa = p.get("cpa_recent_exact")

        if broad_conv < ACTIVE_CONTRIBUTOR_RECENT_CONV:
            risk = "LOW"
            rationale = (f"BROAD has only {broad_conv} recent conv; pausing has minimal volume impact. "
                         f"EXACT will absorb signal.")
        elif exact_conv >= broad_conv and pd.notna(exact_cpa):
            risk = "MEDIUM"
            rationale = (f"BROAD ({broad_conv} conv) is meaningful but EXACT ({exact_conv} conv) is "
                         f"performing — likely safe to consolidate.")
        elif exact_conv < broad_conv:
            risk = "HIGH"
            rationale = (f"BROAD ({broad_conv} conv) outperforming EXACT ({exact_conv} conv). "
                         f"Pausing BROAD may strand conversions EXACT can't catch.")
        else:
            risk = "REVIEW"
            rationale = "Insufficient signal to classify; needs manual review."

        rows.append({
            "keyword": p["keyword"],
            "broad_match_type": p["match_type_broad"],
            "broad_conv_recent": broad_conv,
            "exact_conv_recent": exact_conv,
            "broad_cpa_recent": broad_cpa,
            "exact_cpa_recent": exact_cpa,
            "consolidation_risk": risk,
            "rationale": rationale,
        })

    df = pd.DataFrame(rows)
    risk_order = {"LOW": 0, "MEDIUM": 1, "REVIEW": 2, "HIGH": 3}
    df["_sort"] = df["consolidation_risk"].map(risk_order)
    return df.sort_values(["_sort", "broad_conv_recent"], ascending=[True, False]).drop(columns="_sort").reset_index(drop=True)


def profit_upside_estimate(pairs: pd.DataFrame, totals: pd.DataFrame, risk: pd.DataFrame) -> pd.DataFrame:
    """
    Bounded estimate of profit upside from consolidation.

    The mechanism is NOT 'pausing BROAD frees up money to spend better' — under
    tROAS bidding, total spend is approximately bounded by what the algorithm
    can profitably deploy, not by individual keyword bids. The mechanism IS
    'pausing BROAD consolidates conversion signal, reducing weekly variance
    and giving the algorithm cleaner data to optimize against.'

    We translate that into a profit estimate via a variance-reduction proxy:
    if churn drops from 50% to 35% (a reasonable target given pre-Mar-9 churn
    was 44%), how much weekly profit volatility would that remove?
    """
    if pairs.empty:
        return pd.DataFrame()

    # LOW-risk pairs are the safe-to-pause candidates.
    safe_pairs = risk[risk["consolidation_risk"] == "LOW"]
    safe_keywords = set(safe_pairs["keyword"])
    safe_broad_ids = pairs[pairs["keyword"].isin(safe_keywords)]["identity_id_broad"].unique()

    safe_broad_totals = totals[totals["identity_id"].isin(safe_broad_ids)]

    n_safe = len(safe_pairs)
    safe_cost_recent = safe_broad_totals["cost_recent"].sum()
    safe_conv_recent = safe_broad_totals["conv_recent"].sum()
    safe_revenue_recent = safe_broad_totals["revenue_recent"].sum()
    safe_profit_recent = safe_revenue_recent - safe_cost_recent

    # Direct effect: pausing LOW-risk BROADs.
    # Assumption: ~70-90% of BROAD conv get recaptured by the EXACT variant
    # (queries the EXACT match-type already covers via Google's close-variant
    # matching). The other 10-30% are genuinely lost. Use 80% as midpoint with
    # explicit bounds.
    recapture_rate_low = 0.70
    recapture_rate_mid = 0.80
    recapture_rate_high = 0.90

    # Indirect effect: variance reduction → higher avg profit.
    # Pre-Mar-9 churn was 44%. Post is 50%. If consolidation gets churn back to
    # ~44% (the natural level), and pre-Mar-9 weekly profit was $206 vs post
    # $260, the variance penalty at higher churn is hard to attribute cleanly.
    # We don't claim variance-reduction profit upside in the estimate — we
    # only claim cost savings from pausing (recoverable via reallocation under
    # tROAS) and the strategic value of cleaner signal.
    #
    # Cost savings = savings on lost conv NOT recaptured (those are conversions
    # the campaign wasn't profitably acquiring anyway, since BROAD CPA tends
    # to be higher and ROAS tends to be lower).

    rows = [
        {
            "scenario": "DIRECT — pause all LOW-risk pairs",
            "value": "",
            "note": "",
        },
        {
            "scenario": f"  pairs paused",
            "value": n_safe,
            "note": "BROAD-side identities with <5 recent conv each",
        },
        {
            "scenario": "  cost removed (recent 6 wks)",
            "value": round(safe_cost_recent, 2),
            "note": f"~${safe_cost_recent / 6:.2f}/wk in spend reallocated by tROAS",
        },
        {
            "scenario": "  conv removed (recent 6 wks)",
            "value": round(safe_conv_recent, 1),
            "note": f"~{safe_conv_recent / 6:.1f}/wk; mostly recaptured by EXACT close variants",
        },
        {
            "scenario": "  est conv permanently lost (low recapture)",
            "value": round(safe_conv_recent * (1 - recapture_rate_high), 1),
            "note": f"assumes 90% recapture (most BROAD queries match EXACT close-variant)",
        },
        {
            "scenario": "  est conv permanently lost (high recapture)",
            "value": round(safe_conv_recent * (1 - recapture_rate_low), 1),
            "note": f"assumes 70% recapture (conservative)",
        },
        {
            "scenario": "  est profit retained (mid case 80% recapture)",
            "value": round(safe_revenue_recent * recapture_rate_mid - safe_cost_recent * recapture_rate_mid, 2),
            "note": "revenue and cost both scale by recapture rate",
        },
        {"scenario": "", "value": "", "note": ""},
        {
            "scenario": "INDIRECT — variance reduction (qualitative)",
            "value": "",
            "note": "",
        },
        {
            "scenario": "  current weekly profit volatility",
            "value": "",
            "note": "weekly profit ranges from -$5,043 to $1,426 in the 12-week window",
        },
        {
            "scenario": "  current week-over-week churn (post-Mar-9)",
            "value": "50%",
            "note": "vs 44% pre-Mar-9; 6pp gap is candidate for consolidation upside",
        },
        {
            "scenario": "  expected churn post-consolidation",
            "value": "~44%",
            "note": "return to pre-Mar-9 baseline as algorithm has cleaner signal",
        },
        {
            "scenario": "  effect on average weekly profit",
            "value": "uncertain",
            "note": "primary effect is variance reduction, not mean increase. "
                    "Likely modest (low single-digit % of weekly profit) but stabilizing.",
        },
    ]

    return pd.DataFrame(rows)


def per_pair_recommendation(pairs: pd.DataFrame, risk: pd.DataFrame) -> pd.DataFrame:
    """Final pair-by-pair action: PAUSE_BROAD, KEEP_BOTH, or PAUSE_EXACT."""
    if pairs.empty:
        return pd.DataFrame()

    merged = pairs.merge(
        risk[["keyword", "broad_match_type", "consolidation_risk", "rationale"]],
        left_on=["keyword", "match_type_broad"],
        right_on=["keyword", "broad_match_type"],
        how="left",
    )

    def action(row):
        risk_level = row["consolidation_risk"]
        if risk_level == "LOW":
            return "PAUSE_BROAD"
        elif risk_level == "MEDIUM":
            return "PAUSE_BROAD (after monitoring)"
        elif risk_level == "HIGH":
            # BROAD outperforming EXACT is unusual — it means the EXACT isn't catching
            # the queries that matter. Worth investigating before pausing either side.
            return "REVIEW (BROAD outperforming)"
        else:
            return "REVIEW"

    merged["recommended_action"] = merged.apply(action, axis=1)

    cols = ["keyword", "match_type_broad", "match_type_exact",
            "conv_recent_broad", "conv_recent_exact",
            "cost_recent_broad", "cost_recent_exact",
            "consolidation_risk", "recommended_action", "rationale"]
    return merged[[c for c in cols if c in merged.columns]]


# --------------------------------------------------------------------------
# Excel output
# --------------------------------------------------------------------------

SHEET_HEADERS = {
    "1_pair_list": (
        "Every keyword phrase that has BOTH an EXACT and a non-EXACT (BROAD/PHRASE) "
        "identity active in the Texas Home Insurance campaign. Suffix _broad refers to "
        "the non-EXACT side, _exact to the EXACT side. Sorted by recent BROAD-side "
        "conversion volume descending."
    ),
    "2_aggregate_impact": (
        "Total volume currently flowing through BROAD-side variants of duplicate pairs. "
        "This is the population at risk if we consolidate by pausing them. Compared to "
        "campaign-wide totals to size the consolidation candidate set."
    ),
    "3_volume_risk_by_pair": (
        f"Per-pair classification: LOW = BROAD has <{ACTIVE_CONTRIBUTOR_RECENT_CONV} "
        "recent conv (safe to pause). MEDIUM = both variants meaningful but EXACT is "
        "performing (likely safe). HIGH = BROAD outperforming EXACT (investigate first). "
        "Sorted by risk level then by BROAD recent conv."
    ),
    "4_profit_upside_estimate": (
        "Bounded estimate of consolidation upside. Two effects modeled: DIRECT cost/conv "
        "savings from pausing LOW-risk BROADs (with explicit recapture-rate assumptions), "
        "and INDIRECT variance reduction (qualitative — primary mechanism is reducing "
        "week-over-week churn from 50% back toward the pre-Mar-9 44% baseline)."
    ),
    "5_recommendation": (
        "Pair-by-pair recommended action. PAUSE_BROAD = safe to pause now. PAUSE_BROAD "
        "(after monitoring) = pause once variant is verified non-essential. REVIEW = "
        "manual investigation needed before any change."
    ),
}


def write_workbook(path: str, sheets: dict[str, pd.DataFrame]):
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment
    from openpyxl.utils.dataframe import dataframe_to_rows
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    wb.remove(wb.active)

    for sheet_name, df in sheets.items():
        ws = wb.create_sheet(title=sheet_name)
        header = SHEET_HEADERS.get(sheet_name, "")

        ws.cell(row=1, column=1, value=header).font = Font(italic=True)
        ws.cell(row=1, column=1).alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[1].height = 70
        ws.column_dimensions["A"].width = 32
        for col_idx in range(2, 25):
            ws.column_dimensions[get_column_letter(col_idx)].width = 16

        current_row = 3

        if df.empty:
            ws.cell(row=current_row, column=1, value="(no rows)").font = Font(italic=True)
            continue

        for r in dataframe_to_rows(df, index=False, header=True):
            for c, val in enumerate(r, start=1):
                if isinstance(val, pd.Timestamp):
                    val = val.strftime("%Y-%m-%d")
                ws.cell(row=current_row, column=c, value=val)
            current_row += 1

        header_row = current_row - len(df) - 1
        for cell in ws[header_row]:
            cell.font = Font(bold=True)

        # Widen text-heavy columns.
        wide_cols = {"keyword": 32, "rationale": 60, "recommended_action": 28,
                     "consolidation_risk": 18, "scenario": 48, "note": 50, "context": 36}
        for cell in ws[header_row]:
            if cell.value in wide_cols:
                ws.column_dimensions[get_column_letter(cell.column)].width = wide_cols[cell.value]

    wb.save(path)


# --------------------------------------------------------------------------
# Console summary
# --------------------------------------------------------------------------

def print_summary(pairs, agg, risk, recs, output_path):
    print()
    print("=" * 72)
    print("DUPLICATE CONSOLIDATION ANALYSIS SUMMARY")
    print("=" * 72)
    print()

    if pairs.empty:
        print("No BROAD/EXACT duplicate pairs found in the campaign.")
        return

    n_pairs = len(pairs)
    n_low = (risk["consolidation_risk"] == "LOW").sum()
    n_med = (risk["consolidation_risk"] == "MEDIUM").sum()
    n_high = (risk["consolidation_risk"] == "HIGH").sum()
    n_review = (risk["consolidation_risk"] == "REVIEW").sum()

    print(f"Total duplicate pairs found: {n_pairs}")
    print(f"  LOW risk    (safe to pause):    {n_low}")
    print(f"  MEDIUM risk (likely safe):      {n_med}")
    print(f"  HIGH risk   (BROAD outperforms): {n_high}")
    print(f"  REVIEW      (insufficient data): {n_review}")
    print()

    if not agg.empty:
        broad_conv_full_row = agg[agg["metric"] == "BROAD-side conv (full window)"]
        broad_cost_full_row = agg[agg["metric"] == "BROAD-side cost (full window)"]
        if not broad_conv_full_row.empty:
            print(f"BROAD-side volume (full window): "
                  f"{broad_conv_full_row.iloc[0]['value']} conv, "
                  f"${broad_cost_full_row.iloc[0]['value']:.2f} cost")
            print(f"  ({broad_conv_full_row.iloc[0]['context']})")

    print()
    print("TOP 3 LOW-RISK CANDIDATES (safe to pause now):")
    low_risk_pairs = risk[risk["consolidation_risk"] == "LOW"].head(3)
    for _, r in low_risk_pairs.iterrows():
        print(f"  {r['keyword'][:40]:40s}  {r['broad_match_type']:6s}  "
              f"BROAD recent conv: {r['broad_conv_recent']}  "
              f"EXACT recent conv: {r['exact_conv_recent']}")

    if n_high > 0:
        print()
        print(f"⚠  {n_high} HIGH-RISK PAIR(S) — BROAD outperforming EXACT, investigate before any change:")
        high_risk_pairs = risk[risk["consolidation_risk"] == "HIGH"]
        for _, r in high_risk_pairs.iterrows():
            print(f"  {r['keyword'][:40]:40s}  BROAD: {r['broad_conv_recent']} conv  "
                  f"EXACT: {r['exact_conv_recent']} conv")

    print()
    print(f"Workbook written to: {output_path}")
    print("=" * 72)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--output-dir", default=OUTPUT_DIR)
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: database not found at {args.db}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(args.db)
    try:
        if args.dry_run:
            print("DRY RUN")
            print("-" * 72)
            print("IDENTITY_TOTALS_SQL:")
            print(IDENTITY_TOTALS_SQL.format(exclusion_clause=_build_exclusion_clause()))
            print("-" * 72)
            totals = fetch_identity_totals(conn)
            status = fetch_identity_status(conn)
            print(f"Identities fetched: {len(totals)}")
            print(f"  EXACT:     {(totals['match_type'] == 'EXACT').sum()}")
            print(f"  BROAD:     {(totals['match_type'] == 'BROAD').sum()}")
            print(f"  PHRASE:    {(totals['match_type'] == 'PHRASE').sum()}")
            print(f"  Other:     {(~totals['match_type'].isin(['EXACT','BROAD','PHRASE'])).sum()}")
            pairs = identify_pairs(totals, status)
            print(f"Duplicate pairs identified: {len(pairs)}")
            return

        totals = fetch_identity_totals(conn)
        status = fetch_identity_status(conn)
        pairs = identify_pairs(totals, status)
        agg = aggregate_impact(pairs, totals)
        risk = volume_risk_by_pair(pairs)
        upside = profit_upside_estimate(pairs, totals, risk)
        recs = per_pair_recommendation(pairs, risk)

        Path(args.output_dir).mkdir(parents=True, exist_ok=True)
        output_path = os.path.join(
            args.output_dir,
            f"texas_duplicate_consolidation_{date.today().isoformat()}.xlsx",
        )

        sheets = {
            "1_pair_list": pairs,
            "2_aggregate_impact": agg,
            "3_volume_risk_by_pair": risk,
            "4_profit_upside_estimate": upside,
            "5_recommendation": recs,
        }
        write_workbook(output_path, sheets)
        print_summary(pairs, agg, risk, recs, output_path)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
