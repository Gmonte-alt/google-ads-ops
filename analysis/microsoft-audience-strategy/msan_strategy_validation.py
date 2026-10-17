"""
msan_strategy_validation.py
===========================
Read-only validation analysis on paid_search.db, Microsoft Home (account_id=2, F149SN63),
to put numbers behind sections 1-2 of the MSAN rebuild strategy.

Modules
  A - Search -> Audience lead echo (weekly cross-correlation, raw + detrended, decay view)
  B - Conquest vs Generic vs Affinity/Pool by network (ad-group grain, Path B)
  C - RPL parity by state x network (tier-transfer check)
  D - WoW GP-decline decomposition (network split + conquest concentration)
  E - tCPA blind spot (RPL dispersion across ad-group types on audience)

Output: msan_strategy_validation.xlsx (one sheet per module + Params_QA).

Run (Windows, paid-search conda env):
  "C:\\ProgramData\\anaconda3\\envs\\paid-search\\python.exe" msan_strategy_validation.py

Conventions enforced (per brief):
  * device='ALL' on every aggregation
  * primary_conv_count / primary_conv_value where they exist (keyword + geo tables)
  * cost_micros / 1e6 -> dollars
  * network bucket: 'Audience' -> audience; everything else -> search
  * revenue-lag cutoff: trailing REVENUE_LAG_DAYS excluded everywhere
  * DB opened read-only (mode=ro)
  * all ratios computed from summed base measures, never averaged ratios
"""

import os
import json
import re
import sqlite3
import sys
import traceback
from datetime import timedelta

import numpy as np
import pandas as pd

# ----------------------------------------------------------------------------
# PARAMS - edit here
# ----------------------------------------------------------------------------
PARAMS = {
    "db_path": r"C:\Projects\google-ads-ops\db\paid_search.db",
    "output_path": r"C:\Projects\google-ads-ops\analysis\microsoft-audience-strategy\output\msan_strategy_validation.xlsx",
    "account_id": 2,                    # Microsoft Home (F149SN63)

    "revenue_lag_days": 2,              # exclude trailing N days (offline revenue lag)

    # Revenue definition (keyword network table). Account-wide convention:
    # Leads = primary conversions (zip-to-quote), Revenue = ALL revenue
    # (offline-inclusive) - Microsoft offline revenue posts into all_conv_value,
    # not the pixel-time primary_conv_value. Using primary_conv_value understates
    # revenue massively (~50-70%) and makes the whole account look GP-negative.
    "kw_revenue_col": "all_conv_value",

    # Revenue-certified window: keyword/geo value columns were only populated
    # from ~spring 2026 (reconciliation: kw-vs-pair ratio 1.00 for May-Jul but
    # near-zero before). All $-figures (B/C/E) are computed on this window;
    # full-history blocks remain for CvR/volume only.
    "reconciled_start": "2026-05-01",

    # Module A
    "a_start_date": "2025-01-01",       # full-history window start for the echo analysis
    "consolidation_date": "2026-01-01", # 2025->2026 campaign consolidation (structural break);
                                        # adjust to the actual consolidation date if known
    "max_lag_weeks": 3,
    "max_lag_days": 14,                 # daily-grain cross-correlation range
    "spike_top_n": 6,                   # top-N search WoW spikes for the decay view
                                        # (directional view - cross-corr is the robust part)

    # Module C
    "c_min_leads_per_cell": 30,         # min primary leads per state x network cell
    "c_parity_band_pct": 15.0,          # "within parity" = |audience RPL - search RPL| <= X%

    # Module D
    "d_lookback_weeks": 16,             # search window for the largest WoW GP decline

    # Alarm backtest (Module A4) - Level 1 = search-down week AND audience
    # impressions > impr_mult x trailing avg AND CvR < cvr_mult x trailing avg.
    # Thresholds are defined against the TRAILING 4-WEEK baseline.
    "a4_impr_mult": 1.5,
    "a4_cvr_mult": 0.75,
    "a4_trailing_weeks": 4,
    "a4_min_trailing_weeks": 3,
    "a4_min_aud_spend": 50,

    # R1 recapture baseline (co-moving definition; frozen on first compute)
    "r1_launch_excl_weeks": 2,       # exclude first N weeks post-consolidation/launch
    # The JUNE consolidation (state campaigns -> tiers) anchors the R1 launch
    # exclusion. Separate from consolidation_date (the Jan structural break used
    # by Module A's cross-correlation split) - changing that would silently move
    # the locked A_Echo_Lag citation.
    "r1_consolidation_date": "2026-06-01",
    # Hardcoded co-moving exclusions per tier (visible spec amendments):
    # T2 6/15 = anomaly week (locked spec); T2 7/06 = half-dark transition week
    # (audience being cut mid-week - neither co-moving nor dark).
    "r1_hardcoded_excl_weeks": {"T2": ["2026-06-15", "2026-07-06"]},
    # Authoritative first-raise date for the T2 holdout clean window when
    # campaign_change_events does not carry the tCPA changes (e.g. "2026-07-13").
    "t2_first_raise_override": None,
    "r1_contraction_pct": 20.0,      # exclude weeks with search rev down > X% WoW
    "frozen_dir": r"C:\Projects\google-ads-ops\analysis\microsoft-audience-strategy\frozen",
    "r1_freeze_file": "r1_baseline_frozen.json",   # sidecar, created once
    "r1_force_refreeze": False,      # True = deliberately re-lock the baseline
    "t2_snapshot_file": "t2_holdout_snapshot.xlsx",  # created once, never overwritten
    "a4_newcid_min_trailing": 4,     # arming guard for new MSAN program campaigns
    "msan_cutover_overrides": {},    # {"campaign name substring": "YYYY-MM-DD"} - authoritative

    # QA
    "spend_qa_days": 28,                # trailing window for the manual spend reconcile
}

AUDIENCE_NET = "Audience"
DATE_CANDIDATES = ["date", "report_date", "day", "event_date", "stat_date"]

QA_ROWS = []   # (check, result, detail)


def qa(check, result, detail=""):
    QA_ROWS.append({"check": check, "result": str(result), "detail": str(detail)})
    print(f"[QA] {check}: {result}  {detail}")


# ----------------------------------------------------------------------------
# Introspection helpers
# ----------------------------------------------------------------------------
def table_cols(conn, table):
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return [r[1] for r in rows]
    except sqlite3.Error:
        return []


def pick_date_col(cols, table):
    for c in DATE_CANDIDATES:
        if c in cols:
            return c
    raise RuntimeError(f"{table}: no date column found among {DATE_CANDIDATES} (cols={cols})")


def scope_clause(conn, table, cols, alias="t"):
    """Return (join_sql, where_sql) that scopes `table` to PARAMS['account_id'].
    Prefers a direct account_id column; falls back to a join through campaigns."""
    acct = PARAMS["account_id"]
    if "account_id" in cols:
        return "", f"{alias}.account_id = {acct}"
    if "campaign_id" in cols:
        camp_cols = table_cols(conn, "campaigns")
        if "account_id" in camp_cols:
            return (f"JOIN campaigns c ON c.campaign_id = {alias}.campaign_id",
                    f"c.account_id = {acct}")
    raise RuntimeError(f"{table}: cannot scope to account {acct} "
                       f"(no account_id, no campaign_id->campaigns path)")


def ki_scope(conn, ki_cols):
    """Scoping for keyword_instances (aliased ki)."""
    acct = PARAMS["account_id"]
    if "account_id" in ki_cols:
        return "", f"ki.account_id = {acct}"
    if "campaign_id" in ki_cols:
        camp_cols = table_cols(conn, "campaigns")
        if "account_id" in camp_cols:
            return ("JOIN campaigns c ON c.campaign_id = ki.campaign_id",
                    f"c.account_id = {acct}")
    raise RuntimeError("keyword_instances: cannot scope to account "
                       "(no account_id, no campaign_id->campaigns path)")


KI_KEY_CANDIDATES = ["instance_id", "id", "keyword_instance_id", "kw_instance_id"]


def ki_join_key(ki_cols):
    """The keyword fact tables FK on instance_id; find the matching key column
    on keyword_instances itself (it is not necessarily named instance_id)."""
    for c in KI_KEY_CANDIDATES:
        if c in ki_cols:
            return c
    raise RuntimeError(f"keyword_instances: no join key found among "
                       f"{KI_KEY_CANDIDATES} (cols={ki_cols})")


def bucket_sql(alias="t"):
    return (f"CASE WHEN {alias}.network = '{AUDIENCE_NET}' "
            f"THEN 'audience' ELSE 'search' END")


def geo_total_filter(conn, table, cols, join, where):
    """If `table` has a geo_state dimension, return the AND-clause that yields
    account totals without double-counting. Detects whether a synthesized
    geo_state='ALL' rollup exists (same pattern as device='ALL')."""
    if "geo_state" not in cols:
        return "", "no geo dimension"
    n_all = conn.execute(
        f"SELECT COUNT(*) FROM {table} t {join} "
        f"WHERE {where} AND t.geo_state = 'ALL'").fetchone()[0]
    if n_all > 0:
        return " AND t.geo_state = 'ALL'", "geo_state='ALL' rollup detected -> using it"
    return "", "no 'ALL' rollup -> summing per-state rows (states partition the total)"


# ----------------------------------------------------------------------------
# Ad-group classification (ORDER MATTERS - identical to sibling carrier-GP analysis)
# ----------------------------------------------------------------------------
def classify_ad_group(name):
    n = (name or "").lower()
    if "costco" in n:
        return "affinity"                          # 1. carve out BEFORE carrier bucketing
    if ("home-group" in n or "home insurance-group" in n
            or n.endswith("-group") or "pooled" in n):
        return "pool"                              # 2. pooled ad groups are NOT conquest
    if "nongeneric" in n or "non-generic" in n:
        return "conquest"                          # 3. Home-NonGeneric-<Carrier>
    if "generic" in n:
        return "generic"                           # 4. Home-Generic-...
    return "other"


# ----------------------------------------------------------------------------
# Ratio helpers (always from summed bases)
# ----------------------------------------------------------------------------
def add_ratios(df):
    df = df.copy()
    df["cvr_pct"] = np.where(df["clicks"] > 0, df["leads"] / df["clicks"] * 100, np.nan)
    df["cpl"] = np.where(df["leads"] > 0, df["spend"] / df["leads"], np.nan)
    df["rpl"] = np.where(df["leads"] > 0, df["revenue"] / df["leads"], np.nan)
    df["gp"] = df["revenue"] - df["spend"]
    df["gp_per_lead"] = np.where(df["leads"] > 0, df["gp"] / df["leads"], np.nan)
    df["roas"] = np.where(df["spend"] > 0, df["revenue"] / df["spend"], np.nan)
    return df


def week_start(s):
    d = pd.to_datetime(s)
    return (d - pd.to_timedelta(d.dt.weekday, unit="D")).dt.date


# ----------------------------------------------------------------------------
# Data pulls
# ----------------------------------------------------------------------------
def pull_keyword_network(conn):
    """Ad-group grain by network bucket from keyword_performance_network_daily (Path B)."""
    t = "keyword_performance_network_daily"
    cols = table_cols(conn, t)
    if not cols:
        raise RuntimeError(f"{t}: table not found")
    dcol = pick_date_col(cols, t)
    if "instance_id" not in cols:
        raise RuntimeError(f"{t}: expected FK instance_id not found (cols={cols})")
    for req in ("primary_conv_count", "primary_conv_value"):
        if req not in cols:
            raise RuntimeError(f"{t}: missing {req} - refusing to fall back to "
                               f"all-conversions (cols={cols})")
    ki_cols = table_cols(conn, "keyword_instances")
    if "ad_group_name" not in ki_cols:
        raise RuntimeError(f"keyword_instances: ad_group_name not found (cols={ki_cols})")
    join, where = ki_scope(conn, ki_cols)
    kkey = ki_join_key(ki_cols)
    kw_join = f"JOIN keyword_instances ki ON ki.{kkey} = t.instance_id {join}"
    geo_and, geo_note = geo_total_filter(conn, t, cols, kw_join, where)
    rev_col = PARAMS["kw_revenue_col"]
    if rev_col not in cols:
        raise RuntimeError(f"{t}: configured kw_revenue_col '{rev_col}' not found "
                           f"(cols={cols})")
    qa("B source columns", "ok",
       f"{t}: date={dcol}, leads=primary_conv_count, revenue={rev_col}, "
       f"ki join key = ki.{kkey}")
    qa("B geo-rollup handling (keyword table)", "info", geo_note)

    # revenue-definition sanity: implied account RPL under both candidate columns
    if "all_conv_value" in cols and "primary_conv_value" in cols:
        rd = pd.read_sql_query(f"""
            SELECT SUM(t.primary_conv_count) AS leads,
                   SUM(t.primary_conv_value) AS rev_primary,
                   SUM(t.all_conv_value)     AS rev_all
            FROM {t} t {kw_join}
            WHERE {where} AND t.device = 'ALL'{geo_and}
              AND t.{dcol} >= '{PARAMS["reconciled_start"]}'
        """, conn)
        L = rd["leads"][0] or np.nan
        qa("Revenue-definition check (reconciled window)", f"using {rev_col}",
           f"implied account RPL since {PARAMS['reconciled_start']}: "
           f"primary_conv_value=${rd['rev_primary'][0]/L:.2f} vs "
           f"all_conv_value=${rd['rev_all'][0]/L:.2f}")

    if "campaign_name" in ki_cols:
        camp_sel, camp_join = "ki.campaign_name", ""
    elif ("campaign_id" in ki_cols
          and "campaign_name" in table_cols(conn, "campaigns")):
        camp_sel = "cc.campaign_name"
        camp_join = "LEFT JOIN campaigns cc ON cc.campaign_id = ki.campaign_id"
    else:
        camp_sel, camp_join = "'(unknown)'", ""
    sql = f"""
        SELECT t.{dcol} AS date,
               ki.ad_group_name,
               {camp_sel} AS campaign_name,
               {bucket_sql()} AS bucket,
               SUM(t.impressions)            AS impressions,
               SUM(t.clicks)                 AS clicks,
               SUM(t.cost_micros)/1e6        AS spend,
               SUM(t.primary_conv_count)     AS leads,
               SUM(t.{rev_col})              AS revenue
        FROM {t} t
        JOIN keyword_instances ki ON ki.{kkey} = t.instance_id
        {join}
        {camp_join}
        WHERE {where}
          AND t.device = 'ALL'{geo_and}
        GROUP BY 1, 2, 3, 4
    """
    df = pd.read_sql_query(sql, conn)
    df["date"] = pd.to_datetime(df["date"])
    df["ad_group_type"] = df["ad_group_name"].map(classify_ad_group)
    return df


def pull_geo_state(conn, cutoff, kw):
    """State x network bucket from geo_performance_daily (per db architect: the
    keyword tables carry no state dimension - geo_state is a constant 'ALL').
    geo_performance_daily lacks primary_conv_* here, so fall back to its generic
    conversions/conversion_value: for a parity RATIO (audience RPL vs search RPL)
    the conversion definition holds as long as both networks use the same one.
    A cross-check against keyword-table primary leads tests whether the generic
    column is in fact primary (as the campaign table's proved to be).
    Returns (df, gate_msg, meta)."""
    t = "geo_performance_daily"
    meta = {"conv_def": "", "xcheck": pd.DataFrame()}
    cols = table_cols(conn, t)
    if not cols:
        return None, f"{t}: table not found - Module C cannot run", meta
    missing = [c for c in ("network", "geo_state") if c not in cols]
    if missing:
        return None, f"{t}: missing {missing} - no state x network split available", meta
    dcol = pick_date_col(cols, t)
    join, where = scope_clause(conn, t, cols)

    # conversion definition: primary if present, else generic (labeled)
    if "primary_conv_count" in cols and "primary_conv_value" in cols:
        lead_col, val_col = "primary_conv_count", "primary_conv_value"
        meta["conv_def"] = "primary_conv_* (zip-to-quote)"
    elif "conversions" in cols and "conversion_value" in cols:
        lead_col, val_col = "conversions", "conversion_value"
        meta["conv_def"] = ("generic conversions/conversion_value (primary_conv_* absent) "
                            "- RPL parity ratio valid if definition is consistent across "
                            "networks; see cross-check")
    else:
        return None, f"{t}: no usable conversion columns (cols={cols})", meta

    dev_and = " AND t.device = 'ALL'" if "device" in cols else ""
    if not dev_and:
        qa("C device handling", "note", f"{t} has no device column - no rollup to filter")

    # geo_state actually populated with real states?
    chk = pd.read_sql_query(
        f"""SELECT COUNT(*) AS n,
                   SUM(CASE WHEN t.geo_state IS NULL OR t.geo_state IN ('', 'ALL')
                       THEN 1 ELSE 0 END) AS n_blank
            FROM {t} t {join} WHERE {where}""", conn)
    n, n_blank = int(chk["n"][0]), int(chk["n_blank"][0])
    if n == 0 or n_blank == n:
        return None, (f"{t}: geo_state carries no real states for account "
                      f"(rows={n}, blank/ALL={n_blank})"), meta

    # cross-check: is the geo table's lead column primary? (last full month, audience)
    kmax = min(kw["date"].max(), cutoff)
    m_end = pd.Timestamp(kmax.year, kmax.month, 1) - timedelta(days=1)
    m_start = pd.Timestamp(m_end.year, m_end.month, 1)
    g_aud = pd.read_sql_query(f"""
        SELECT SUM(t.{lead_col}) AS leads FROM {t} t {join}
        WHERE {where}{dev_and} AND t.network = '{AUDIENCE_NET}'
          AND t.{dcol} BETWEEN '{m_start.date()}' AND '{m_end.date()}'
    """, conn)["leads"][0] or 0
    k_aud = kw[(kw["bucket"] == "audience")
               & kw["date"].between(m_start, m_end)]["leads"].sum()
    ratio = g_aud / k_aud if k_aud else np.nan
    verdict = ("PASS - geo leads = primary; RPL levels AND ratios usable"
               if (k_aud and 0.9 <= ratio <= 1.1)
               else "DIVERGENT - geo leads are all-conversions; use RPL parity "
                    "RATIOS only, not levels")
    qa("C conv-definition cross-check", verdict.split(" - ")[0],
       f"{m_start.date()}..{m_end.date()} audience: geo {lead_col}={g_aud:,.0f} vs "
       f"keyword primary={k_aud:,.0f} (ratio={ratio:.3f})")
    meta["xcheck"] = pd.DataFrame([{
        "window": f"{m_start.date()}..{m_end.date()}",
        f"geo_{lead_col}_audience": g_aud,
        "keyword_primary_conv_audience": k_aud,
        "ratio": ratio, "verdict": verdict,
    }])

    sql = f"""
        SELECT t.geo_state,
               {bucket_sql()} AS bucket,
               SUM(t.cost_micros)/1e6    AS spend,
               SUM(t.{lead_col})         AS leads,
               SUM(t.{val_col})          AS revenue,
               MIN(t.{dcol})             AS min_date,
               MAX(t.{dcol})             AS max_date
        FROM {t} t
        {join}
        WHERE {where}{dev_and}
          AND t.geo_state IS NOT NULL
          AND t.geo_state NOT IN ('', 'ALL')
          AND t.{dcol} >= '{PARAMS["reconciled_start"]}'
          AND t.{dcol} <= '{cutoff.date().isoformat()}'
        GROUP BY 1, 2
    """
    df = pd.read_sql_query(sql, conn)
    return df, (f"ok - sourced from {t} using {meta['conv_def'].split(' - ')[0].split(' (')[0]} "
                f"(rows={n}, blank/ALL rows excluded={n_blank})"), meta


# ----------------------------------------------------------------------------
# Campaign-grain network pair (per db architect, penny-reconciled to geo)
# ----------------------------------------------------------------------------
def pull_campaign_pair(conn):
    """campaign_performance_daily = SEARCH ONLY by design (no network column);
    campaign_performance_network_daily = AUDIENCE (+ trace syndicated -> search).
    Union the pair, tagged. Revenue = conversion_value; leads = conversions
    (confirmed = primary zip-to-quote). Verified by architect: search + audience
    reconciles to geo_performance_daily within $1/month."""
    frames = []
    t = "campaign_performance_daily"
    cols = table_cols(conn, t)
    if not cols:
        raise RuntimeError(f"{t}: table not found")
    dcol = pick_date_col(cols, t)
    join, where = scope_clause(conn, t, cols)
    dev_and = " AND t.device = 'ALL'" if "device" in cols else ""
    geo_and, note = geo_total_filter(conn, t, cols, join, where)
    qa("Pair search side", "ok",
       f"{t} (search-only by design): date={dcol}; device filter="
       f"{'yes' if dev_and else 'n/a'}; {note}")
    frames.append(pd.read_sql_query(f"""
        SELECT t.{dcol} AS date, 'search' AS bucket,
               SUM(t.clicks) AS clicks, SUM(t.cost_micros)/1e6 AS spend,
               SUM(t.conversions) AS leads, SUM(t.conversion_value) AS revenue
        FROM {t} t {join} WHERE {where}{dev_and}{geo_and} GROUP BY 1
    """, conn))

    t = "campaign_performance_network_daily"
    cols = table_cols(conn, t)
    dcol = pick_date_col(cols, t)
    join, where = scope_clause(conn, t, cols)
    dev_and = " AND t.device = 'ALL'" if "device" in cols else ""
    frames.append(pd.read_sql_query(f"""
        SELECT t.{dcol} AS date, {bucket_sql()} AS bucket,
               SUM(t.clicks) AS clicks, SUM(t.cost_micros)/1e6 AS spend,
               SUM(t.conversions) AS leads, SUM(t.conversion_value) AS revenue
        FROM {t} t {join} WHERE {where}{dev_and} GROUP BY 1, 2
    """, conn))
    df = pd.concat(frames, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"])
    return df.groupby(["date", "bucket"], as_index=False).sum(numeric_only=True)


def revenue_reconciliation(conn, pair, kw, cutoff, n_months=3):
    """Cross-grain revenue reconciliation for the last N full months:
    campaign pair (search+audience) vs geo_performance_daily vs the keyword
    network table. Adjudicates whether keyword-grain dollars are usable.
    Returns (df, kw_dollars_ok)."""
    t = "geo_performance_daily"
    cols = table_cols(conn, t)
    gdcol = pick_date_col(cols, t)
    gjoin, gwhere = scope_clause(conn, t, cols)
    gdev = " AND t.device = 'ALL'" if "device" in cols else ""
    rows = []
    m_end = pd.Timestamp(cutoff.year, cutoff.month, 1) - timedelta(days=1)
    for _ in range(n_months):
        m_start = pd.Timestamp(m_end.year, m_end.month, 1)
        p = pair[pair["date"].between(m_start, m_end)]
        s_rev = p[p["bucket"] == "search"]["revenue"].sum()
        a_rev = p[p["bucket"] == "audience"]["revenue"].sum()
        g_rev = pd.read_sql_query(f"""
            SELECT SUM(t.conversion_value) AS r FROM {t} t {gjoin}
            WHERE {gwhere}{gdev}
              AND t.{gdcol} BETWEEN '{m_start.date()}' AND '{m_end.date()}'
        """, conn)["r"][0] or 0.0
        k = kw[kw["date"].between(m_start, m_end)]
        rows.append({
            "month": m_start.strftime("%Y-%m"),
            "pair_search_rev": s_rev, "pair_audience_rev": a_rev,
            "pair_total": s_rev + a_rev, "geo_rev": g_rev,
            "pair_vs_geo_delta": (s_rev + a_rev) - g_rev,
            "kw_table_rev": k["revenue"].sum(),
            "kw_vs_pair_ratio": (k["revenue"].sum() / (s_rev + a_rev)
                                 if (s_rev + a_rev) else np.nan),
            "pair_leads": p["leads"].sum(), "kw_leads": k["leads"].sum(),
        })
        m_end = m_start - timedelta(days=1)
    df = pd.DataFrame(rows)
    pair_geo_ok = (df["pair_vs_geo_delta"].abs()
                   <= 0.05 * df["geo_rev"].clip(lower=1)).all()
    kw_ok = df["kw_vs_pair_ratio"].between(0.9, 1.1).all()
    qa("Revenue reconciliation (pair vs geo)",
       "RECONCILES" if pair_geo_ok else "MISMATCH - investigate",
       "; ".join(f"{r['month']}: pair ${r['pair_total']:,.0f} vs geo "
                 f"${r['geo_rev']:,.0f}" for _, r in df.iterrows()))
    qa("Keyword-table dollar verdict",
       "USABLE - kw revenue matches pair" if kw_ok else
       "NOT ACCOUNT-COMPLETE - kw $-figures (B/E GP, D concentration $) are "
       "indicative only; lead/CvR figures unaffected",
       "; ".join(f"{r['month']}: ratio {r['kw_vs_pair_ratio']:.2f}"
                 for _, r in df.iterrows()))
    return df, kw_ok



def pull_campaign_pair_by_campaign(conn):
    """Campaign-grain version of the pair, with campaign names, for the tier
    break-out (module A2)."""
    camp_cols = table_cols(conn, "campaigns")
    name_col = "campaign_name" if "campaign_name" in camp_cols else None
    frames = []
    t = "campaign_performance_daily"
    cols = table_cols(conn, t)
    dcol = pick_date_col(cols, t)
    join, where = scope_clause(conn, t, cols)
    if not join and name_col:
        join = "JOIN campaigns c ON c.campaign_id = t.campaign_id"
    dev_and = " AND t.device = 'ALL'" if "device" in cols else ""
    geo_and, _ = geo_total_filter(conn, t, cols, join, where)
    nm = f"c.{name_col}" if name_col else "CAST(t.campaign_id AS TEXT)"
    frames.append(pd.read_sql_query(f"""
        SELECT t.{dcol} AS date, t.campaign_id, {nm} AS campaign_name,
               'search' AS bucket, SUM(t.cost_micros)/1e6 AS spend,
               SUM(t.conversions) AS leads, SUM(t.conversion_value) AS revenue
        FROM {t} t {join} WHERE {where}{dev_and}{geo_and} GROUP BY 1, 2, 3
    """, conn))
    t = "campaign_performance_network_daily"
    cols = table_cols(conn, t)
    dcol = pick_date_col(cols, t)
    join, where = scope_clause(conn, t, cols)
    if not join and name_col:
        join = "JOIN campaigns c ON c.campaign_id = t.campaign_id"
    dev_and = " AND t.device = 'ALL'" if "device" in cols else ""
    frames.append(pd.read_sql_query(f"""
        SELECT t.{dcol} AS date, t.campaign_id, {nm} AS campaign_name,
               {bucket_sql()} AS bucket, SUM(t.cost_micros)/1e6 AS spend,
               SUM(t.conversions) AS leads, SUM(t.conversion_value) AS revenue
        FROM {t} t {join} WHERE {where}{dev_and} GROUP BY 1, 2, 3, 4
    """, conn))
    df = pd.concat(frames, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"])
    return df


def module_a2(pc_all, cutoff):
    """Tier break-out: per-campaign weekly search vs audience with the audience
    ROAS trajectory, plus the direct test of the restated thesis - does
    audience ROAS at week t correlate with the search revenue CHANGE at
    weeks t-k (economics decay after search declines)?"""
    pc = pc_all[(pc_all["date"] >= pd.Timestamp(PARAMS["reconciled_start"]))
                & (pc_all["date"] <= cutoff)].copy()
    pc["week"] = week_start(pc["date"])

    spend_rank = (pc.groupby("campaign_name")["spend"].sum()
                    .sort_values(ascending=False))
    tiers = [n for n in spend_rank.index
             if "tier" in str(n).lower() or classify_campaign_program(n)[0]]
    picks = (tiers or list(spend_rank.index))[:8]
    qa("A2 campaigns", f"{len(picks)} selected",
       "; ".join(str(p)[:45] for p in picks))

    weekly_tables, lag_rows = {}, []

    def build(frame, label):
        w = (frame.groupby(["week", "bucket"])
                  .agg(spend=("spend", "sum"), leads=("leads", "sum"),
                       revenue=("revenue", "sum")).reset_index())
        piv = w.pivot(index="week", columns="bucket",
                      values=["revenue", "spend", "leads"]).fillna(0.0).sort_index()
        out = pd.DataFrame(index=piv.index)
        for m in ("revenue", "spend", "leads"):
            for b in ("search", "audience"):
                out[f"{b[:3]}_{m[:3]}"] = piv.get((m, b), 0.0)
        out["aud_gp"] = out["aud_rev"] - out["aud_spe"]
        out["aud_roas"] = np.where(out["aud_spe"] > 0,
                                   out["aud_rev"] / out["aud_spe"], np.nan)
        out["aud_share_pct"] = np.where(
            (out["sea_rev"] + out["aud_rev"]) > 0,
            out["aud_rev"] / (out["sea_rev"] + out["aud_rev"]) * 100, np.nan)
        out["search_rev_wow"] = out["sea_rev"].diff()
        out["wk_label"] = [f"{d.month}/{d.day}" for d in out.index]
        # thesis test: audience ROAS[t] vs search revenue change[t-k]
        roas = out["aud_roas"].where(out["aud_spe"] >= 50)   # ignore near-dark weeks
        for k in range(0, 3):
            x = out["search_rev_wow"].shift(k)
            n = int((roas.notna() & x.notna()).sum())
            lag_rows.append({"campaign": label, "lag_weeks_k": k,
                             "corr_audROAS_vs_searchWoW": roas.corr(x),
                             "n_weeks": n})
        return out.reset_index().rename(columns={"week": "week_start"})

    for p in picks:
        weekly_tables[p] = build(pc[pc["campaign_name"] == p], str(p)[:45])
    weekly_tables["ACCOUNT (all campaigns)"] = build(pc, "ACCOUNT")

    lag = pd.DataFrame(lag_rows)
    pos = lag[(lag["lag_weeks_k"] > 0) & lag["corr_audROAS_vs_searchWoW"].notna()]
    if len(pos):
        qa("A2 thesis test", "see A2 sheet",
           f"median corr(aud ROAS[t], search WoW[t-k]) across campaigns: "
           f"k=1 {pos[pos.lag_weeks_k==1]['corr_audROAS_vs_searchWoW'].median():.2f}, "
           f"k=2 {pos[pos.lag_weeks_k==2]['corr_audROAS_vs_searchWoW'].median():.2f} "
           f"(positive = ROAS falls after search falls)")
    return {"weekly": weekly_tables, "lag": lag}


def pull_change_events(conn, wk_p, wk_t):
    """Budget/tCPA/etc changes around the decline window (architect: rule out a
    self-inflicted search drop before the strategy leans on the episode)."""
    t = "campaign_change_events"
    cols = table_cols(conn, t)
    if not cols:
        return pd.DataFrame([{"note": f"{t} not found - cannot check for "
                              "self-inflicted decline"}])
    dcol = None
    for c in ("change_date", "date", "event_date", "change_datetime",
              "changed_at", "timestamp", "report_date", "day"):
        if c in cols:
            dcol = c
            break
    if dcol is None:
        return pd.DataFrame([{"note": f"{t}: no recognizable date column "
                              f"(cols={cols})"}])
    try:
        join, where = scope_clause(conn, t, cols)
    except RuntimeError:
        join, where = "", "1=1"
    start = (pd.Timestamp(wk_p) - timedelta(days=7)).date()
    end = (pd.Timestamp(wk_t) + timedelta(days=6)).date()
    keep = [c for c in cols if c != dcol][:8]
    sel = ", ".join(f"t.{c}" for c in keep)
    df = pd.read_sql_query(f"""
        SELECT t.{dcol} AS change_date{', ' + sel if sel else ''}
        FROM {t} t {join}
        WHERE {where} AND t.{dcol} BETWEEN '{start}' AND '{end}'
        ORDER BY t.{dcol} LIMIT 60
    """, conn)
    qa("D change-events in decline window",
       f"{len(df)} events {start}..{end}",
       "review for budget/tCPA changes that would explain the search drop")
    return df


# ----------------------------------------------------------------------------
# Module A - echo / lag
# ----------------------------------------------------------------------------
def weekly_leads(camp_df, cutoff, start=None):
    d = camp_df[camp_df["date"] <= cutoff].copy()
    if start is not None:
        d = d[d["date"] >= pd.Timestamp(start)]
    d["week"] = week_start(d["date"])
    w = (d.groupby(["week", "bucket"])["leads"].sum()
          .unstack("bucket").fillna(0.0).sort_index())
    for b in ("search", "audience"):
        if b not in w.columns:
            w[b] = 0.0
    # drop a trailing partial week (week containing the cutoff, if incomplete)
    if len(w) and (cutoff.date() - w.index[-1]).days < 6:
        w = w.iloc[:-1]
    return w[["search", "audience"]]


def xcorr_table(w, max_lag, label):
    """k > 0: audience LAGS search; k < 0: audience LEADS search."""
    rows = []
    a_raw, s_raw = w["audience"], w["search"]
    a_d, s_d = a_raw.diff(), s_raw.diff()
    for k in range(-max_lag, max_lag + 1):
        rows.append({
            "period": label,
            "lag_weeks_k": k,
            "corr_raw_levels": a_raw.corr(s_raw.shift(k)),
            "corr_first_diff": a_d.corr(s_d.shift(k)),
            "n_weeks": int(min(a_raw.notna().sum(), s_raw.shift(k).notna().sum())),
        })
    return pd.DataFrame(rows)


def decay_view(w, top_n):
    """Average indexed response of audience leads around the top-N search WoW spikes."""
    s_diff = w["search"].diff()
    if s_diff.abs().sum() == 0 or w["search"].sum() == 0:
        return pd.DataFrame([{"note": "search series flat/zero - no spikes to index"}])
    spikes = s_diff.nlargest(top_n).index.tolist()
    frames = []
    idx = list(w.index)
    for t0 in spikes:
        i = idx.index(t0)
        rel = {}
        for off in range(-1, 4):
            j = i + off
            if 0 <= j < len(idx):
                rel[off] = (w["search"].iloc[j], w["audience"].iloc[j])
        if 0 not in rel or rel[0][1] == 0:
            continue
        base_s = rel[0][0] or np.nan
        base_a = rel[0][1] or np.nan
        row = {"spike_week": str(t0)}
        for off, (sv, av) in rel.items():
            row[f"search_idx_t{off:+d}"] = round(sv / base_s * 100, 1) if base_s else np.nan
            row[f"aud_idx_t{off:+d}"] = round(av / base_a * 100, 1) if base_a else np.nan
        frames.append(row)
    return pd.DataFrame(frames)


def daily_xcorr_table(pair, cutoff, start, max_lag_days):
    """Daily-grain first-diff cross-correlation (architect: a 3-10 day lag
    smears into k=0 at weekly grain; this resolves it)."""
    d = pair[(pair["date"] <= cutoff) & (pair["date"] >= pd.Timestamp(start))]
    w = (d.pivot_table(index="date", columns="bucket", values="leads",
                       aggfunc="sum").sort_index())
    for b in ("search", "audience"):
        if b not in w.columns:
            w[b] = 0.0
    w = w.reindex(pd.date_range(w.index.min(), w.index.max(), freq="D")).fillna(0.0)
    a_d, s_d = w["audience"].diff(), w["search"].diff()
    rows = [{"lag_days_k": k, "corr_first_diff": a_d.corr(s_d.shift(k))}
            for k in range(-7, max_lag_days + 1)]
    df = pd.DataFrame(rows)
    if df["corr_first_diff"].notna().any():
        best = df.loc[df["corr_first_diff"].idxmax()]
        lead = df[df["lag_days_k"] < 0]
        best_lead = (lead.loc[lead["corr_first_diff"].idxmax()]
                     if lead["corr_first_diff"].notna().any() else None)
        detail = f"daily first-diff leads, {start}..{cutoff.date()}"
        if best_lead is not None:
            detail += (f"; best audience-LEADING corr: k={int(best_lead['lag_days_k'])}d "
                       f"{best_lead['corr_first_diff']:.2f}")
        qa("A daily-lag peak",
           f"k={int(best['lag_days_k'])} days, corr={best['corr_first_diff']:.2f}", detail)
    return df


def persistence_view(w, top_n):
    """Architect's key episode pattern: after the top-N search WoW DROPS, does
    audience hold/rise while search stays down? Indexed to the PRIOR week
    (t-1 = 100)."""
    s_diff = w["search"].diff()
    if s_diff.abs().sum() == 0:
        return pd.DataFrame([{"note": "search series flat - no drops to index"}])
    drops = s_diff.nsmallest(top_n).index.tolist()
    idx = list(w.index)
    frames = []
    for t0 in drops:
        i = idx.index(t0)
        if i == 0:
            continue
        base_s, base_a = w["search"].iloc[i - 1], w["audience"].iloc[i - 1]
        if not base_s or not base_a:
            continue
        row = {"search_drop_week": str(t0),
               "search_wow_delta_leads": round(s_diff.loc[t0], 0)}
        for off in range(-1, 4):
            j = i + off
            if 0 <= j < len(idx):
                row[f"search_idx_t{off:+d}"] = round(w["search"].iloc[j] / base_s * 100, 1)
                row[f"aud_idx_t{off:+d}"] = round(w["audience"].iloc[j] / base_a * 100, 1)
        frames.append(row)
    return pd.DataFrame(frames)


def module_a(pair, conn, cutoff, recon):
    """Weekly + daily echo analysis on the unioned campaign pair
    (real search AND audience series, revenue-reconciled)."""
    w_full = weekly_leads(pair, cutoff, PARAMS["a_start_date"])
    w_cons = weekly_leads(pair, cutoff, PARAMS["consolidation_date"])

    corr = pd.concat([
        xcorr_table(w_cons, PARAMS["max_lag_weeks"],
                    f"consolidated (>= {PARAMS['consolidation_date']}) [HEADLINE]"),
        xcorr_table(w_full, PARAMS["max_lag_weeks"],
                    f"full history (>= {PARAMS['a_start_date']}) [robustness - "
                    f"structural break at consolidation]"),
    ], ignore_index=True)

    decay = decay_view(w_cons, PARAMS["spike_top_n"])
    persist = persistence_view(w_cons, PARAMS["spike_top_n"])
    daily = daily_xcorr_table(pair, cutoff, PARAMS["consolidation_date"],
                              PARAMS["max_lag_days"])
    weekly = w_full.reset_index().rename(columns={"week": "week_start"})
    weekly["structural_break_flag"] = np.where(
        pd.to_datetime(weekly["week_start"]) >= pd.Timestamp(PARAMS["consolidation_date"]),
        "consolidated", "pre-consolidation")
    return {"weekly": weekly, "corr": corr, "decay": decay, "persist": persist,
            "daily": daily, "recon": recon}


# ----------------------------------------------------------------------------
# Module B - conquest vs generic on audience
# ----------------------------------------------------------------------------

def module_a3(kw, cutoff):
    """Reallocation-vs-persistence test (strategy-thread question): during
    search-decline weeks, does hybrid audience show the budget-reallocation
    signature (impressions UP, CPC DOWN, CvR DOWN as the machinery buys deeper
    into colder inventory) or the intent-pool-persistence signature (all three
    steady)? Three correlations per tier, on first differences, plus a snapshot
    of each tier's biggest search-drop week vs its other-week averages."""
    d = kw[(kw["date"] >= pd.Timestamp(PARAMS["reconciled_start"]))
           & (kw["date"] <= cutoff)].copy()
    if d.empty or "impressions" not in d.columns:
        return None
    d["week"] = week_start(d["date"])
    spend_rank = (d.groupby("campaign_name")["spend"].sum()
                    .sort_values(ascending=False))
    tiers = [n for n in spend_rank.index
             if "tier" in str(n).lower() or classify_campaign_program(n)[0]]
    picks = (tiers or list(spend_rank.index))[:8]

    sig_rows, weekly_tables = [], {}

    def build(frame, label):
        w = (frame.groupby(["week", "bucket"])
                  .agg(impressions=("impressions", "sum"), clicks=("clicks", "sum"),
                       spend=("spend", "sum"), leads=("leads", "sum"),
                       revenue=("revenue", "sum")).reset_index())
        piv = w.pivot(index="week", columns="bucket",
                      values=["impressions", "clicks", "spend", "leads",
                              "revenue"]).fillna(0.0).sort_index()
        out = pd.DataFrame(index=piv.index)
        out["sea_leads"] = piv.get(("leads", "search"), 0.0)
        out["sea_rev"] = piv.get(("revenue", "search"), 0.0)
        for m in ("impressions", "clicks", "spend", "leads"):
            out[f"aud_{m[:5]}"] = piv.get((m, "audience"), 0.0)
        out["aud_cpc"] = np.where(out["aud_click"] > 0,
                                  out["aud_spend"] / out["aud_click"], np.nan)
        out["aud_cpm"] = np.where(out["aud_impre"] > 0,
                                  out["aud_spend"] / out["aud_impre"] * 1000, np.nan)
        out["aud_cvr_pct"] = np.where(out["aud_click"] > 0,
                                      out["aud_leads"] / out["aud_click"] * 100, np.nan)
        out["sea_leads_wow"] = out["sea_leads"].diff()
        ds = out["sea_leads"].diff()
        row = {"campaign": label,
               "corr_audImpr_vs_searchLeads":
                   out["aud_impre"].diff().corr(ds),
               "corr_audCPC_vs_searchLeads":
                   out["aud_cpc"].diff().corr(ds),
               "corr_audCvR_vs_searchLeads":
                   out["aud_cvr_pct"].diff().corr(ds),
               "n_weeks": int(len(out))}
        # biggest search-drop week vs other-week averages (index 100 = others)
        if ds.notna().any():
            t0 = ds.idxmin()
            others = out.drop(index=t0)
            for col, nm in (("aud_impre", "impr"), ("aud_cpc", "cpc"),
                            ("aud_cvr_pct", "cvr")):
                base = others[col].mean()
                row[f"dropwk_{nm}_idx"] = (round(out.loc[t0, col] / base * 100, 1)
                                           if base and not np.isnan(base) else np.nan)
            row["drop_week"] = str(t0)
        sig_rows.append(row)
        out["wk_label"] = [f"{x.month}/{x.day}" for x in out.index]
        return out.reset_index().rename(columns={"week": "week_start"})

    for pck in picks:
        weekly_tables[pck] = build(d[d["campaign_name"] == pck], str(pck)[:45])
    weekly_tables["ACCOUNT (all campaigns)"] = build(d, "ACCOUNT")

    sig = pd.DataFrame(sig_rows)
    acct = sig[sig["campaign"] == "ACCOUNT"]
    if len(acct):
        r = acct.iloc[0]
        realloc = ((r["corr_audImpr_vs_searchLeads"] or 0) < -0.2
                   and (r["corr_audCPC_vs_searchLeads"] or 0) > 0.2)
        qa("A3 reallocation-vs-persistence",
           "REALLOCATION signature" if realloc else "no clear reallocation signature",
           f"account corr vs search-leads change: aud impressions "
           f"{r['corr_audImpr_vs_searchLeads']:.2f} (negative = impr rise when "
           f"search falls), aud CPC {r['corr_audCPC_vs_searchLeads']:.2f} "
           f"(positive = CPC falls when search falls), aud CvR "
           f"{r['corr_audCvR_vs_searchLeads']:.2f} (positive = colder users)")
    return {"signature": sig, "weekly": weekly_tables}



def cutover_dates(conn):
    """Legacy hybrid cutover dates: PARAMS overrides (authoritative) + best-effort
    detection from campaign_change_events (status -> paused-like)."""
    out = {k: pd.Timestamp(v).date()
           for k, v in PARAMS["msan_cutover_overrides"].items()}
    cols = table_cols(conn, "campaign_change_events")
    if cols:
        dcol = next((c for c in ("change_date", "date", "event_date") if c in cols), None)
        item = next((c for c in ("item_changed", "item", "change_type") if c in cols), None)
        newv = next((c for c in ("new_value", "new", "value_new") if c in cols), None)
        camp_cols = table_cols(conn, "campaigns")
        if dcol and item and newv and "campaign_id" in cols and "campaign_name" in camp_cols:
            try:
                df = pd.read_sql_query(f"""
                    SELECT c.campaign_name, MIN(t.{dcol}) AS d
                    FROM campaign_change_events t
                    JOIN campaigns c ON c.campaign_id = t.campaign_id
                    WHERE LOWER(t.{item}) LIKE '%status%'
                      AND LOWER(t.{newv}) LIKE '%paus%'
                    GROUP BY 1
                """, conn)
                for _, r in df.iterrows():
                    key = str(r["campaign_name"])
                    if not any(k in key for k in out):
                        out[key] = pd.Timestamp(r["d"]).date()
            except Exception:
                pass
    return out


def module_a4(a3_res, cutovers=None):
    """Standing Level-1 alarm evaluation (the strategy thread's tripwire):
    per campaign-week, fire when search leads fall WoW AND audience impressions
    exceed impr_mult x trailing-4wk avg AND audience CvR sits below cvr_mult x
    trailing-4wk avg. Also flags Level-2 CvR drift (CvR down alongside search
    for 2 consecutive weeks). Regenerates every run = live telemetry."""
    if not a3_res:
        return None
    im, cm = PARAMS["a4_impr_mult"], PARAMS["a4_cvr_mult"]
    tw, mtw = PARAMS["a4_trailing_weeks"], PARAMS["a4_min_trailing_weeks"]
    ms = PARAMS["a4_min_aud_spend"]
    cutovers = cutovers or {}
    evals, fires, drifts = [], [], []
    for name, tbl in a3_res["weekly"].items():
        if "ACCOUNT" in name:
            continue
        is_newcid = classify_campaign_program(name)[0] is not None
        req_trail = PARAMS["a4_newcid_min_trailing"] if is_newcid else mtw
        cut = next((d for k, d in cutovers.items() if k in str(name)), None)
        t_ = tbl.reset_index(drop=True)
        cvr_down_streak = 0
        for i in range(1, len(t_)):
            row = t_.iloc[i]
            if cut and row["week_start"] >= cut:
                continue   # legacy hybrid past its cutover - stop evaluating
            if not row["aud_spend"] or row["aud_spend"] < ms:
                cvr_down_streak = 0
                continue
            trail = t_.iloc[max(0, i - tw):i]
            trail = trail[trail["aud_spend"] >= ms]
            if len(trail) < req_trail:
                continue
            impr_avg, cvr_avg = trail["aud_impre"].mean(), trail["aud_cvr_pct"].mean()
            impr_r = row["aud_impre"] / impr_avg if impr_avg else np.nan
            cvr_r = row["aud_cvr_pct"] / cvr_avg if cvr_avg else np.nan
            search_down = (t_.iloc[i]["sea_leads"] < t_.iloc[i - 1]["sea_leads"])
            fire = bool(search_down and impr_r > im and cvr_r < cm)
            evals.append({"campaign": str(name)[:45], "week": str(row["week_start"])[:10],
                          "search_down": search_down,
                          "impr_vs_trail": round(impr_r, 2),
                          "cvr_vs_trail": round(cvr_r, 2), "L1_FIRE": fire})
            if fire:
                fires.append(evals[-1])
            cvr_wow_down = (i > 0 and row["aud_cvr_pct"] < t_.iloc[i - 1]["aud_cvr_pct"])
            cvr_down_streak = cvr_down_streak + 1 if (cvr_wow_down and search_down) else 0
            if cvr_down_streak >= 2:
                drifts.append({"campaign": str(name)[:45],
                               "week": str(row["week_start"])[:10],
                               "note": "L2 WARNING - CvR down with search 2+ consecutive wks"})
    ev = pd.DataFrame(evals)
    summary = pd.DataFrame([{
        "cells_evaluated": len(ev),
        "L1_fires": int(ev["L1_FIRE"].sum()) if len(ev) else 0,
        "L2_warnings": len(drifts),
        "thresholds": f"impr>{im}x, cvr<{cm}x trailing-{tw}wk (min {mtw} trailing, "
                      f"aud spend >= ${ms}/wk)",
    }])
    qa("A4 Level-1 alarm evaluation",
       f"{summary['L1_fires'][0]} fire(s) across {len(ev)} cells",
       "; ".join(f"{f['campaign'][:30]} {f['week']}" for f in fires) or "none")
    return {"summary": summary, "evals": ev,
            "fires": pd.DataFrame(fires), "drifts": pd.DataFrame(drifts)}



# ----------------------------------------------------------------------------
# New-CID program classification (naming convention: MSAN_T{tier}_{PROGRAM})
# ----------------------------------------------------------------------------
PROGRAM_RE = re.compile(
    r"^MSAN[_\-\s]*T?(\d+|ALL)[_\-\s]+"
    r"(HARVEST|RMK|REMARKETING|INMKT|IN-?MARKET|PRED|PREDICTIVE|CONTENT)",
    re.IGNORECASE)
PROGRAM_CANON = {"REMARKETING": "RMK", "IN-MARKET": "INMKT", "INMARKET": "INMKT",
                 "PREDICTIVE": "PRED"}


def frozen_path(filename):
    d = PARAMS.get("frozen_dir") or os.path.dirname(PARAMS["output_path"]) or "."
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, filename)


def classify_campaign_program(name):
    m = PROGRAM_RE.match(str(name).strip())
    if not m:
        return None, None
    prog = m.group(2).upper().replace("IN MARKET", "INMKT")
    prog = PROGRAM_CANON.get(prog, prog)
    return prog, f"T{m.group(1).upper()}"


def campaign_tier_token(name):
    n = str(name).lower()
    for t in ("tier1", "tier2", "tier3", "tier4", "tier5"):
        if t in n.replace(" ", ""):
            return "T" + t[-1]
    _, tier = classify_campaign_program(name)
    return tier


def weekly_by_campaign(pc, cutoff):
    d = pc[(pc["date"] >= pd.Timestamp(PARAMS["reconciled_start"]))
           & (pc["date"] <= cutoff)].copy()
    d["week"] = week_start(d["date"])
    # complete weeks only, BOTH edges - a partial week must never enter a frozen
    # baseline or a trailing-3 read (trailing edge: cutoff; leading edge: window
    # start mid-week creates a stub week)
    rs = pd.Timestamp(PARAMS["reconciled_start"])
    d = d[[ (pd.Timestamp(w) + timedelta(days=6)) <= cutoff
            and pd.Timestamp(w) >= rs
            for w in d["week"] ]]
    w = (d.groupby(["campaign_name", "week", "bucket"])
           .agg(spend=("spend", "sum"), leads=("leads", "sum"),
                revenue=("revenue", "sum")).reset_index())
    piv = w.pivot_table(index=["campaign_name", "week"], columns="bucket",
                        values=["spend", "leads", "revenue"], fill_value=0.0)
    out = pd.DataFrame(index=piv.index)
    for m in ("spend", "leads", "revenue"):
        for b in ("search", "audience"):
            out[f"{b[:3]}_{m[:3]}"] = piv.get((m, b), 0.0)
    return out.reset_index()


def co_moving_mask(w, is_t2=False, hard_excl=()):
    """w: per-campaign weekly frame sorted by week with sea_rev, aud_spe.
    Returns (mask, reasons) per the R1 co-moving definition."""
    N = PARAMS["r1_launch_excl_weeks"]
    X = PARAMS["r1_contraction_pct"] / 100.0
    cons = pd.Timestamp(PARAMS["r1_consolidation_date"]).date()
    hard_excl = set(hard_excl) | (
        set(PARAMS["r1_hardcoded_excl_weeks"].get("T2", [])) if is_t2 else set())
    w = w.sort_values("week").reset_index(drop=True)
    reasons = [""] * len(w)
    mask = [True] * len(w)
    post = [i for i in range(len(w)) if w.loc[i, "week"] >= cons
            and (w.loc[i, "sea_rev"] > 0 or w.loc[i, "aud_spe"] > 0)]
    launch_ix = set(post[:N])
    prev = None
    for i in range(len(w)):
        r = w.loc[i]
        if i in launch_ix:
            mask[i], reasons[i] = False, "launch"
        elif prev and prev > 0 and (r["sea_rev"] - prev) / prev < -X:
            mask[i], reasons[i] = False, f"contraction>{PARAMS['r1_contraction_pct']:.0f}%"
        elif str(r["week"]) in hard_excl:
            mask[i], reasons[i] = False, "hardcoded exclusion (see PARAMS)"
        elif r["aud_spe"] < 50:
            mask[i], reasons[i] = False, "aud spend<$50"
        prev = r["sea_rev"]
    return mask, reasons


# ----------------------------------------------------------------------------
# Deliverable #1 - T2 holdout (time-sensitive)
# ----------------------------------------------------------------------------
def module_t2_holdout(pc, conn, cutoff):
    wk = weekly_by_campaign(pc, cutoff)
    t2n = [n for n in wk["campaign_name"].unique()
           if "tier2" in str(n).lower().replace(" ", "")]
    if not t2n:
        return None
    w = wk[wk["campaign_name"] == t2n[0]].sort_values("week").reset_index(drop=True)
    t2_ids = pc[pc["campaign_name"] == t2n[0]]["campaign_id"].unique().tolist()
    dark = w[w["aud_spe"] < 50]
    dark = dark[dark["week"] >= pd.Timestamp("2026-06-15").date()]
    first_dark = dark["week"].min() if len(dark) else None

    # tCPA raises from change events (T2, post-exclusion)
    raises = pd.DataFrame()
    all_events = pd.DataFrame()
    first_raise = None
    cols = table_cols(conn, "campaign_change_events")
    if cols:
        dcol = next((c for c in ("change_date", "date", "event_date", "changed_at")
                     if c in cols), None)
        item = next((c for c in ("item_changed", "item", "change_type", "field")
                     if c in cols), None)
        if dcol and item:
            try:
                join, where = scope_clause(conn, "campaign_change_events", cols)
            except RuntimeError:
                join, where = "", "1=1"
            keep = [c for c in cols if c != dcol][:6]
            id_and = (f" AND t.campaign_id IN ({','.join(str(int(i)) for i in t2_ids)})"
                      if "campaign_id" in cols and t2_ids else "")
            raises = pd.read_sql_query(f"""
                SELECT t.{dcol} AS change_date, {', '.join('t.'+c for c in keep)}
                FROM campaign_change_events t {join}
                WHERE {where} AND t.{dcol} >= '2026-06-25'{id_and}
                  AND (LOWER(t.{item}) LIKE '%cpa%' OR LOWER(t.{item}) LIKE '%target%'
                       OR LOWER(t.{item}) LIKE '%bid%' OR LOWER(t.{item}) LIKE '%strateg%')
                ORDER BY t.{dcol}
            """, conn)
            # diagnostic: ALL T2 change events since 6/25, any item - eyeballable
            all_events = pd.read_sql_query(f"""
                SELECT t.{dcol} AS change_date, {', '.join('t.'+c for c in keep)}
                FROM campaign_change_events t {join}
                WHERE {where} AND t.{dcol} >= '2026-06-25'{id_and}
                ORDER BY t.{dcol} LIMIT 60
            """, conn)
            if len(raises):
                first_raise = pd.Timestamp(raises["change_date"].min()).date()
    if PARAMS.get("t2_first_raise_override"):
        first_raise = pd.Timestamp(PARAMS["t2_first_raise_override"]).date()
        qa("T2 raise dating", "PARAMS override in effect", str(first_raise))
    elif first_raise is None:
        qa("T2 raise dating", "NOT FOUND in change events",
           "all dark weeks will be labeled clean - set t2_first_raise_override "
           "with the actual first-raise date, or verify the change-events "
           "pipeline captures Microsoft bid-strategy changes (architect item)")

    pre = w[w["week"] < (first_dark or cutoff.date())]
    mask, reasons = co_moving_mask(pre, is_t2=True)
    pre = pre.assign(comoving=mask, excl_reason=reasons)
    pre_cm = pre[pre["comoving"]]
    post = w[w["aud_spe"] < 50]
    post = post[post["week"] >= (first_dark or cutoff.date())].copy()
    post["raise_contaminated"] = [first_raise is not None and wkd >= first_raise
                                  for wkd in post["week"]]
    clean = post[~post["raise_contaminated"]]
    bases = []
    if len(clean):
        bases.append((clean, "clean (pre-raise) dark weeks"))
    if len(post) and (len(clean) != len(post)):
        bases.append((post, "ALL dark weeks (incl. raise-contaminated)"
                            + ("" if len(clean) else " - NO clean window")))
    if not bases:
        bases = [(post, "no post-exclusion dark weeks found")]
    rows = []
    for basis, basis_note in bases:
        rows.append({
            "t2_campaign": str(t2n[0])[:45],
            "first_dark_week": str(first_dark),
            "first_tcpa_raise": str(first_raise),
            "post_basis": basis_note,
            "pre_comoving_weeks": ", ".join(str(x) for x in pre_cm["week"]),
            "pre_search_rev_wk": pre_cm["sea_rev"].mean(),
            "pre_search_leads_wk": pre_cm["sea_lea"].mean(),
            "removed_aud_rev_wk": pre_cm["aud_rev"].mean(),
            "removed_aud_leads_wk": pre_cm["aud_lea"].mean(),
            "post_search_rev_wk": basis["sea_rev"].mean() if len(basis) else np.nan,
            "post_search_leads_wk": basis["sea_lea"].mean() if len(basis) else np.nan,
            "search_delta_wk": (basis["sea_rev"].mean() - pre_cm["sea_rev"].mean())
                               if len(basis) and len(pre_cm) else np.nan,
            "apparent_backfill_pct": ((basis["sea_rev"].mean() - pre_cm["sea_rev"].mean())
                                      / pre_cm["aud_rev"].mean() * 100)
                                     if len(basis) and len(pre_cm)
                                     and pre_cm["aud_rev"].mean() else np.nan,
        })
    summ = pd.DataFrame(rows)
    basis_note = rows[0]["post_basis"]
    qa("T2 holdout", basis_note,
       f"pre search ${summ['pre_search_rev_wk'][0]:,.0f}/wk vs post "
       f"${summ['post_search_rev_wk'][0]:,.0f}/wk; removed aud "
       f"${summ['removed_aud_rev_wk'][0]:,.0f}/wk; apparent backfill "
       f"{summ['apparent_backfill_pct'][0]:.0f}%")
    detail = w.copy()
    detail["phase"] = ["dark" if s < 50 and wd >= (first_dark or cutoff.date())
                       else "pre" for s, wd in zip(detail["aud_spe"], detail["week"])]
    return {"summary": summ, "pre_detail": pre, "post_detail": post,
            "detail": detail, "raises": raises, "all_events": all_events}


def write_t2_snapshot(t2):
    """Values-frozen sidecar - created once, never overwritten."""
    path = frozen_path(PARAMS["t2_snapshot_file"])
    if os.path.exists(path):
        qa("T2 snapshot", "exists - not overwritten", path)
        return
    with pd.ExcelWriter(path, engine="openpyxl") as wr:
        sw = SheetWriter(wr, "T2_Holdout_FROZEN",
                         f"T2 holdout snapshot - FROZEN {pd.Timestamp.now().date()}",
                         "Values-frozen copy; the live sheet regenerates, this file never")
        sw.block("Summary", t2["summary"].round(1))
        sw.block("Weekly detail", t2["detail"].round(1))
        sw.block("tCPA raise events", t2["raises"])
    qa("T2 snapshot", "WRITTEN (frozen)", path)


# ----------------------------------------------------------------------------
# Deliverable #2 - R1 recapture baseline (frozen sidecar)
# ----------------------------------------------------------------------------
def module_r1(pc, cutoff):
    wk = weekly_by_campaign(pc, cutoff)
    freeze_path = frozen_path(PARAMS["r1_freeze_file"])
    frozen = None
    if os.path.exists(freeze_path) and not PARAMS["r1_force_refreeze"]:
        with open(freeze_path) as f:
            frozen = json.load(f)
        qa("R1 baseline", "loaded FROZEN sidecar", freeze_path)

    def baseline_for(w, label, is_t2=False):
        mask, reasons = co_moving_mask(w, is_t2=is_t2)
        used = w[pd.Series(mask, index=w.index)]
        if len(used) < 3:
            qa("R1 baseline WARNING", f"{label}: only {len(used)} co-moving weeks",
               "membership too thin - review exclusion params")
        srev = used["sea_rev"].sum()
        return {"weeks": [str(x) for x in used["week"]],
                "n_weeks": len(used),
                "aud_leads_per_search_dollar": float(used["aud_lea"].sum() / srev)
                                               if srev else None,
                "aud_rpl": float(used["aud_rev"].sum() / used["aud_lea"].sum())
                           if used["aud_lea"].sum() else None,
                "aud_gp_wk": float((used["aud_rev"] - used["aud_spe"]).mean())
                             if len(used) else None}

    if frozen is None:
        tiers = {}
        for name in wk["campaign_name"].unique():
            tt = campaign_tier_token(name)
            if not tt or classify_campaign_program(name)[0]:
                continue   # tiers only; program campaigns are the numerator side
            w = wk[wk["campaign_name"] == name]
            tiers[tt] = baseline_for(w, tt, is_t2=(tt == "T2"))
            tiers[tt]["campaign"] = str(name)[:60]
        est = wk.groupby("week")[["sea_rev", "aud_spe", "aud_lea", "aud_rev"]].sum().reset_index()
        frozen = {"frozen_at": str(pd.Timestamp.now().date()),
                  "params": {k: PARAMS[k] for k in
                             ("r1_launch_excl_weeks", "r1_contraction_pct",
                              "reconciled_start", "consolidation_date")},
                  "tiers": tiers,
                  "estate": baseline_for(est, "ESTATE")}
        with open(freeze_path, "w") as f:
            json.dump(frozen, f, indent=1)
        qa("R1 baseline", "computed and FROZEN", freeze_path)

    base_rows = []
    for tt, b in frozen["tiers"].items():
        base_rows.append({"tier": tt, "campaign": b.get("campaign", ""),
                          "n_weeks": b["n_weeks"],
                          "aud_leads_per_$search": round(b["aud_leads_per_search_dollar"], 5)
                                                   if b["aud_leads_per_search_dollar"] else None,
                          "aud_rpl": round(b["aud_rpl"], 2) if b["aud_rpl"] else None,
                          "aud_gp_wk": round(b["aud_gp_wk"], 0) if b["aud_gp_wk"] is not None else None,
                          "weeks_used": "; ".join(b["weeks"])})
    e = frozen["estate"]
    base_rows.append({"tier": "ESTATE", "campaign": "(all)", "n_weeks": e["n_weeks"],
                      "aud_leads_per_$search": round(e["aud_leads_per_search_dollar"], 5)
                                               if e["aud_leads_per_search_dollar"] else None,
                      "aud_rpl": round(e["aud_rpl"], 2) if e["aud_rpl"] else None,
                      "aud_gp_wk": round(e["aud_gp_wk"], 0) if e["aud_gp_wk"] is not None else None,
                      "weeks_used": "; ".join(e["weeks"])})
    base_df = pd.DataFrame(base_rows)

    # post-split recapture (populates as MSAN program campaigns appear)
    rec_rows = []
    prog_names = [(n,) + classify_campaign_program(n)
                  for n in wk["campaign_name"].unique()
                  if classify_campaign_program(n)[0]]
    tier_search = {}   # tier -> weekly search rev of the tier's legacy/search campaign
    for name in wk["campaign_name"].unique():
        tt = campaign_tier_token(name)
        if tt and not classify_campaign_program(name)[0]:
            tier_search[tt] = wk[wk["campaign_name"] == name].set_index("week")["sea_rev"]
    for name, prog, tt in prog_names:
        if prog != "HARVEST" or tt not in frozen["tiers"]:
            continue
        b = frozen["tiers"][tt]
        w = wk[wk["campaign_name"] == name].sort_values("week")
        w = w[w["aud_spe"] >= 50]
        if len(w) < 3:
            rec_rows.append({"campaign": str(name)[:45], "tier": tt,
                             "status": f"{len(w)} stable wks - clock starts wk 3"})
            continue
        t3 = w.tail(3)
        srev = tier_search.get(tt, pd.Series(dtype=float)).reindex(t3["week"]).sum()
        lead_term = ((t3["aud_lea"].sum() / srev) / b["aud_leads_per_search_dollar"]
                     if srev and b["aud_leads_per_search_dollar"] else np.nan)
        rpl_term = ((t3["aud_rev"].sum() / t3["aud_lea"].sum()) / b["aud_rpl"]
                    if t3["aud_lea"].sum() and b["aud_rpl"] else np.nan)
        rec_rows.append({"campaign": str(name)[:45], "tier": tt,
                         "lead_term": round(lead_term, 3), "rpl_term": round(rpl_term, 3),
                         "gp_recapture": round(lead_term * rpl_term, 3)
                                         if not (np.isnan(lead_term) or np.isnan(rpl_term)) else np.nan,
                         "aud_gp_wk_now": round((t3["aud_rev"] - t3["aud_spe"]).mean(), 0),
                         "baseline_gp_wk": round(b["aud_gp_wk"], 0) if b["aud_gp_wk"] else None,
                         "status": "trailing-3wk read"})
    rec_df = pd.DataFrame(rec_rows) if rec_rows else pd.DataFrame(
        [{"note": "(no MSAN program campaigns in data yet - scaffold armed)"}])
    return {"baseline": base_df, "recapture": rec_df,
            "frozen_at": frozen["frozen_at"], "frozen": frozen}


# ----------------------------------------------------------------------------
# Deliverable #4 - R2 program P&L scaffold
# ----------------------------------------------------------------------------
def module_r2(pc, cutoff, r1):
    wk = weekly_by_campaign(pc, cutoff)
    wk["program"] = [classify_campaign_program(n)[0] for n in wk["campaign_name"]]
    prog = wk[wk["program"].notna()]
    est_gp_wk = r1["frozen"]["estate"]["aud_gp_wk"] if r1 else None
    head = pd.DataFrame([{
        "pre_split_estate_aud_gp_wk (frozen co-moving baseline)":
            round(est_gp_wk, 0) if est_gp_wk is not None else None,
        "note": "Prospecting/predictive GP counts HERE only - never in R1 recapture terms",
    }])
    if prog.empty:
        return {"head": head, "by_program": pd.DataFrame(
            [{"note": "(no MSAN program campaigns yet - scaffold armed)"}]),
            "weekly": pd.DataFrame()}
    byp = (prog.groupby("program")
               .agg(aud_spend=("aud_spe", "sum"), aud_leads=("aud_lea", "sum"),
                    aud_rev=("aud_rev", "sum")).reset_index())
    byp["aud_gp"] = byp["aud_rev"] - byp["aud_spend"]
    wkly = (prog.groupby("week")[["aud_spe", "aud_rev"]].sum().reset_index())
    wkly["estate_program_gp"] = wkly["aud_rev"] - wkly["aud_spe"]
    return {"head": head, "by_program": byp.round(0), "weekly": wkly.round(0)}


def module_b(kw, cutoff):
    d = kw[kw["date"] <= cutoff]
    scope = (f"{d['date'].min().date()}..{d['date'].max().date()} - table spans the "
             f"2025 structure too; pre-consolidation completeness for retired state "
             f"campaigns unverified")
    order = {"conquest": 0, "generic": 1, "affinity": 2, "pool": 3, "other": 4}

    def cut(frame):
        a = (frame.groupby(["ad_group_type", "bucket"])
                  [["clicks", "spend", "leads", "revenue"]].sum().reset_index())
        a = add_ratios(a)
        return a.sort_values(["ad_group_type", "bucket"],
                             key=lambda s: s.map(order) if s.name == "ad_group_type" else s)

    agg = cut(d)
    d_recent = d[d["date"] >= pd.Timestamp(PARAMS["reconciled_start"])]
    agg_recent = cut(d_recent)
    scope_recent = (f"{PARAMS['reconciled_start']}..{d['date'].max().date()} "
                    f"(revenue-certified window)")

    # full classification roster: every ad group -> its assigned type
    roster = (d.groupby(["ad_group_type", "ad_group_name"])
                [["spend", "leads"]].sum().reset_index())
    order2 = {"conquest": 0, "generic": 1, "affinity": 2, "pool": 3, "other": 4}
    roster = roster.sort_values(
        ["ad_group_type", "spend"], ascending=[True, False],
        key=lambda s: s.map(order2) if s.name == "ad_group_type" else s)

    # classification coverage
    cov = (d.groupby("ad_group_type")["spend"].sum().reset_index())
    cov["spend_share_pct"] = cov["spend"] / cov["spend"].sum() * 100
    other_names = (d[d["ad_group_type"] == "other"]
                   .groupby("ad_group_name")["spend"].sum()
                   .sort_values(ascending=False).head(15).reset_index())
    qa("B classification coverage",
       f"other = {cov.loc[cov['ad_group_type']=='other','spend_share_pct'].sum():.1f}% of spend",
       "top unclassified ad groups on the B sheet")
    return {"agg": agg, "agg_recent": agg_recent, "coverage": cov,
            "other_names": other_names, "roster": roster, "scope": scope,
            "scope_recent": scope_recent}


# ----------------------------------------------------------------------------
# Module C - RPL parity by state x network
# ----------------------------------------------------------------------------

CARRIER_RE = re.compile(r"non-?generic[-_ ]+(.+)$", re.IGNORECASE)


def extract_carrier(ad_group_name):
    m = CARRIER_RE.search(str(ad_group_name))
    return m.group(1).strip() if m else str(ad_group_name)


def module_b2(kw, cutoff):
    """Conquest carrier break-out (Gilbert's masking challenge): does the
    GP-positive conquest-audience aggregate hide carrier-level drains? Carrier
    x network side-by-side, sorted worst audience GP first, plus a carrier x
    campaign audience cut to localize any drains."""
    d = kw[(kw["date"] >= pd.Timestamp(PARAMS["reconciled_start"]))
           & (kw["date"] <= cutoff)
           & (kw["ad_group_type"] == "conquest")].copy()
    if d.empty:
        return None
    d["carrier"] = d["ad_group_name"].map(extract_carrier)
    scope = (f"{PARAMS['reconciled_start']}..{d['date'].max().date()} "
             f"(revenue-certified window)")

    agg = (d.groupby(["carrier", "bucket"])
             [["clicks", "spend", "leads", "revenue"]].sum().reset_index())
    agg = add_ratios(agg)
    keep = ["clicks", "spend", "leads", "revenue", "cvr_pct", "cpl", "rpl",
            "gp", "gp_per_lead", "roas"]
    wide = agg.pivot(index="carrier", columns="bucket", values=keep)
    wide.columns = [f"{'aud' if b == 'audience' else 'sea'}_{m}"
                    for m, b in wide.columns]
    a_cols = [c for c in ("aud_spend", "aud_leads", "aud_cvr_pct", "aud_cpl",
                          "aud_rpl", "aud_gp", "aud_gp_per_lead", "aud_roas")
              if c in wide.columns]
    s_cols = [c for c in ("sea_spend", "sea_leads", "sea_cvr_pct", "sea_rpl",
                          "sea_gp", "sea_roas") if c in wide.columns]
    wide = wide[a_cols + s_cols].sort_values(
        "aud_gp" if "aud_gp" in wide.columns else a_cols[0]).reset_index()

    if "aud_gp" in wide.columns:
        neg = wide[wide["aud_gp"] < 0]
        pos = wide[wide["aud_gp"] >= 0]
        qa("B2 conquest-carrier masking check",
           f"{len(neg)} of {len(wide)} carriers GP-NEGATIVE on audience",
           f"drains sum ${neg['aud_gp'].sum():,.0f} vs winners "
           f"+${pos['aud_gp'].sum():,.0f} -> net "
           f"${wide['aud_gp'].sum():,.0f}; drain spend "
           f"${neg['aud_spend'].sum():,.0f}")

    by_camp = (d[d["bucket"] == "audience"]
               .groupby(["carrier", "campaign_name"])
               [["clicks", "spend", "leads", "revenue"]].sum().reset_index())
    by_camp = add_ratios(by_camp).sort_values("gp").reset_index(drop=True)
    return {"by_carrier": wide, "by_campaign": by_camp, "scope": scope}


def module_c(conn, cutoff, kw):
    d, gate, meta = pull_geo_state(conn, cutoff, kw)
    qa("C geo gate", "ok" if d is not None else "BLOCKED", gate)
    if d is None or d.empty:
        return None
    scope = f"{d['min_date'].min()}..{d['max_date'].max()}"
    agg = d.groupby(["geo_state", "bucket"])[["leads", "revenue", "spend"]].sum().reset_index()
    piv = agg.pivot(index="geo_state", columns="bucket",
                    values=["leads", "revenue"]).fillna(0.0)
    out = pd.DataFrame(index=piv.index)
    for b in ("search", "audience"):
        lv = piv.get(("leads", b), pd.Series(0.0, index=piv.index))
        rv = piv.get(("revenue", b), pd.Series(0.0, index=piv.index))
        out[f"{b}_leads"] = lv
        out[f"{b}_rpl"] = np.where(lv > 0, rv / lv, np.nan)
    out["rpl_delta_pct"] = np.where(
        out["search_rpl"] > 0,
        (out["audience_rpl"] - out["search_rpl"]) / out["search_rpl"] * 100, np.nan)
    minl = PARAMS["c_min_leads_per_cell"]
    out["meets_min_leads"] = (out["search_leads"] >= minl) & (out["audience_leads"] >= minl)
    band = PARAMS["c_parity_band_pct"]
    qualified = out[out["meets_min_leads"]]
    within = (qualified["rpl_delta_pct"].abs() <= band).sum()
    summary = pd.DataFrame([{
        "states_total": len(out),
        "states_meeting_min_leads": len(qualified),
        f"states_within_±{band:.0f}%_of_search_rpl": int(within),
        "share_within_band_pct": round(within / len(qualified) * 100, 1) if len(qualified) else np.nan,
        "min_leads_per_cell": minl,
        "verdict": ("tier model transfers" if len(qualified)
                    and within / len(qualified) >= 0.6
                    else "audience may need its own tier cut (see §7 quarter-end re-run)"),
    }])
    out = out.sort_values("audience_leads", ascending=False).reset_index()
    return {"by_state": out, "summary": summary, "scope": scope,
            "conv_def": meta["conv_def"], "xcheck": meta["xcheck"]}


# ----------------------------------------------------------------------------
# Module D - GP-decline decomposition
# ----------------------------------------------------------------------------
def module_d(pair, kw, cutoff, conn, kw_dollars_ok):
    d = pair[pair["date"] <= cutoff].copy()
    d["week"] = week_start(d["date"])
    wk = (d.groupby(["week", "bucket"])
            .agg(spend=("spend", "sum"), revenue=("revenue", "sum"))
            .reset_index())
    wk["gp"] = wk["revenue"] - wk["spend"]
    piv = wk.pivot(index="week", columns="bucket", values="gp").fillna(0.0).sort_index()
    for b in ("search", "audience"):
        if b not in piv.columns:
            piv[b] = 0.0
    piv["total_gp"] = piv["search"] + piv["audience"]
    # complete weeks only within lookback
    if len(piv) and (cutoff.date() - piv.index[-1]).days < 6:
        piv = piv.iloc[:-1]
    piv = piv.tail(PARAMS["d_lookback_weeks"])
    piv["total_wow_pct"] = piv["total_gp"].pct_change() * 100

    declines = piv[piv["total_wow_pct"] < 0]
    if declines.empty:
        qa("D decline window", "none found",
           f"no WoW total-GP decline in last {PARAMS['d_lookback_weeks']} complete weeks")
        decomp = pd.DataFrame([{"note": "No WoW GP decline in lookback window"}])
        conc = pd.DataFrame()
        window_txt = "n/a"
    else:
        wk_t = declines["total_wow_pct"].idxmin()
        i = list(piv.index).index(wk_t)
        wk_p = piv.index[i - 1]
        d_total = piv.loc[wk_t, "total_gp"] - piv.loc[wk_p, "total_gp"]
        d_aud = piv.loc[wk_t, "audience"] - piv.loc[wk_p, "audience"]
        d_sea = piv.loc[wk_t, "search"] - piv.loc[wk_p, "search"]
        decomp = pd.DataFrame([{
            "prior_week": str(wk_p), "decline_week": str(wk_t),
            "total_gp_prior": piv.loc[wk_p, "total_gp"],
            "total_gp_decline_week": piv.loc[wk_t, "total_gp"],
            "wow_decline_pct": piv.loc[wk_t, "total_wow_pct"],
            "delta_gp_total": d_total,
            "delta_gp_audience": d_aud,
            "delta_gp_search": d_sea,
            "audience_share_of_decline_pct":
                round(d_aud / d_total * 100, 1) if d_total else np.nan,
        }])
        window_txt = f"{wk_p} -> {wk_t}"
        qa("D decline window", window_txt,
           f"total GP {piv.loc[wk_t,'total_wow_pct']:.1f}% WoW; audience share of "
           f"decline = {decomp['audience_share_of_decline_pct'][0]}%")

        # conquest concentration inside the decline window (keyword grain)
        k = kw[kw["date"] <= cutoff].copy()
        k["week"] = week_start(k["date"])
        kk = k[k["week"].isin([wk_p, wk_t])]
        conc = (kk.groupby(["week", "ad_group_type", "bucket"])
                  [["spend", "revenue", "leads"]].sum().reset_index())
        conc["gp"] = conc["revenue"] - conc["spend"]
        pv = conc.pivot_table(index=["ad_group_type", "bucket"], columns="week",
                              values=["gp", "leads"], fill_value=0.0)
        pv.columns = [f"{m}_{c}" for m, c in pv.columns]
        gp_cols = sorted(c for c in pv.columns if c.startswith("gp_"))
        ld_cols = sorted(c for c in pv.columns if c.startswith("leads_"))
        if len(gp_cols) == 2:
            pv["delta_gp"] = pv[gp_cols[1]] - pv[gp_cols[0]]
        if len(ld_cols) == 2:
            pv["delta_leads"] = pv[ld_cols[1]] - pv[ld_cols[0]]
        conc = pv.reset_index().sort_values(
            "delta_gp" if "delta_gp" in pv else pv.columns[0])
        if not kw_dollars_ok:
            conc["caveat"] = ("kw $-figures not account-complete (see Params_QA) "
                              "- weight delta_leads over delta_gp")
        events = pull_change_events(conn, wk_p, wk_t)

    weekly_out = piv.reset_index().rename(columns={"week": "week_start"})
    if declines.empty:
        events = pd.DataFrame()
    return {"weekly_gp": weekly_out, "decomp": decomp, "concentration": conc,
            "window": window_txt, "events": events}


# ----------------------------------------------------------------------------
# Module E - tCPA blind spot (RPL dispersion on audience)
# ----------------------------------------------------------------------------
def module_e(b_agg):
    aud = b_agg[b_agg["bucket"] == "audience"].copy()
    tot = aud[["clicks", "spend", "leads", "revenue"]].sum()
    avg_rpl = tot["revenue"] / tot["leads"] if tot["leads"] else np.nan
    aud["rpl_vs_audience_avg_pct"] = np.where(
        avg_rpl and avg_rpl > 0, (aud["rpl"] - avg_rpl) / avg_rpl * 100, np.nan)
    aud = aud[["ad_group_type", "leads", "spend", "revenue", "cpl", "rpl",
               "rpl_vs_audience_avg_pct", "gp_per_lead"]]
    totrow = pd.DataFrame([{
        "ad_group_type": "AUDIENCE TOTAL", "leads": tot["leads"], "spend": tot["spend"],
        "revenue": tot["revenue"],
        "cpl": tot["spend"] / tot["leads"] if tot["leads"] else np.nan,
        "rpl": avg_rpl, "rpl_vs_audience_avg_pct": 0.0,
        "gp_per_lead": (tot["revenue"] - tot["spend"]) / tot["leads"]
                       if tot["leads"] else np.nan,
    }])
    disp = pd.concat([aud, totrow], ignore_index=True)

    gap = pd.DataFrame()
    cq = aud[aud["ad_group_type"] == "conquest"]
    gn = aud[aud["ad_group_type"] == "generic"]
    if len(cq) and len(gn):
        gap = pd.DataFrame([{
            "conquest_rpl": cq["rpl"].iloc[0], "generic_rpl": gn["rpl"].iloc[0],
            "rpl_gap_$": gn["rpl"].iloc[0] - cq["rpl"].iloc[0],
            "conquest_gp_per_lead": cq["gp_per_lead"].iloc[0],
            "generic_gp_per_lead": gn["gp_per_lead"].iloc[0],
            "gp_per_lead_gap_$": gn["gp_per_lead"].iloc[0] - cq["gp_per_lead"].iloc[0],
            "note": "At a uniform tCPA, bidding pays the same per lead across both "
                    "populations - this gap is what lead-count optimization cannot see.",
        }])
    return {"dispersion": disp, "gap": gap}


# ----------------------------------------------------------------------------
# Excel output
# ----------------------------------------------------------------------------
class SheetWriter:
    def __init__(self, writer, sheet, title, scope_note):
        self.writer, self.sheet, self.row = writer, sheet, 0
        pd.DataFrame([[title]]).to_excel(writer, sheet_name=sheet, startrow=0,
                                         header=False, index=False)
        pd.DataFrame([[scope_note]]).to_excel(writer, sheet_name=sheet, startrow=1,
                                              header=False, index=False)
        self.row = 3

    def block(self, label, df, index=False):
        pd.DataFrame([[label]]).to_excel(self.writer, sheet_name=self.sheet,
                                         startrow=self.row, header=False, index=False)
        self.row += 1
        if df is None or len(df) == 0:
            pd.DataFrame([["(no rows)"]]).to_excel(
                self.writer, sheet_name=self.sheet, startrow=self.row,
                header=False, index=False)
            self.row += 2
            return
        df.to_excel(self.writer, sheet_name=self.sheet, startrow=self.row, index=index)
        self.row += len(df) + (2 if not index else 3)
        self.row += 1



def add_a2_charts(writer, a2_res):
    """Combo chart per campaign block (per Gilbert's example): green columns =
    audience GP, solid blue line = search revenue, dashed orange line = audience
    revenue, single shared $ axis, legend right, abbreviated m/d dates."""
    from openpyxl.chart import BarChart, LineChart, Reference
    from openpyxl.chart.axis import ChartLines
    from openpyxl.chart.series import SeriesLabel
    from openpyxl.drawing.line import LineProperties
    ws = writer.sheets.get("A2_Tier_Breakout")
    if ws is None or not a2_res:
        return
    labels = {ws.cell(row=r, column=1).value: r for r in range(1, ws.max_row + 1)}
    for name, tbl in a2_res["weekly"].items():
        lbl = f"Weekly: {name}"
        if lbl not in labels or len(tbl) < 2:
            continue
        hdr = labels[lbl] + 1
        n = len(tbl)
        cols = list(tbl.columns)
        c_gp = cols.index("aud_gp") + 1
        c_srev = cols.index("sea_rev") + 1
        c_arev = cols.index("aud_rev") + 1
        c_lbl = cols.index("wk_label") + 1
        lo = float(min(0.0, tbl["aud_gp"].min()) * 1.1)
        hi = float(max(tbl["sea_rev"].max(), tbl["aud_rev"].max(),
                       tbl["aud_gp"].max()) * 1.1)
        lo, hi = 500 * np.floor(lo / 500), 500 * np.ceil(hi / 500)
        cats = Reference(ws, min_col=c_lbl, min_row=hdr + 1, max_row=hdr + n)

        bar = BarChart()
        bar.type = "col"
        bar.add_data(Reference(ws, min_col=c_gp, min_row=hdr, max_row=hdr + n),
                     titles_from_data=True)
        bar.set_categories(cats)
        bar.gapWidth = 300
        bar.y_axis.scaling.min, bar.y_axis.scaling.max = lo, hi
        bar.y_axis.title = "$"
        bar.y_axis.delete = False
        bar.y_axis.tickLblPos = "nextTo"
        bar.y_axis.number_format = '$#,##0,"K"'
        bar.y_axis.majorGridlines = ChartLines()
        bar.x_axis.delete = False
        bar.x_axis.tickLblPos = "low"
        bar.title = f"{str(name)[:48]} - search vs audience weekly revenue"
        bar.legend.position = "r"
        bar.legend.overlay = False
        bar.height, bar.width = 9.5, 26
        try:
            bar.series[0].tx = SeriesLabel(v="Audience GP")
            bar.series[0].graphicalProperties.solidFill = "97C459"
        except Exception:
            pass

        line = LineChart()
        line.add_data(Reference(ws, min_col=c_srev, max_col=c_arev,
                                min_row=hdr, max_row=hdr + n), titles_from_data=True)
        line.set_categories(cats)
        line.y_axis.axId = 200
        line.y_axis.delete = True
        line.y_axis.scaling.min, line.y_axis.scaling.max = lo, hi
        try:
            s_search, s_aud = line.series[0], line.series[1]
            s_search.tx = SeriesLabel(v="Search Rev")
            s_aud.tx = SeriesLabel(v="Audience Rev")
            s_search.graphicalProperties.line = LineProperties(
                solidFill="2A78D6", w=25000)
            s_aud.graphicalProperties.line = LineProperties(
                solidFill="EB6834", w=25000, prstDash="dash")
            s_search.smooth = s_aud.smooth = False
        except Exception:
            pass
        bar += line
        ws.add_chart(bar, f"M{hdr}")


def add_charts(writer, a_res):
    from openpyxl.chart import BarChart, LineChart, Reference
    ws = writer.sheets.get("A_Echo_Lag")
    if ws is None:
        return
    # locate the corr table + weekly table by scanning col A labels
    labels = {ws.cell(row=r, column=1).value: r for r in range(1, ws.max_row + 1)}
    corr_lbl = next((v for v in labels if v and str(v).startswith("Cross-correlation")), None)
    wk_lbl = next((v for v in labels if v and str(v).startswith("Weekly leads")), None)
    try:
        if corr_lbl:
            hdr = labels[corr_lbl] + 1
            n = len(a_res["corr"])
            ch = BarChart(); ch.title = "Audience[t] vs Search[t-k] correlation (first-diff)"
            ch.y_axis.title = "corr"; ch.x_axis.title = "lag k (weeks)"
            data = Reference(ws, min_col=4, min_row=hdr, max_row=hdr + n)  # corr_first_diff
            cats = Reference(ws, min_col=2, min_row=hdr + 1, max_row=hdr + n)
            ch.add_data(data, titles_from_data=True); ch.set_categories(cats)
            ws.add_chart(ch, "J4")
        if wk_lbl:
            hdr = labels[wk_lbl] + 1
            n = len(a_res["weekly"])
            ch = LineChart(); ch.title = "Weekly leads: search vs audience"
            data = Reference(ws, min_col=2, max_col=3, min_row=hdr, max_row=hdr + n)
            cats = Reference(ws, min_col=1, min_row=hdr + 1, max_row=hdr + n)
            ch.add_data(data, titles_from_data=True); ch.set_categories(cats)
            ch.width, ch.height = 24, 10
            ws.add_chart(ch, "J22")
    except Exception as e:  # charts are nice-to-have; never fail the run for them
        qa("Charts", "skipped", repr(e))


def main():
    print(f"Opening (read-only): {PARAMS['db_path']}")
    conn = sqlite3.connect(f"file:{PARAMS['db_path']}?mode=ro", uri=True)
    results = {}
    pair = kw = None
    try:
        # Primary sources: keyword network table (ad-group grain) + the unioned
        # campaign pair (account/network grain, per db architect)
        kw = pull_keyword_network(conn)
        pair = pull_campaign_pair(conn)
        max_date = pair["date"].max()
        cutoff = max_date - timedelta(days=PARAMS["revenue_lag_days"])
        qa("Revenue-lag cutoff", str(cutoff.date()),
           f"max data date {max_date.date()} minus {PARAMS['revenue_lag_days']}d "
           f"(applied to ALL modules)")

        recon, kw_dollars_ok = revenue_reconciliation(conn, pair, kw, cutoff)

        # ---- Module A ----
        try:
            results["A"] = module_a(pair, conn, cutoff, recon)
        except Exception:
            qa("Module A", "FAILED", traceback.format_exc(limit=3))

        # ---- Module A2 (tier break-out + economics-decay test) ----
        pc_all = None
        try:
            pc_all = pull_campaign_pair_by_campaign(conn)
            results["A2"] = module_a2(pc_all, cutoff)
        except Exception:
            qa("Module A2", "FAILED", traceback.format_exc(limit=3))

        # ---- Deliverable #1: T2 holdout (time-sensitive) ----
        if pc_all is not None:
            try:
                results["T2H"] = module_t2_holdout(pc_all, conn, cutoff)
                if results["T2H"]:
                    write_t2_snapshot(results["T2H"])
            except Exception:
                qa("T2 holdout", "FAILED", traceback.format_exc(limit=3))

        # ---- Deliverables #2/#4: R1 baseline + R2 scaffold ----
        if pc_all is not None:
            try:
                results["R1"] = module_r1(pc_all, cutoff)
                results["R2"] = module_r2(pc_all, cutoff, results.get("R1"))
            except Exception:
                qa("R1/R2", "FAILED", traceback.format_exc(limit=3))

        # ---- Module A3 (reallocation-vs-persistence test) ----
        try:
            results["A3"] = module_a3(kw, cutoff)
        except Exception:
            qa("Module A3", "FAILED", traceback.format_exc(limit=3))

        # ---- Module A4 (Level-1 alarm evaluation) ----
        try:
            results["A4"] = module_a4(results.get("A3"), cutover_dates(conn))
        except Exception:
            qa("Module A4", "FAILED", traceback.format_exc(limit=3))

        # ---- Module B ----
        if kw is not None:
            try:
                results["B"] = module_b(kw, cutoff)
            except Exception:
                qa("Module B", "FAILED", traceback.format_exc(limit=3))

        # ---- Module B2 (conquest carrier break-out) ----
        if "B" in results:
            try:
                results["B2"] = module_b2(kw, cutoff)
            except Exception:
                qa("Module B2", "FAILED", traceback.format_exc(limit=3))

        # ---- Module C ----
        if kw is not None:
            try:
                results["C"] = module_c(conn, cutoff, kw)
            except Exception:
                qa("Module C", "FAILED", traceback.format_exc(limit=3))

        # ---- Module D ----
        if pair is not None and kw is not None:
            try:
                results["D"] = module_d(pair, kw, cutoff, conn, kw_dollars_ok)
            except Exception:
                qa("Module D", "FAILED", traceback.format_exc(limit=3))

        # ---- Module E ----
        if "B" in results:
            try:
                results["E"] = module_e(results["B"]["agg_recent"])
            except Exception:
                qa("Module E", "FAILED", traceback.format_exc(limit=3))

        # ---- Spend QA assertion (manual reconcile figure) ----
        if pair is not None:
            s28 = pair[(pair["date"] > cutoff - timedelta(days=PARAMS["spend_qa_days"]))
                       & (pair["date"] <= cutoff)]["spend"].sum()
            qa("Spend QA (manual reconcile)",
               f"${s28:,.0f} trailing {PARAMS['spend_qa_days']}d to {cutoff.date()}",
               "compare vs tracker/campaign workbook; a ~2x figure flags a "
               "device='ALL' miss")
    finally:
        conn.close()

    # ------------------------------------------------------------------ write
    out = PARAMS["output_path"]
    out_dir = os.path.dirname(out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        if "A" in results:
            a = results["A"]
            sw = SheetWriter(writer, "A_Echo_Lag",
                             "Module A - Search->Audience lead echo (cross-correlation + decay)",
                             "Grain: account-total weekly + daily | Source: UNIONED campaign pair "
                             "(campaign_performance_daily = search-only + "
                             "campaign_performance_network_daily = audience; revenue-reconciled "
                             "to geo within $1/month) | Leads=conversions(=primary), "
                             "Revenue=conversion_value "
                             f"| Structural break at {PARAMS['consolidation_date']}")
            sw.block("Weekly leads by network bucket (search vs audience)", a["weekly"])
            sw.block("Cross-correlation: audience[t] vs search[t-k] - negative k = "
                     "audience LEADS search, positive k = audience lags "
                     "(LEAD WITH first-diff - raw levels overstate)", a["corr"])
            sw.block(f"Daily-grain cross-correlation, lags -7..{PARAMS['max_lag_days']} days "
                     "(negative = audience leads; resolves sub-week structure weekly "
                     "grain smears into k=0)", a["daily"])
            sw.block(f"Persistence after top {PARAMS['spike_top_n']} search WoW DROPS "
                     "(prior week = 100; the architect's late-June episode pattern)",
                     a["persist"])
            sw.block(f"Decay view - top {PARAMS['spike_top_n']} search WoW spikes, "
                     "indexed to spike week = 100", a["decay"])
            sw.block("Cross-grain revenue reconciliation (pair vs geo vs keyword table, "
                     "last 3 full months)", a["recon"].round(2))
        if "A2" in results:
            a2 = results["A2"]
            sw = SheetWriter(writer, "A2_Tier_Breakout",
                             "Module A2 - Tier break-out: search vs audience weekly + "
                             "audience ROAS trajectory",
                             f"Grain: campaign weekly | Source: unioned pair at campaign grain "
                             f"| Window: {PARAMS['reconciled_start']}+ (revenue-certified) "
                             "| Thesis test: does audience ROAS fall AFTER search revenue "
                             "falls (k=1,2)?")
            sw.block("Thesis test - corr(audience ROAS[t], search rev WoW[t-k]) per "
                     "campaign (positive at k=1/2 supports economics-decay-after-"
                     "search-decline; aud spend <$50/wk excluded)", a2["lag"].round(2))
            for name, tbl in a2["weekly"].items():
                sw.block(f"Weekly: {name}", tbl.round(1))
        if results.get("A3"):
            a3r = results["A3"]
            sw = SheetWriter(writer, "A3_Reallocation_Test",
                             "Module A3 - Budget-reallocation vs intent-persistence "
                             "signatures during search declines",
                             f"Grain: campaign weekly | Source: keyword network table | "
                             f"Window: {PARAMS['reconciled_start']}+ | READ: reallocation = "
                             "aud impressions RISE (corr<0), CPC FALLS (corr>0), CvR FALLS "
                             "(corr>0) when search leads fall; persistence = all ~0. "
                             "dropwk_*_idx: biggest search-drop week vs other-week avg=100")
            sw.block("Signature summary - three correlations + drop-week snapshot "
                     "per campaign (first differences)", a3r["signature"].round(2))
            for name, tbl in a3r["weekly"].items():
                sw.block(f"Weekly: {name}", tbl.round(2))
        if results.get("A4"):
            a4r = results["A4"]
            sw = SheetWriter(writer, "A4_Alarm_Backtest",
                             "Module A4 - Level-1 tripwire evaluation (T2-signature alarm)",
                             "Fire = search leads down WoW + aud impressions above mult x "
                             "trailing avg + aud CvR below mult x trailing avg | Baselines: "
                             "TRAILING window (see thresholds) | Regenerates each run = "
                             "standing telemetry until ops tooling exists")
            sw.block("Summary", a4r["summary"])
            sw.block("Level-1 FIRES", a4r["fires"])
            sw.block("Level-2 warnings (CvR drift with search, 2+ consecutive weeks)",
                     a4r["drifts"])
            sw.block("All evaluated cells", a4r["evals"])
        if results.get("T2H"):
            t2 = results["T2H"]
            sw = SheetWriter(writer, "T2_Holdout",
                             "T2 natural experiment - pre/post audience-exclusion read "
                             "(§2 incrementality citation; before/after template)",
                             "Pre = co-moving weeks per R1 definition | Post = audience-dark "
                             "weeks, raise-contaminated weeks flagged | FROZEN copy: sidecar "
                             f"{PARAMS['t2_snapshot_file']} (created once)")
            sw.block("Summary + backfill verdict", t2["summary"].round(1))
            sw.block("Pre-exclusion weeks (co-moving flag + exclusion reasons)",
                     t2["pre_detail"].round(1))
            sw.block("Post-exclusion dark weeks (raise contamination flagged)",
                     t2["post_detail"].round(1))
            sw.block("tCPA raise events (campaign_change_events, pattern-matched)",
                     t2["raises"])
            sw.block("DIAGNOSTIC - ALL T2 change events since 6/25 (any item; if "
                     "your raises aren't here, the pipeline doesn't capture them "
                     "-> use t2_first_raise_override)", t2["all_events"])
        if results.get("R1"):
            r1 = results["R1"]
            sw = SheetWriter(writer, "R1_Recapture",
                             f"R1 - Co-moving recapture baseline (FROZEN {r1['frozen_at']}) "
                             "+ post-split recapture reads",
                             "Baseline membership locked in sidecar "
                             f"{PARAMS['r1_freeze_file']}; excludes launch wks, "
                             f">{PARAMS['r1_contraction_pct']:.0f}% search contractions, "
                             "T2 6/15, aud<$50 | Post-split reads: trailing-3wk, clock "
                             "starts wk 3 per campaign")
            sw.block("Frozen baseline per tier + estate (with explicit week membership)",
                     r1["baseline"])
            sw.block("Post-split recapture (lead term x RPL term = GP recapture)",
                     r1["recapture"])
        if results.get("R2"):
            r2 = results["R2"]
            sw = SheetWriter(writer, "R2_Program_PL",
                             "R2 - Program P&L scaffold (estate audience GP, all new-CID "
                             "programs vs pre-split co-moving baseline)",
                             "Populates as MSAN_<TIER>_<PROGRAM> campaigns appear")
            sw.block("Baseline", r2["head"])
            sw.block("GP by program", r2["by_program"])
            sw.block("Estate program GP by week", r2["weekly"])
        if "B" in results:
            b = results["B"]
            sw = SheetWriter(writer, "B_Conquest_vs_Generic",
                             "Module B - Ad-group type x network (CvR / RPL / GP / spend)",
                             f"Grain: ad-group | Source: keyword_performance_network_daily + "
                             f"keyword_instances (leads=primary_conv_count, "
                             f"revenue={PARAMS['kw_revenue_col']} - $-figures subject to the "
                             f"keyword-table dollar verdict on Params_QA) | Scope: {b['scope']}")
            sw.block(f"HEADLINE - type x network, {b['scope_recent']} - "
                     "$-figures certified", b["agg_recent"].round(2))
            sw.block("Full history - CvR/volume authoritative; $-figures DISTORTED "
                     "(value columns unpopulated before ~spring 2026)",
                     b["agg"].round(2))
            sw.block("Classification coverage (spend share by type)", b["coverage"].round(2))
            sw.block("Top 'other' (unclassified) ad groups by spend", b["other_names"].round(2))
            sw.block("Classification roster - every ad group and its assigned type "
                     "(rule order: Costco->affinity, Group/pooled->pool, "
                     "NonGeneric->conquest, Generic->generic, else->other)",
                     b["roster"].round(0))
        if results.get("B2"):
            b2r = results["B2"]
            sw = SheetWriter(writer, "B2_Conquest_Carriers",
                             "Module B2 - Conquest carrier break-out (masking check on "
                             "the GP-positive conquest-audience aggregate)",
                             f"Grain: carrier (parsed from Home-NonGeneric-<Carrier>) | "
                             f"Source: same as B | Scope: {b2r['scope']} | Sorted worst "
                             "audience GP first")
            sw.block("Carrier x network - audience columns first, search alongside "
                     "(low-lead carriers: treat GP sign as noise)",
                     b2r["by_carrier"].round(2))
            sw.block("Audience by carrier x campaign - localizes drains to specific "
                     "campaigns/tiers", b2r["by_campaign"].round(2))
        if results.get("C"):
            c = results["C"]
            sw = SheetWriter(writer, "C_RPL_Parity_State",
                             "Module C - RPL parity by state x network (tier-transfer check)",
                             f"Grain: state | Source: geo_performance_daily, windowed to "
                             f"the revenue-certified period | Conversion definition: "
                             f"{c['conv_def']} | Scope: {c['scope']}")
            sw.block("Parity summary", c["summary"])
            sw.block("Conversion-definition cross-check (geo leads vs keyword primary, "
                     "audience, last full month)", c["xcheck"])
            sw.block(f"Per-state RPL (min {PARAMS['c_min_leads_per_cell']} leads/cell to "
                     f"qualify; band ±{PARAMS['c_parity_band_pct']:.0f}%)",
                     c["by_state"].round(2))
        else:
            SheetWriter(writer, "C_RPL_Parity_State",
                        "Module C - BLOCKED (see Params_QA geo gate)",
                        "geo_performance_daily failed the network/geo_state/conversions gate")
        if "D" in results:
            d = results["D"]
            sw = SheetWriter(writer, "D_GP_Decline",
                             "Module D - WoW GP-decline decomposition",
                             "Grain: weekly network + ad-group concentration | Source: "
                             "keyword_performance_network_daily (leads=primary_conv_count, "
                             f"revenue={PARAMS['kw_revenue_col']}) "
                             f"| Decline window: {d['window']}")
            sw.block("Weekly GP by network bucket", d["weekly_gp"].round(0))
            sw.block("Largest WoW decline - network decomposition", d["decomp"].round(1))
            sw.block("Concentration: GP + leads delta by ad-group type x network in "
                     "decline window", d["concentration"].round(0))
            sw.block("Change events in/around the decline window (rule out a "
                     "self-inflicted search drop: budget/tCPA changes)", d["events"])
        if "E" in results:
            e = results["E"]
            sw = SheetWriter(writer, "E_tCPA_Blind_Spot",
                             "Module E - RPL dispersion on audience (what tCPA cannot see)",
                             "Grain: ad-group type, audience bucket only | Source: same as B "
                             "- $-figures subject to the keyword-table dollar verdict on "
                             "Params_QA; the DISPERSION pattern holds if the value column is "
                             "proportional to real revenue")
            sw.block("RPL / GP-per-lead by ad-group type on audience", e["dispersion"].round(2))
            sw.block("Conquest vs generic per-lead gap", e["gap"].round(2))

        # Params & QA
        sw = SheetWriter(writer, "Params_QA", "Parameters & QA",
                         "Every number upstream depends on these settings and checks")
        sw.block("Parameters", pd.DataFrame(
            [{"param": k, "value": str(v)} for k, v in PARAMS.items()]))
        sw.block("QA checks", pd.DataFrame(QA_ROWS))

        # ---- Deliverable #5: Citations sheet (fixed layout) + named ranges ----
        try:
            cit = []
            a = results.get("A", {})
            if "corr" in a:
                cdf = a["corr"]
                for per, key in (("consolidated", "CIT_XCORR_K0_CONS"),
                                 ("full history", "CIT_XCORR_K0_FULL")):
                    m = cdf[(cdf["period"].str.startswith(per)) & (cdf["lag_weeks_k"] == 0)]
                    if len(m):
                        cit.append((key, f"weekly first-diff xcorr k=0 ({per})",
                                    round(float(m["corr_first_diff"].iloc[0]), 3)))
            a3r = results.get("A3")
            if a3r is not None:
                s = a3r["signature"]
                acct = s[s["campaign"] == "ACCOUNT"]
                if len(acct):
                    r = acct.iloc[0]
                    cit += [("CIT_A3_ACCT_IMPR", "A3 acct corr aud-impr vs search",
                             r["corr_audImpr_vs_searchLeads"]),
                            ("CIT_A3_ACCT_CPC", "A3 acct corr aud-CPC vs search",
                             r["corr_audCPC_vs_searchLeads"]),
                            ("CIT_A3_ACCT_CVR", "A3 acct corr aud-CvR vs search",
                             r["corr_audCvR_vs_searchLeads"]),
                            ("CIT_A3_ACCT_DROPWK", "A3 acct drop-week impr/cpc/cvr idx",
                             f"{r.get('dropwk_impr_idx')}/{r.get('dropwk_cpc_idx')}/"
                             f"{r.get('dropwk_cvr_idx')}")]
                for _, r in s.iterrows():
                    nm = str(r["campaign"])
                    if "Tier2" in nm:
                        cit.append(("CIT_A3_T2_DROPWK", "A3 T2 drop-week impr/cvr idx",
                                    f"{r.get('dropwk_impr_idx')}/{r.get('dropwk_cvr_idx')}"))
                    if "Tier1" in nm:
                        cit.append(("CIT_A3_T1_DROPWK", "A3 T1 drop-week impr/cpc idx",
                                    f"{r.get('dropwk_impr_idx')}/{r.get('dropwk_cpc_idx')}"))
            c_res = results.get("C")
            if c_res:
                cs = c_res["summary"].iloc[0]
                qual = c_res["by_state"]
                med = qual[qual["meets_min_leads"]]["rpl_delta_pct"].median()
                cit += [("CIT_C_QUALIFIED", "C qualified states",
                         int(cs["states_meeting_min_leads"])),
                        ("CIT_C_WITHIN_BAND_PCT", "C share within band %",
                         cs["share_within_band_pct"]),
                        ("CIT_C_MEDIAN_DELTA", "C median aud-vs-search RPL delta %",
                         round(float(med), 1))]
            sw = SheetWriter(writer, "Citations",
                             "Citation anchors - FIXED layout; cite these cells / named "
                             "ranges, never raw block rows (block rows shift between runs)",
                             f"Regenerated each run from certified data | {len(cit)} anchors")
            cit_df = pd.DataFrame(cit, columns=["named_range", "label", "value"])
            sw.block("Anchors", cit_df)
            from openpyxl.workbook.defined_name import DefinedName
            ws_c = writer.sheets["Citations"]
            hdr_row = None
            for rr in range(1, ws_c.max_row + 1):
                if ws_c.cell(row=rr, column=1).value == "named_range":
                    hdr_row = rr
                    break
            if hdr_row:
                for k in range(len(cit_df)):
                    nm = cit_df.iloc[k]["named_range"]
                    ref = f"Citations!$C${hdr_row + 1 + k}"
                    try:
                        writer.book.defined_names[nm] = DefinedName(nm, attr_text=ref)
                    except Exception:
                        try:
                            writer.book.defined_names.append(DefinedName(nm, attr_text=ref))
                        except Exception:
                            pass
        except Exception:
            qa("Citations", "FAILED", traceback.format_exc(limit=3))

        add_charts(writer, results.get("A", {}))
        add_a2_charts(writer, results.get("A2", {}))

    print(f"\nWrote {out}")
    print("Review Params_QA first - especially the conversion cross-check and the "
          "spend reconcile figure.")


if __name__ == "__main__":
    main()
