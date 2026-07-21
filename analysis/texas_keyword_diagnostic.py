"""
Texas Home Insurance campaign — keyword-level CPA volatility diagnostic.

Tests whether week-to-week conversion-volume swings (Mar 23 onward) are driven
by a rotating subset of keyword identities — consistent with the post-Mar-9
tROAS change rationing impressions to a less stable mix.

Outputs an Excel workbook with seven sheets:
  1_concentration              Weekly top-10 share + Herfindahl, with pre/post summary
  1b_top_keywords_by_week      Long-form top-10 identities per week
  2_churn                      Week-over-week dropouts/new entries/median change
  3_pre_post_troas             Pre vs post Mar 9 comparison, top 30 movers
  3b_pre_post_excl_transition  Same as 3, with the Mar 9 transition week dropped
  4_recent_volatility          CV of weekly conversions, Mar 23 onward (>=10 conv filter)
  4b_all_recent_active         Unfiltered companion to sheet 4: every identity with >=1
                               conv in the recent 6 weeks, sorted by total conv desc

Excludes renters insurance keywords (out of scope for a home insurance campaign).
Sheets 1b, 3, 3b, 4 carry instance-derived context columns: match_type_summary,
keyword_status_summary, ad_group_count, ad_groups_summary, dominant_ad_group_status.

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

# Renters insurance keywords are out of scope for a Texas home insurance campaign.
# Their presence pre-window is itself a finding (likely broad-match spillover) but
# they're noise for the volatility diagnostic. Reverse by emptying this list.
EXCLUDE_KEYWORD_PATTERNS = ["renters"]

# A keyword whose latest activity is >7 days before WINDOW_END is flagged stale
# in keyword_status_summary even if its instance status is ENABLED.
# DISABLED BY DEFAULT: in the May 5 run, 162/168 identities flagged stale, which
# strongly suggests last_seen_date does NOT mean "last day this keyword served an
# impression" but something else (sync timestamp lag? first-seen mirror?). Until
# the field's actual semantics are confirmed, this would be noise on every row.
# Re-enable by setting STALENESS_ENABLED = True.
STALENESS_ENABLED = False
STALENESS_DAYS = 7

# Truncation length for ad_groups_summary column in output.
AD_GROUPS_SUMMARY_MAX_LEN = 60


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
  {{exclusion_clause}}
GROUP BY week_start, kpd.report_date, kid.id, kid.normalized_text
"""


def _build_exclusion_clause() -> str:
    """SQL fragment for EXCLUDE_KEYWORD_PATTERNS. Returns empty string if no exclusions."""
    if not EXCLUDE_KEYWORD_PATTERNS:
        return ""
    clauses = [f"kid.normalized_text NOT LIKE '%{p}%'" for p in EXCLUDE_KEYWORD_PATTERNS]
    return "AND " + " AND ".join(clauses)


def fetch_daily(conn: sqlite3.Connection) -> pd.DataFrame:
    """Pull daily identity-level rows for the full window. Single source of truth."""
    sql = DAILY_KEYWORD_SQL.format(exclusion_clause=_build_exclusion_clause())
    df = pd.read_sql_query(
        sql,
        conn,
        params=(CAMPAIGN_ID, ACCOUNT_ID, WINDOW_START, WINDOW_END),
        parse_dates=["week_start", "report_date"],
    )
    return df


# --------------------------------------------------------------------------
# Per-identity context — match_type, statuses, ad-group rollup
# --------------------------------------------------------------------------

# One row per (identity, instance, ad_group). We aggregate to identity-level in pandas
# because the "dominant" calculations need conv weights from the daily table.
INSTANCE_CONTEXT_SQL = f"""
SELECT
    kid.id                  AS identity_id,
    kid.normalized_text     AS keyword,
    ki.id                   AS instance_id,
    ki.match_type           AS match_type,
    ki.status               AS keyword_status,
    ki.last_seen_date       AS last_seen_date,
    ki.ad_group_id          AS ad_group_id,
    ag.ad_group_name        AS ad_group_name,
    ag.status               AS ad_group_status
FROM keyword_instances ki
JOIN keyword_identities kid ON ki.identity_id  = kid.id
JOIN campaigns          c   ON ki.campaign_id  = c.campaign_id
LEFT JOIN ad_groups     ag  ON ki.ad_group_id  = ag.ad_group_id
WHERE c.campaign_id = ?
  AND c.account_id  = ?
  {{exclusion_clause}}
"""

# Per-instance conv totals over the window. Used to weight "dominant" calculations
# when an identity spans multiple instances (different match types or ad groups).
INSTANCE_CONV_SQL = f"""
SELECT
    kpd.instance_id          AS instance_id,
    SUM(kpd.primary_conv_count) AS conv
FROM keyword_performance_daily kpd
JOIN keyword_instances  ki  ON kpd.instance_id = ki.id
JOIN keyword_identities kid ON ki.identity_id  = kid.id
JOIN campaigns          c   ON ki.campaign_id  = c.campaign_id
WHERE c.campaign_id   = ?
  AND c.account_id    = ?
  AND kpd.device      = 'ALL'
  AND kpd.geo_state   = 'ALL'
  AND kpd.report_date BETWEEN ? AND ?
  {{exclusion_clause}}
GROUP BY kpd.instance_id
"""


def fetch_instance_context(conn: sqlite3.Connection) -> pd.DataFrame:
    sql = INSTANCE_CONTEXT_SQL.format(exclusion_clause=_build_exclusion_clause())
    return pd.read_sql_query(
        sql,
        conn,
        params=(CAMPAIGN_ID, ACCOUNT_ID),
        parse_dates=["last_seen_date"],
    )


def fetch_instance_conv(conn: sqlite3.Connection) -> pd.DataFrame:
    """Per-instance conv totals over the window — for weighting dominant calculations."""
    sql = INSTANCE_CONV_SQL.format(exclusion_clause=_build_exclusion_clause())
    return pd.read_sql_query(
        sql,
        conn,
        params=(CAMPAIGN_ID, ACCOUNT_ID, WINDOW_START, WINDOW_END),
    )


def _dominant_by_conv(grp: pd.DataFrame, group_col: str, conv_col: str = "conv") -> str | None:
    """Pick the value of group_col with the most conv. Falls back to mode on ties or all-zero."""
    if grp.empty:
        return None
    summed = grp.groupby(group_col, dropna=False)[conv_col].sum().sort_values(ascending=False)
    if summed.iloc[0] == 0:
        # Nobody has any conv — fall back to instance count.
        return grp[group_col].mode().iloc[0] if not grp[group_col].mode().empty else None
    return summed.index[0]


def build_identity_context(instances: pd.DataFrame, instance_conv: pd.DataFrame) -> pd.DataFrame:
    """
    For each identity, compute:
      match_type_summary       - dominant match_type (by conv), '*' suffix if mixed
      keyword_status_summary   - 'ENABLED' / 'PAUSED' / 'MIXED (...)' / staleness suffix
      ad_group_count           - distinct ad groups
      ad_groups_summary        - comma-joined names, truncated
      dominant_ad_group_status - status of ad group with most conv in window
    """
    inst = instances.merge(instance_conv, on="instance_id", how="left").fillna({"conv": 0})

    window_end = pd.Timestamp(WINDOW_END)
    stale_cutoff = window_end - pd.Timedelta(days=STALENESS_DAYS)

    rows = []
    for ident_id, grp in inst.groupby("identity_id"):
        # Match-type summary — dominant by conv, '*' if multiple distinct values exist.
        mts = grp["match_type"].dropna().unique()
        if len(mts) == 0:
            match_type_summary = "UNKNOWN"
        elif len(mts) == 1:
            match_type_summary = mts[0]
        else:
            dominant = _dominant_by_conv(grp, "match_type") or mts[0]
            match_type_summary = f"{dominant}*"

        # Keyword status summary — uniform vs mixed across instances.
        statuses = grp["keyword_status"].fillna("UNKNOWN").tolist()
        n_enabled = sum(1 for s in statuses if s == "ENABLED")
        n_paused = sum(1 for s in statuses if s == "PAUSED")
        n_other = len(statuses) - n_enabled - n_paused

        if n_enabled == len(statuses):
            base_status = "ENABLED"
        elif n_paused == len(statuses):
            base_status = "PAUSED"
        elif n_other == len(statuses):
            base_status = grp["keyword_status"].fillna("UNKNOWN").mode().iloc[0]
        else:
            parts = []
            if n_enabled:
                parts.append(f"{n_enabled} ENABLED")
            if n_paused:
                parts.append(f"{n_paused} PAUSED")
            if n_other:
                parts.append(f"{n_other} OTHER")
            base_status = f"MIXED ({' / '.join(parts)})"

        # Staleness check: only meaningful if base is ENABLED, and only if enabled.
        if base_status == "ENABLED" and STALENESS_ENABLED:
            enabled_mask = grp["keyword_status"] == "ENABLED"
            last_seen = grp.loc[enabled_mask, "last_seen_date"].max()
            if pd.notna(last_seen) and last_seen < stale_cutoff:
                keyword_status_summary = (
                    f"ENABLED (stale, last seen {last_seen.strftime('%Y-%m-%d')})"
                )
            else:
                keyword_status_summary = "ENABLED"
        else:
            keyword_status_summary = base_status

        # Ad group rollup
        ag_rows = grp.dropna(subset=["ad_group_id"])
        unique_ags = ag_rows.drop_duplicates(subset=["ad_group_id"])
        ad_group_count = len(unique_ags)

        if ad_group_count == 0:
            ad_groups_summary = "(none)"
            dominant_ad_group_status = "UNKNOWN"
        else:
            names = unique_ags["ad_group_name"].fillna("(unnamed)").tolist()
            joined = ", ".join(names)
            if len(joined) > AD_GROUPS_SUMMARY_MAX_LEN:
                ad_groups_summary = joined[: AD_GROUPS_SUMMARY_MAX_LEN - 3] + "..."
            else:
                ad_groups_summary = joined

            # Dominant ad group by conv in window. If all conv is zero, fall back
            # to instance count.
            ag_conv = ag_rows.groupby("ad_group_id", dropna=False).agg(
                conv=("conv", "sum"),
                n_instances=("instance_id", "count"),
                ad_group_status=("ad_group_status", "first"),
            ).reset_index()
            if ag_conv["conv"].sum() > 0:
                ag_conv = ag_conv.sort_values("conv", ascending=False)
            else:
                ag_conv = ag_conv.sort_values("n_instances", ascending=False)
            top = ag_conv.iloc[0]
            dominant_ad_group_status = top["ad_group_status"] if pd.notna(top["ad_group_status"]) else "UNKNOWN"

        rows.append({
            "identity_id": ident_id,
            "match_type_summary": match_type_summary,
            "keyword_status_summary": keyword_status_summary,
            "ad_group_count": ad_group_count,
            "ad_groups_summary": ad_groups_summary,
            "dominant_ad_group_status": dominant_ad_group_status,
        })

    return pd.DataFrame(rows)


CONTEXT_COLUMNS = [
    "match_type_summary",
    "keyword_status_summary",
    "ad_group_count",
    "ad_groups_summary",
    "dominant_ad_group_status",
]


def merge_context(df: pd.DataFrame, context: pd.DataFrame, after_col: str = "keyword") -> pd.DataFrame:
    """Merge context onto df by identity_id, placing context columns after `after_col`."""
    if "identity_id" not in df.columns:
        return df
    merged = df.merge(context, on="identity_id", how="left")
    cols = list(merged.columns)
    for c in CONTEXT_COLUMNS:
        if c in cols:
            cols.remove(c)
    insert_at = cols.index(after_col) + 1 if after_col in cols else len(cols)
    new_order = cols[:insert_at] + CONTEXT_COLUMNS + cols[insert_at:]
    return merged[new_order]


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

def _recent_volatility(weekly: pd.DataFrame, min_conv: int, sort_by: str) -> pd.DataFrame:
    """Per-identity weekly conv stats over the recent 6-week window.
    min_conv = identity-level conv threshold over the period.
    sort_by = 'cv' (volatility ranking) or 'total_conv_6w' (volume ranking).
    """
    recent = weekly[weekly["week_start"] >= pd.Timestamp(RECENT_START)].copy()

    weeks = sorted(recent["week_start"].unique())
    identities = recent.groupby(["identity_id", "keyword"], as_index=False)["conv"].sum()
    identities = identities[identities["conv"] >= min_conv]

    rows = []
    for _, ident in identities.iterrows():
        per_week = recent[recent["identity_id"] == ident["identity_id"]].set_index("week_start")["conv"]
        series = per_week.reindex(weeks, fill_value=0)
        mean = series.mean()
        std = series.std(ddof=0)
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

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.sort_values(sort_by, ascending=False, na_position="last").reset_index(drop=True)


def analysis_4_volatility(weekly: pd.DataFrame) -> pd.DataFrame:
    """Filtered to identities with >= MIN_CONV_RECENT (10) conv. Sorted by CV desc."""
    return _recent_volatility(weekly, min_conv=MIN_CONV_RECENT, sort_by="cv")


def analysis_4b_all_recent_active(weekly: pd.DataFrame) -> pd.DataFrame:
    """All identities with >=1 conv in the recent 6 weeks. Sorted by total conv desc."""
    return _recent_volatility(weekly, min_conv=1, sort_by="total_conv_6w")


# --------------------------------------------------------------------------
# Excel output
# --------------------------------------------------------------------------

SHEET_HEADERS = {
    "1_concentration": (
        "Weekly keyword concentration. Herfindahl = sum of squared identity shares "
        "of weekly conversions (full distribution, not just top 10). Higher = more "
        "concentrated. Pre/post block at top tests the rationing hypothesis directly. "
        f"Excludes keywords matching: {EXCLUDE_KEYWORD_PATTERNS}."
    ),
    "1b_top_keywords_by_week": (
        f"Top {TOP_N_KEYWORDS_PER_WEEK} keyword identities by conversions, per week. "
        "Use to eyeball which keywords are concentrated in spike weeks. "
        "Context columns (match_type_summary, keyword_status_summary, ad_group_count, "
        "ad_groups_summary, dominant_ad_group_status) reflect current state. "
        f"Excludes keywords matching: {EXCLUDE_KEYWORD_PATTERNS}."
    ),
    "2_churn": (
        "Week-over-week churn at identity level. dropouts = had conv in week N, none in N+1. "
        "new_entries = vice versa. churn_rate = (dropouts + new_entries) / total unique "
        "identities across the pair. Pre/post block at top. "
        f"Excludes keywords matching: {EXCLUDE_KEYWORD_PATTERNS}."
    ),
    "3_pre_post_troas": (
        f"Pre (Feb 9 - Mar 8, 4 wks) vs post (Mar 9 - May 3, 8 wks) tROAS comparison. "
        f"Threshold: >= {MIN_CONV_PRE_POST} conv in either period. pct_change_conv is "
        "computed on WEEKLY RATES (per-week conv) to control for asymmetric window length. "
        f"Top {TOP_N_MOVERS} by |pct_change_conv|. status flags new_entrant_post and dropped_out_post. "
        "Context columns reflect current state. "
        f"Excludes keywords matching: {EXCLUDE_KEYWORD_PATTERNS}."
    ),
    "3b_pre_post_excl_transition": (
        "Same as 3_pre_post_troas, but the Mar 9-15 transition week is excluded from the "
        "post period. Mar 9 was the worst-profit week (237 conv at $72 CPA, $-5,043 profit), "
        "likely a transition artifact as the new tROAS took effect mid-week. This sheet "
        "removes that distortion. Post period here is Mar 16 - May 3 (7 weeks). "
        f"Excludes keywords matching: {EXCLUDE_KEYWORD_PATTERNS}."
    ),
    "4_recent_volatility": (
        f"Coefficient of variation (stdev/mean) of weekly conversions, Mar 23 onward "
        f"(6 weeks). Filtered to identities with >= {MIN_CONV_RECENT} total conv in the period. "
        "Zero-conv weeks counted as zero. Top of list = identities most responsible for "
        "campaign-level week-to-week volume swings. Context columns reflect current state. "
        f"Excludes keywords matching: {EXCLUDE_KEYWORD_PATTERNS}."
    ),
    "4b_all_recent_active": (
        "Same window and metric as sheet 4, but UNFILTERED — every identity with >=1 conv "
        "in the recent 6 weeks. Sorted by total_conv_6w descending so you can scan from "
        "biggest contributors down through the long tail. CV may be blank for identities "
        "active in only one week (stdev undefined). Context columns reflect current state. "
        f"Excludes keywords matching: {EXCLUDE_KEYWORD_PATTERNS}."
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
        # Default width for all columns; specific text-heavy columns widened after data load.
        for col_idx in range(2, 25):
            from openpyxl.utils import get_column_letter
            ws.column_dimensions[get_column_letter(col_idx)].width = 16

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

        # Widen text-heavy columns based on header names.
        from openpyxl.utils import get_column_letter
        wide_cols = {"keyword": 32, "ad_groups_summary": 40, "keyword_status_summary": 28,
                     "match_type_summary": 14, "dominant_ad_group_status": 14, "status": 18,
                     "week_pair": 26}
        for cell in ws[header_row]:
            if cell.value in wide_cols:
                ws.column_dimensions[get_column_letter(cell.column)].width = wide_cols[cell.value]

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
    if EXCLUDE_KEYWORD_PATTERNS:
        print(f"Excluded keyword patterns: {EXCLUDE_KEYWORD_PATTERNS}")
        print("(Pre/post Herfindahl numbers will differ slightly from prior runs that "
              "included these.)")

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
            print(DAILY_KEYWORD_SQL.format(exclusion_clause=_build_exclusion_clause()))
            print("-" * 72)
            print(f"Params: campaign_id={CAMPAIGN_ID}, account_id={ACCOUNT_ID}, "
                  f"window={WINDOW_START}..{WINDOW_END}")
            print(f"Excluded patterns: {EXCLUDE_KEYWORD_PATTERNS or '(none)'}")
            daily = fetch_daily(conn)
            print(f"Daily rows fetched: {len(daily):,}")
            print(f"Distinct identities: {daily['identity_id'].nunique():,}")
            print(f"Distinct weeks: {daily['week_start'].nunique()}")
            print(f"Date range: {daily['report_date'].min()} to {daily['report_date'].max()}")
            print(f"Total conv: {daily['conv'].sum():,.0f}")
            print(f"Total cost: ${daily['cost'].sum():,.2f}")
            print()
            instances = fetch_instance_context(conn)
            instance_conv = fetch_instance_conv(conn)
            identity_context = build_identity_context(instances, instance_conv)
            print(f"Instance context rows: {len(instances):,} "
                  f"(across {instances['identity_id'].nunique():,} identities)")
            print(f"Multi-instance identities: "
                  f"{(instances.groupby('identity_id').size() > 1).sum():,}")
            print(f"Multi-ad-group identities: "
                  f"{(identity_context['ad_group_count'] > 1).sum():,}")
            print(f"Mixed-status identities: "
                  f"{identity_context['keyword_status_summary'].str.startswith('MIXED').sum():,}")
            if STALENESS_ENABLED:
                print(f"Stale-but-enabled identities: "
                      f"{identity_context['keyword_status_summary'].str.contains('stale').sum():,}")
            else:
                print("Staleness flag: DISABLED (last_seen_date semantics unconfirmed)")
            return

        daily = fetch_daily(conn)
        if daily.empty:
            print("ERROR: no rows returned from main query. Check campaign_id, account_id, date window.",
                  file=sys.stderr)
            sys.exit(1)

        # Per-identity context (match_type, statuses, ad groups). Built once and
        # merged onto the four keyword-bearing sheets.
        instances = fetch_instance_context(conn)
        instance_conv = fetch_instance_conv(conn)
        identity_context = build_identity_context(instances, instance_conv)

        weekly = aggregate_weekly_identity(daily)

        # Run analyses
        conc_summary, conc_pp_block, top_kw_by_week = analysis_1_concentration(weekly)
        churn, churn_pp_block = analysis_2_churn(weekly)
        movers = analysis_3_pre_post(daily)
        movers_excl = analysis_3b_pre_post_excl_transition(daily)
        volatility = analysis_4_volatility(weekly)
        all_recent = analysis_4b_all_recent_active(weekly)

        # Merge context onto the keyword sheets.
        top_kw_by_week = merge_context(top_kw_by_week, identity_context, after_col="keyword")
        movers = merge_context(movers, identity_context, after_col="keyword")
        movers_excl = merge_context(movers_excl, identity_context, after_col="keyword")
        volatility = merge_context(volatility, identity_context, after_col="keyword")
        all_recent = merge_context(all_recent, identity_context, after_col="keyword")

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
            "4b_all_recent_active": all_recent,
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
