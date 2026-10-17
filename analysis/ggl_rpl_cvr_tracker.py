#!/usr/bin/env python3
"""
ggl_rpl_cvr_tracker.py — Google Home RPL / CvR tracker workbook.

Automates the hand-maintained ggl_rpl_cvr_tracker.xlsx. Reads the
Google Ads UI export (campaign × date × device × state grain) and
generates a tracker workbook with six view sheets:

    State_Daily      State_Weekly      State_Monthly
    Campaign_Daily   Campaign_Weekly   Campaign_Monthly

Each view sheet stacks one block per metric per device section, rows =
entities (states or campaigns, TOTAL row first), columns = periods:

    Daily    last --days days (default 30) ending at the anchor date
    Weekly   last --weeks ISO Mon-Sun weeks (default 13), current
             partial week included and marked
    Monthly  calendar months YTD (Jan 1 of the anchor year → anchor),
             current partial month marked (MTD)

DEVICE SECTIONS. The original Microsoft template split each metric by
Search / Audience network; this Google account is Search-only, so the
split is by device instead: All Devices / Mobile / Non-Mobile.
Mobile = Device type 'Mobile phones'; Non-Mobile = All Devices − Mobile
(the subtraction convention, per the DB architect — it absorbs tablets
and any trace device types rather than silently dropping them).

METRICS. Mirrors campaign_performance_data_google.py's thirteen
(Impressions, Clicks, CTR, Leads, Revenue, Cost, CPC, CPL, CvR, ROAS,
RPL, GP, Margin) plus GP/Lead, which the hand tracker already carried.
Ratios are weighted at the aggregation level (sum numerator / sum
denominator per cell), never averaged.

VALUES, NOT FORMULAS. The hand template used SUMIFS into the Raw Data
sheet; this script computes every cell in pandas and writes static
values. The workbook is regenerated whenever data updates (that is the
automation), so live formulas would only slow file opens — with six
views the SUMIFS count would run six figures. The filtered raw rows are
still written to a Raw_Data sheet for auditability (--no-raw to skip).

FILTERS. Rows are kept when Location type == 'Physical location'
(matching the hand template's SUMIFS) — interest-location rows are
excluded so state attribution is physical.

CHARTS. Per metric, a line chart of the All-Devices section:
  - Campaign sheets: one series per OPEN campaign (spend > 0 in the
    last 14 days ending at the anchor), capped at --chart-series.
  - State sheets: one series per top state by window spend, same cap.
  - Monthly sheets: the same charts over YTD months — the requested
    YTD monthly performance visual.

NAVIGATION. Every sheet carries two hyperlink rows at the top: a sheet
bar (Index + all views + Raw_Data) and metric jump links into that
sheet's blocks.

DATA SOURCE. Two modes:

  DB mode (default) — queries paid_search.db on the local machine,
  reading the state-grain daily view campaign_performance_geo_daily_search
  (a _search view per the PMax-exclusion architecture; PMax is excluded
  by construction and reconciliation deltas equal to PMax spend are
  CORRECT). Campaign names/scoping come from campaigns_search (fallback:
  campaigns) via campaign_id, account_id=1. The geo table's exact column
  names are resolved at runtime against candidate lists (state/
  state_name/region..., cost_micros/cost/spend..., etc.) and the
  resolved mapping is printed — if a required column can't be found the
  script exits listing the table's actual columns so the mapping is a
  one-line fix. Device handling auto-detects the storage pattern:
    - rollup pattern: an 'ALL' device value exists → All Devices = ALL
      rows, Mobile = MOBILE rows, Non-Mobile = ALL − MOBILE (the
      account-wide anti-double-count convention);
    - native pattern: no rollup → mobile-like device values are Mobile
      and the complement is Non-Mobile.

  Raw mode (--raw) — one or more Google Ads UI exports (.csv, or .xlsx
  containing a 'Raw Data' sheet), concatenated and deduplicated on
  (campaign_id, date, device, state) keeping the last file's row.
  Physical-location filtering applies in this mode.

USAGE
    python ggl_rpl_cvr_tracker.py                      # DB mode
    python ggl_rpl_cvr_tracker.py --db-path other.db --account-id 1
    python ggl_rpl_cvr_tracker.py --geo-table my_geo_view
    python ggl_rpl_cvr_tracker.py --raw export.csv [export2.csv ...]

    --anchor-date   YYYY-MM-DD; default = MAX(date) in the source
    --days 30 / --weeks 13 / --chart-series 8 / --output-dir / --no-raw
"""

import argparse
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from openpyxl import Workbook, load_workbook
from openpyxl.chart import LineChart, Reference
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

# ═══════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════

DEFAULT_DB_PATH = Path(r"C:\\Projects\\google-ads-ops\\db\\paid_search.db")
DEFAULT_GEO_TABLE = "campaign_performance_geo_daily_search"
DEFAULT_ACCOUNT_ID = 1

DEFAULT_DAYS = 30
DEFAULT_WEEKS = 13
DEFAULT_CHART_SERIES = 8
OPEN_CAMPAIGN_LOOKBACK_DAYS = 14   # spend>0 in this window = "open"

INT_FMT = "#,##0"
MONEY_FMT = '"$"#,##0.00'
CONV_FMT = "#,##0.00"
ROAS_FMT = "0.00"
PCT_FMT = "0.00%"

# (key, label, fmt) — key is the derived-metric column produced by
# derive_metrics(). Order defines the block order on every sheet and
# mirrors campaign_performance_data_google.py, plus GP/Lead.
METRICS = [
    ("imp",    "A — Impressions",          INT_FMT),
    ("clk",    "B — Clicks",               INT_FMT),
    ("ctr",    "C — CTR (weighted)",       PCT_FMT),
    ("conv",   "D — Leads",                CONV_FMT),
    ("rev",    "E — Revenue",              MONEY_FMT),
    ("cost",   "F — Cost",                 MONEY_FMT),
    ("cpc",    "G — CPC (weighted)",       MONEY_FMT),
    ("cpl",    "H — CPL (weighted)",       MONEY_FMT),
    ("cvr",    "I — CvR (weighted)",       PCT_FMT),
    ("roas",   "J — ROAS (weighted)",      ROAS_FMT),
    ("rpl",    "K — RPL (weighted)",       MONEY_FMT),
    ("gp",     "L — GP",                   MONEY_FMT),
    ("margin", "M — Margin (weighted)",    PCT_FMT),
    ("gpl",    "N — GP/Lead (weighted)",   MONEY_FMT),
]
SECTIONS = ["All Devices", "Mobile", "Non-Mobile"]

VIEW_SHEETS = ["State_Daily", "State_Weekly", "State_Monthly",
               "Campaign_Daily", "Campaign_Weekly", "Campaign_Monthly"]

# Styles
TITLE_FONT = Font(bold=True, size=14)
NOTE_FONT = Font(italic=True, size=9, color="666666")
SECTION_FONT = Font(bold=True, size=11)
HEADER_FILL = PatternFill("solid", fgColor="1F4E79")
HEADER_FONT = Font(bold=True, color="FFFFFF", size=10)
BODY_FONT = Font(size=10)
TOTAL_FONT = Font(bold=True, size=10)
TOTAL_FILL = PatternFill("solid", fgColor="DDEBF7")
PARTIAL_FILL = PatternFill("solid", fgColor="FFF2CC")
LINK_FONT = Font(color="0563C1", underline="single", size=10)
THIN = Side(style="thin", color="D9D9D9")
BORDER = Border(top=THIN, bottom=THIN, left=THIN, right=THIN)


# ═══════════════════════════════════════════════════════════════════
# RAW DATA LOADING
# ═══════════════════════════════════════════════════════════════════

# Canonical column names ← accepted header spellings in the UI export.
COLMAP = {
    "campaign": ["Campaign name", "Campaign"],
    "campaign_id": ["Campaign ID"],
    "date": ["Date", "Day"],
    "device": ["Device type", "Device"],
    "state": ["State/province (physical location)", "State/province",
              "State"],
    "location_type": ["Location type"],
    "imp": ["Impressions", "Impr."],
    "clk": ["Clicks"],
    "cost": ["Spend", "Cost"],
    "conv": ["Conversions"],
    "rev": ["Revenue", "Conv. value"],
}


def _canonicalize(df: pd.DataFrame, source: str) -> pd.DataFrame:
    out = {}
    for canon, options in COLMAP.items():
        hit = next((o for o in options if o in df.columns), None)
        if hit is None:
            if canon in ("campaign_id", "location_type"):
                continue  # optional
            raise SystemExit(
                f"ERROR: {source}: none of {options} found "
                f"(columns: {list(df.columns)[:12]}...)")
        out[canon] = df[hit]
    return pd.DataFrame(out)


def load_raw(paths: list[str]) -> pd.DataFrame:
    frames = []
    for p in paths:
        path = Path(p)
        if not path.exists():
            raise SystemExit(f"ERROR: raw file not found: {path}")
        if path.suffix.lower() in (".xlsx", ".xlsm"):
            wb = load_workbook(path, read_only=True, data_only=True)
            sheet = "Raw Data" if "Raw Data" in wb.sheetnames else wb.sheetnames[0]
            rows = wb[sheet].iter_rows(values_only=True)
            header = next(rows)
            df = pd.DataFrame(rows, columns=header)
            df = df.dropna(how="all")
            wb.close()
        else:
            df = pd.read_csv(path, thousands=",")
        frames.append(_canonicalize(df, path.name))
        print(f"  loaded {len(frames[-1]):,} rows from {path.name}")

    df = pd.concat(frames, ignore_index=True)

    # Physical-location rows only (matches the hand template's SUMIFS).
    if "location_type" in df.columns:
        before = len(df)
        df = df[df["location_type"] == "Physical location"]
        if before - len(df):
            print(f"  filtered {before - len(df):,} non-physical-location rows")

    df["date"] = pd.to_datetime(df["date"]).dt.date
    for c in ("imp", "clk"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype(int)
    for c in ("cost", "conv", "rev"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0).astype(float)
    df["state"] = df["state"].fillna("(unknown)")
    df["is_mobile"] = df["device"] == "Mobile phones"

    # Dedupe across multiple exports: last file wins per grain row.
    key = ["campaign", "date", "device", "state"]
    if "campaign_id" in df.columns:
        key = ["campaign_id"] + key[1:]
    before = len(df)
    df = df.drop_duplicates(subset=key, keep="last")
    if before - len(df):
        print(f"  deduplicated {before - len(df):,} overlapping rows")
    return df.reset_index(drop=True)


# ═══════════════════════════════════════════════════════════════════
# DB MODE
# ═══════════════════════════════════════════════════════════════════

# Column mapping for the geo daily view — PINNED to the verified
# schema (DB architect fact sheet, 2026-07-30):
#   campaign_id, account_id, date, device, geo_target_constant_id,
#   geo_state, impressions, clicks, cost_micros, conversions,
#   conversion_value, all_conversions, all_conversion_value,
#   last_synced_at
# The verified names lead each candidate list, with fallbacks retained
# for portability (--geo-table at another view). The resolved mapping
# prints on every run; a miss exits with the table's actual columns.
#
# CONVERSION SEMANTICS (architect-verified): conversions /
# conversion_value are Google-NATIVE goal actions, not routed-primary —
# acceptable for state-grain RPL/CvR trends (native vs routed reconcile
# within ~1.5% at account level post-GBRAIDS promotion). Routed-primary
# does not exist at state grain; the keyword table is routed truth but
# has no state axis. NEVER mix this table's conversions with kpd routed
# numbers inside one metric. all_conversions / all_conversion_value are
# deliberately ABSENT from the candidates — they include monitor-mode
# actions (e.g. the dormant EQ trickle) and must not be picked up even
# by fallthrough.
DB_CANDIDATES = {
    "campaign_id": ["campaign_id"],
    "date": ["date", "report_date", "day"],
    "device": ["device", "device_type"],          # optional
    "state": ["geo_state", "state", "state_name", "region",
              "state_province", "province", "location_name"],
    "imp": ["impressions", "impr"],
    "clk": ["clicks"],
    "cost": ["cost_micros", "cost", "spend", "cost_usd"],
    "conv": ["conversions", "primary_conv_count", "conv", "leads"],
    "rev": ["conversion_value", "primary_conv_value", "revenue",
            "conv_value"],
}
MOBILE_DEVICE_VALUES = {"MOBILE", "MOBILE_PHONES", "Mobile phones",
                        "Mobile", "mobile"}


def _table_columns(conn, table: str) -> list[str]:
    try:
        cur = conn.execute(f"SELECT * FROM {table} LIMIT 0")
    except sqlite3.Error as exc:
        raise SystemExit(f"ERROR: cannot read {table}: {exc}")
    return [d[0] for d in cur.description]


def load_db(db_path: Path, geo_table: str, account_id: int) -> pd.DataFrame:
    if not db_path.exists():
        raise SystemExit(f"ERROR: DB not found at {db_path}")
    conn = sqlite3.connect(db_path)

    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    if geo_table not in tables:
        geo_like = sorted(t for t in tables if "geo" in t.lower())
        raise SystemExit(
            f"ERROR: {geo_table} not found in {db_path.name}.\n"
            f"  Geo-like tables/views present: {geo_like or '(none)'}\n"
            f"  Point --geo-table at the right one.")

    cols = _table_columns(conn, geo_table)
    resolved, missing = {}, []
    for canon, options in DB_CANDIDATES.items():
        hit = next((o for o in options if o in cols), None)
        if hit is None and canon != "device":
            missing.append((canon, options))
        elif hit is not None:
            resolved[canon] = hit
    if missing:
        lines = "\n".join(f"    {c}: tried {o}" for c, o in missing)
        raise SystemExit(
            f"ERROR: {geo_table} is missing required columns:\n{lines}\n"
            f"  Actual columns: {cols}\n"
            f"  If the data lives under different names, add them to "
            f"DB_CANDIDATES (one line) or check with the DB architect.")
    print(f"  {geo_table} column mapping: " +
          ", ".join(f"{k}←{v}" for k, v in resolved.items()))

    # Campaign names come from campaigns_search per the PMax
    # architecture (fallback: campaigns). Account scoping: the geo view
    # carries account_id NATIVELY (unlike the keyword perf tables), so
    # filter g.account_id directly when present — cheaper than scoping
    # through the join, which remains the fallback.
    camp_table = "campaigns_search" if "campaigns_search" in tables else "campaigns"
    camp_cols = _table_columns(conn, camp_table)
    name_col = next((c for c in ("campaign_name", "name") if c in camp_cols),
                    None)
    if name_col is None:
        raise SystemExit(f"ERROR: {camp_table} has no campaign name column "
                         f"(columns: {camp_cols})")
    native_acct = "account_id" in cols
    acct_pred = ("g.account_id = ?" if native_acct
                 else "c.account_id = ?")

    sel = [f"g.{resolved['campaign_id']} AS campaign_id",
           f"c.{name_col} AS campaign",
           f"g.{resolved['date']} AS date",
           f"g.{resolved['state']} AS state",
           f"SUM(g.{resolved['imp']}) AS imp",
           f"SUM(g.{resolved['clk']}) AS clk",
           f"SUM(g.{resolved['cost']}) AS cost",
           f"SUM(g.{resolved['conv']}) AS conv",
           f"SUM(g.{resolved['rev']}) AS rev"]
    group = [f"g.{resolved['campaign_id']}", f"c.{name_col}",
             f"g.{resolved['date']}", f"g.{resolved['state']}"]
    has_device = "device" in resolved
    if has_device:
        sel.insert(4, f"g.{resolved['device']} AS device")
        group.append(f"g.{resolved['device']}")

    sql = (f"SELECT {', '.join(sel)} FROM {geo_table} g "
           f"JOIN {camp_table} c ON c.campaign_id = g.{resolved['campaign_id']} "
           f"WHERE {acct_pred} "
           f"GROUP BY {', '.join(group)}")
    df = pd.read_sql(sql, conn, params=(account_id,))
    conn.close()
    if df.empty:
        raise SystemExit(f"ERROR: no rows for account_id={account_id} "
                         f"in {geo_table}")
    print(f"  {len(df):,} grain rows from {geo_table} "
          f"(account_id={account_id} "
          f"{'native' if native_acct else 'via ' + camp_table}; "
          f"names via {camp_table})")

    df["date"] = pd.to_datetime(df["date"]).dt.date
    if resolved["cost"] == "cost_micros":
        df["cost"] = df["cost"] / 1.0e6
    for c in ("imp", "clk"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype(int)
    for c in ("cost", "conv", "rev"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0).astype(float)
    # Geographic attribution: Google's geographic view drops
    # unattributable volume, so state sums may run slightly below
    # campaign totals — a small structural gap is CORRECT, never
    # "reconciled away". NULL/blank geo_state rows are kept in an
    # explicit Unattributed bucket (architect verification 2026-07-30:
    # currently 0% of impressions — this is a cheap guard, expected
    # empty in practice).
    df["state"] = df["state"].fillna("").astype(str)
    blank = df["state"].str.strip() == ""
    if blank.any():
        n = int(df.loc[blank, "imp"].sum())
        print(f"  ⚠ {blank.sum():,} rows with blank geo_state "
              f"({n:,} impressions) → bucketed as 'Unattributed'")
    df.loc[blank, "state"] = "Unattributed"

    if not has_device:
        raise SystemExit(
            f"ERROR: {geo_table} has no device column — the tracker's "
            f"Mobile / Non-Mobile sections need one. Check with the DB "
            f"architect whether geo data is stored at device grain.")

    # Device pattern (architect verification 2026-07-30): THIS view is
    # NATIVE — DESKTOP / MOBILE / TABLET only, no 'ALL' rollup — so the
    # native branch below fires: All Devices = sum of splits (nothing to
    # double-count), Mobile = MOBILE, Non-Mobile = DESKTOP + TABLET.
    # Do NOT assume symmetry with campaign_performance_daily_search,
    # which DOES carry an 'ALL' rollup. Two tables, two patterns, both
    # documented; the rollup branch is kept for other --geo-table
    # targets.
    devices = set(df["device"].dropna().unique())
    if "ALL" in devices:
        # Rollup pattern (account convention): All Devices = ALL rows;
        # Mobile = MOBILE rows; Non-Mobile synthesized as ALL − MOBILE
        # so trace device types are absorbed, never double-counted.
        print(f"  device pattern: rollup (values: {sorted(devices)}) — "
              f"All=ALL, Mobile=MOBILE, Non-Mobile=ALL−MOBILE")
        key = ["campaign_id", "campaign", "date", "state"]
        alls = df[df["device"] == "ALL"].groupby(key)[BASE].sum()
        mob = (df[df["device"].isin(MOBILE_DEVICE_VALUES)]
               .groupby(key)[BASE].sum())
        mob = mob.reindex(alls.index).fillna(0.0)
        nonmob = (alls - mob).clip(lower=0)
        mob["is_mobile"], nonmob["is_mobile"] = True, False
        out = pd.concat([mob.reset_index(), nonmob.reset_index()],
                        ignore_index=True)
        for c in ("imp", "clk"):
            out[c] = out[c].round().astype(int)
        return out
    # Native pattern: per-device rows only — complement split.
    print(f"  device pattern: native per-device rows "
          f"(values: {sorted(devices)})")
    df["is_mobile"] = df["device"].isin(MOBILE_DEVICE_VALUES)
    return df.drop(columns=["device"]).reset_index(drop=True)


# ═══════════════════════════════════════════════════════════════════
# PERIODS
# ═══════════════════════════════════════════════════════════════════

def build_periods(view: str, anchor: date, n_days: int, n_weeks: int):
    """Return list of (label, start, end, is_partial) for a view kind."""
    periods = []
    if view == "daily":
        for i in range(n_days - 1, -1, -1):
            d = anchor - timedelta(days=i)
            periods.append((d.strftime("%m/%d"), d, d, False))
    elif view == "weekly":
        anchor_mon = anchor - timedelta(days=anchor.weekday())
        for i in range(n_weeks - 1, -1, -1):
            mon = anchor_mon - timedelta(weeks=i)
            sun = mon + timedelta(days=6)
            partial = sun > anchor
            end = min(sun, anchor)
            lbl = mon.strftime("%m/%d") + (" (partial)" if partial else "")
            periods.append((lbl, mon, end, partial))
    elif view == "monthly":
        m = date(anchor.year, 1, 1)
        while m <= anchor:
            nxt = (date(m.year + 1, 1, 1) if m.month == 12
                   else date(m.year, m.month + 1, 1))
            month_end = nxt - timedelta(days=1)
            partial = month_end > anchor
            end = min(month_end, anchor)
            lbl = m.strftime("%b %Y") + (" (MTD)" if partial else "")
            periods.append((lbl, m, end, partial))
            m = nxt
    return periods


# ═══════════════════════════════════════════════════════════════════
# AGGREGATION
# ═══════════════════════════════════════════════════════════════════

BASE = ["imp", "clk", "cost", "conv", "rev"]


def section_frame(df: pd.DataFrame, section: str) -> pd.DataFrame:
    if section == "Mobile":
        return df[df["is_mobile"]]
    if section == "Non-Mobile":
        # Subtraction convention implemented by complement: with no ALL
        # rollup rows in the UI export, All Devices − Mobile ≡ the
        # non-mobile device rows, trace types included.
        return df[~df["is_mobile"]]
    return df


def aggregate(df: pd.DataFrame, entity_col: str, entities: list[str],
              periods, section: str) -> dict[str, pd.DataFrame]:
    """→ {base_metric: DataFrame[rows=TOTAL+entities, cols=period idx]}"""
    sub = section_frame(df, section)
    idx = ["TOTAL"] + entities
    out = {b: pd.DataFrame(0.0, index=idx, columns=range(len(periods)))
           for b in BASE}
    for j, (_lbl, start, end, _p) in enumerate(periods):
        window = sub[(sub["date"] >= start) & (sub["date"] <= end)]
        by_ent = window.groupby(entity_col)[BASE].sum()
        tot = window[BASE].sum()
        for b in BASE:
            out[b].iloc[0, j] = tot[b]
            aligned = by_ent[b].reindex(entities).fillna(0.0)
            out[b].iloc[1:, j] = aligned.values
    return out


def derive_metrics(base: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Add weighted ratio metrics; zero denominators → NaN (blank cell)."""
    imp, clk = base["imp"], base["clk"]
    cost, conv, rev = base["cost"], base["conv"], base["rev"]
    gp = rev - cost

    def ratio(num, den):
        return num.where(den != 0).div(den.where(den != 0))

    return {
        **base,
        "ctr": ratio(clk, imp),
        "cpc": ratio(cost, clk),
        "cpl": ratio(cost, conv),
        "cvr": ratio(conv, clk),
        "roas": ratio(rev, cost),
        "rpl": ratio(rev, conv),
        "gp": gp,
        "margin": ratio(gp, rev),
        "gpl": ratio(gp, conv),
    }


# ═══════════════════════════════════════════════════════════════════
# SHEET WRITING
# ═══════════════════════════════════════════════════════════════════

def write_sheet_nav(ws, row: int) -> int:
    """Sheet bar: links to Index, every view sheet, and Raw_Data."""
    ws.cell(row=row, column=1, value="Sheets:").font = TOTAL_FONT
    col = 2
    for name in ["Index"] + VIEW_SHEETS + ["Raw_Data"]:
        c = ws.cell(row=row, column=col, value=name)
        c.hyperlink = f"#'{name}'!A1"
        c.font = LINK_FONT
        col += 1
    return row + 1


def write_view_sheet(ws, view_kind: str, entity_kind: str,
                     entities: list[str], periods,
                     metrics_by_section: dict[str, dict[str, pd.DataFrame]],
                     chart_entities: list[str], anchor: date,
                     source_note: str = "the Google Ads UI export"):
    """Write one view sheet: nav, 14 metrics × 3 device sections, charts.

    metrics_by_section: {section: {metric_key: DataFrame}}
    chart_entities: entity subset plotted on the per-metric charts.
    """
    n_per = len(periods)
    n_ent = len(entities) + 1  # + TOTAL row

    view_label = {"daily": "Daily", "weekly": "Weekly (ISO Mon-Sun)",
                  "monthly": "Monthly (calendar, YTD)"}[view_kind]
    ws.cell(row=1, column=1,
            value=f"{entity_kind} × {view_label}").font = TITLE_FONT
    ws.cell(row=2, column=1, value=(
        f"Generated from {source_note}, "
        f"anchored to {anchor}. Rows = {entity_kind.lower()}s with a "
        f"TOTAL row first; columns = periods, most recent last; partial "
        f"periods are amber. Each metric repeats in three device sections: "
        f"All Devices, Mobile, and Non-Mobile (= All Devices − Mobile). "
        f"Ratios are weighted per cell. Values are computed at build time "
        f"— regenerate the workbook to refresh. The chart beside each "
        f"metric plots the All-Devices section for "
        f"{'open campaigns' if entity_kind == 'Campaign' else 'top states'}."
    )).font = NOTE_FONT

    nav_row = write_sheet_nav(ws, 4)

    # Geometry: per metric = 3 sections; per section = label + 2 header
    # rows + entity rows + 1 spacer; +1 extra spacer between metrics.
    sec_stride = 3 + n_ent + 1
    metric_stride = 3 * sec_stride + 1
    LINKS_PER_ROW = 5
    n_nav_rows = (len(METRICS) + LINKS_PER_ROW - 1) // LINKS_PER_ROW
    first_label_row = nav_row + n_nav_rows + 2

    # Metric jump links
    ws.cell(row=nav_row, column=1, value="Jump to:").font = TOTAL_FONT
    for i, (_k, label, _f) in enumerate(METRICS):
        r = nav_row + i // LINKS_PER_ROW
        c = 2 + (i % LINKS_PER_ROW)
        target = first_label_row + i * metric_stride
        cell = ws.cell(row=r, column=c, value=label.split("— ")[1])
        cell.hyperlink = f"#'{ws.title}'!A{target}"
        cell.font = LINK_FONT

    date_fmt = {"daily": "mm/dd", "weekly": "yyyy-mm-dd",
                "monthly": "mmm yyyy"}[view_kind]

    for mi, (key, label, fmt) in enumerate(METRICS):
        metric_top = first_label_row + mi * metric_stride
        for si, section in enumerate(SECTIONS):
            top = metric_top + si * sec_stride
            frame = metrics_by_section[section][key]

            ws.cell(row=top, column=1,
                    value=f"{label} — {section}").font = SECTION_FONT

            # period_start / period_end header rows
            ws.cell(row=top + 1, column=1, value="period_start →").font = BODY_FONT
            ws.cell(row=top + 2, column=1, value="period_end →").font = BODY_FONT
            for j, (_lbl, start, end, partial) in enumerate(periods):
                for rr, val in ((top + 1, start), (top + 2, end)):
                    c = ws.cell(row=rr, column=2 + j, value=val)
                    c.number_format = date_fmt
                    c.fill = HEADER_FILL
                    c.font = HEADER_FONT
                    c.alignment = Alignment(horizontal="center")

            # entity rows: TOTAL first
            for ei, ent in enumerate(["TOTAL"] + entities):
                r = top + 3 + ei
                name_cell = ws.cell(row=r, column=1, value=ent)
                name_cell.font = TOTAL_FONT if ei == 0 else BODY_FONT
                if ei == 0:
                    name_cell.fill = TOTAL_FILL
                for j, (_lbl, _s, _e, partial) in enumerate(periods):
                    v = frame.iloc[ei, j]
                    cell = ws.cell(row=r, column=2 + j,
                                   value=None if pd.isna(v) else float(v))
                    cell.number_format = fmt
                    cell.font = TOTAL_FONT if ei == 0 else BODY_FONT
                    cell.border = BORDER
                    if partial:
                        cell.fill = PARTIAL_FILL
                    elif ei == 0:
                        cell.fill = TOTAL_FILL

        # ── Chart: All-Devices section, one series per chart entity ──
        chart = LineChart()
        chart.title = f"{label.split('— ')[1]} — {ws.title}"
        chart.style = 12
        chart.height = min(12.0, max(7.0, 0.42 * metric_stride))
        chart.width = 20.0
        chart.y_axis.majorGridlines = None
        chart.y_axis.numFmt = fmt
        chart.x_axis.numFmt = date_fmt
        chart.y_axis.delete = False
        chart.x_axis.delete = False
        if chart.legend is not None:
            chart.legend.position = "b"

        all_dev_top = metric_top  # section 0
        cats = Reference(ws, min_col=2, max_col=1 + n_per,
                         min_row=all_dev_top + 1, max_row=all_dev_top + 1)
        ent_rows = {e: i for i, e in enumerate(["TOTAL"] + entities)}
        for ent in chart_entities:
            if ent not in ent_rows:
                continue
            r = all_dev_top + 3 + ent_rows[ent]
            series_ref = Reference(ws, min_col=1, max_col=1 + n_per,
                                   min_row=r, max_row=r)
            chart.add_data(series_ref, titles_from_data=True, from_rows=True)
        chart.set_categories(cats)
        for s in chart.series:
            s.smooth = False

        anchor_col = get_column_letter(n_per + 4)
        ws.add_chart(chart, f"{anchor_col}{metric_top}")

    # widths + freeze
    ws.column_dimensions["A"].width = 46
    for c in range(2, n_per + 2):
        ws.column_dimensions[get_column_letter(c)].width = 11
    ws.freeze_panes = f"B{first_label_row}"


def write_index(ws, anchor: date, args, n_states: int, n_campaigns: int,
                open_campaigns: list[str], top_states: list[str],
                date_min: date, date_max: date, n_rows: int,
                source_note: str):
    ws.cell(row=1, column=1,
            value="Google Home — RPL / CvR Tracker").font = TITLE_FONT
    ws.cell(row=2, column=1, value=(
        "Automated replacement for the hand-maintained tracker. Six views "
        "(State/Campaign × Daily/Weekly/Monthly), each metric split by "
        "device (All Devices / Mobile / Non-Mobile), values computed at "
        "build time from the Google Ads UI export.")).font = NOTE_FONT

    rows = [
        ("Anchor date", str(anchor)),
        ("Data source", source_note),
        ("Raw data span", f"{date_min} → {date_max}  ({n_rows:,} rows kept)"),
        ("Daily window", f"last {args.days} days"),
        ("Weekly window", f"last {args.weeks} ISO weeks (partial current included)"),
        ("Monthly window", f"YTD {anchor.year} (current month = MTD)"),
        ("States tracked", str(n_states)),
        ("Campaigns tracked", str(n_campaigns)),
        ("Open campaigns (charted)",
         f"{len(open_campaigns)} — spend > 0 in last "
         f"{OPEN_CAMPAIGN_LOOKBACK_DAYS} days"),
        ("Top states (charted)", ", ".join(top_states)),
    ]
    r = 4
    for k, v in rows:
        ws.cell(row=r, column=1, value=k).font = TOTAL_FONT
        ws.cell(row=r, column=2, value=v).font = BODY_FONT
        r += 1

    r += 1
    ws.cell(row=r, column=1, value="Sheets:").font = TOTAL_FONT
    r += 1
    for name in VIEW_SHEETS + ["Raw_Data"]:
        c = ws.cell(row=r, column=1, value=name)
        c.hyperlink = f"#'{name}'!A1"
        c.font = LINK_FONT
        r += 1
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 80


def write_raw_sheet(ws, df: pd.DataFrame):
    cols = ["campaign", "campaign_id", "date", "device", "state",
            "imp", "clk", "cost", "conv", "rev"]
    cols = [c for c in cols if c in df.columns]
    for j, c in enumerate(cols, 1):
        cell = ws.cell(row=1, column=j, value=c)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
    for i, row in enumerate(df[cols].itertuples(index=False), 2):
        for j, v in enumerate(row, 1):
            ws.cell(row=i, column=j,
                    value=v if not isinstance(v, date) else v)
    ws.column_dimensions["A"].width = 46
    ws.freeze_panes = "A2"


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db-path", default=str(DEFAULT_DB_PATH),
                   help="paid_search.db (DB mode, the default)")
    p.add_argument("--geo-table", default=DEFAULT_GEO_TABLE,
                   help=f"State-grain daily view to read. "
                        f"Default: {DEFAULT_GEO_TABLE}")
    p.add_argument("--account-id", type=int, default=DEFAULT_ACCOUNT_ID)
    p.add_argument("--raw", nargs="+", default=None,
                   help="Raw mode instead of DB: Google Ads UI export(s) "
                        "(.csv, or .xlsx with a 'Raw Data' sheet)")
    p.add_argument("--output-dir", default=".")
    p.add_argument("--anchor-date", default=None,
                   help="YYYY-MM-DD; default = MAX(date) in the source")
    p.add_argument("--days", type=int, default=DEFAULT_DAYS)
    p.add_argument("--weeks", type=int, default=DEFAULT_WEEKS)
    p.add_argument("--chart-series", type=int, default=DEFAULT_CHART_SERIES,
                   help="Max series per chart (open campaigns / top states)")
    p.add_argument("--no-raw", action="store_true",
                   help="Skip the Raw_Data sheet")
    return p.parse_args()


def main():
    args = parse_args()
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.raw:
        print("Loading raw export(s)...")
        df = load_raw(args.raw)
        source_note = f"UI export ({len(args.raw)} file(s))"
    else:
        print(f"Querying {args.db_path} ...")
        df = load_db(Path(args.db_path), args.geo_table, args.account_id)
        source_note = (f"{args.geo_table} (account_id={args.account_id}; "
                       f"PMax excluded by _search architecture)")
    date_min, date_max = df["date"].min(), df["date"].max()
    anchor = (date.fromisoformat(args.anchor_date) if args.anchor_date
              else date_max)
    print(f"  {len(df):,} rows kept | {date_min} → {date_max} | "
          f"anchor {anchor}")

    # Entities, ordered by all-window spend desc; retired (zz-) last.
    camp_spend = df.groupby("campaign")["cost"].sum()
    campaigns = sorted(camp_spend.index,
                       key=lambda c: (c.lower().startswith("zz"),
                                      -camp_spend[c]))
    state_spend = df.groupby("state")["cost"].sum()
    states = sorted(state_spend.index, key=lambda s: -state_spend[s])

    open_cut = anchor - timedelta(days=OPEN_CAMPAIGN_LOOKBACK_DAYS - 1)
    recent = df[df["date"] >= open_cut]
    open_spend = recent.groupby("campaign")["cost"].sum()
    open_campaigns = [c for c in campaigns
                      if open_spend.get(c, 0) > 0][:args.chart_series]
    top_states = states[:args.chart_series]
    print(f"  {len(states)} states | {len(campaigns)} campaigns "
          f"({len(open_campaigns)} open → charted)")

    wb = Workbook()
    wb.remove(wb.active)
    ws_idx = wb.create_sheet("Index")

    views = [("State_Daily", "daily", "State", states, top_states),
             ("State_Weekly", "weekly", "State", states, top_states),
             ("State_Monthly", "monthly", "State", states, top_states),
             ("Campaign_Daily", "daily", "Campaign", campaigns, open_campaigns),
             ("Campaign_Weekly", "weekly", "Campaign", campaigns, open_campaigns),
             ("Campaign_Monthly", "monthly", "Campaign", campaigns, open_campaigns)]

    for sheet_name, view_kind, ent_kind, entities, chart_ents in views:
        periods = build_periods(view_kind, anchor, args.days, args.weeks)
        ent_col = "state" if ent_kind == "State" else "campaign"
        metrics_by_section = {}
        for section in SECTIONS:
            base = aggregate(df, ent_col, entities, periods, section)
            metrics_by_section[section] = derive_metrics(base)
        ws = wb.create_sheet(sheet_name)
        write_view_sheet(ws, view_kind, ent_kind, entities, periods,
                         metrics_by_section, chart_ents, anchor,
                         source_note=source_note)
        print(f"  ✓ {sheet_name} ({len(periods)} periods × "
              f"{len(entities)+1} rows × {len(METRICS)}×3 blocks)")

    if not args.no_raw:
        ws_raw = wb.create_sheet("Raw_Data")
        write_raw_sheet(ws_raw, df)
        print(f"  ✓ Raw_Data ({len(df):,} rows)")
    else:
        # Sheet nav links point at Raw_Data; create a stub explaining.
        ws_raw = wb.create_sheet("Raw_Data")
        ws_raw.cell(row=1, column=1,
                    value="Raw data omitted (--no-raw).").font = NOTE_FONT

    write_index(ws_idx, anchor, args, len(states), len(campaigns),
                open_campaigns, top_states, date_min, date_max, len(df),
                source_note)

    out_path = out_dir / f"ggl_rpl_cvr_tracker_{anchor.isoformat()}.xlsx"
    wb.save(out_path)
    print(f"\n  ✓ Wrote {out_path}")

    # Console recap for a quick sanity read
    w = df[(df["date"] >= anchor - timedelta(days=args.days - 1))
           & (df["date"] <= anchor)]
    m = w[w["is_mobile"]]
    print(f"\nLast {args.days} days: cost ${w['cost'].sum():,.2f} | "
          f"leads {w['conv'].sum():,.1f} | rev ${w['rev'].sum():,.2f} | "
          f"mobile cost share "
          f"{(m['cost'].sum()/w['cost'].sum()*100 if w['cost'].sum() else 0):.1f}%")


if __name__ == "__main__":
    main()
