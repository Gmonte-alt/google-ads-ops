"""
08_load_microsoft_campaigns.py
==============================
Campaign Structure Analytics — Microsoft Ads (CSV-based)
Folder: C:\\Projects\\google-ads-ops\\campaign_db_scripts\\

Loads Microsoft campaign and ad group structure from three CSV exports
into the campaigns and ad_groups tables.

WHY THREE CSVs
    The Microsoft Ads UI does not expose all needed fields from any single
    export. We pull from three sources and join them by campaign_id:

    1. Reports > Reports > Campaign (daily=None)
         -> campaign_name, status, campaign_type
         Status, ID, name, type are reliable from the Reports builder.

    2. Campaigns grid (main left-nav Campaigns view, Download button)
         -> budget amount (dollars), bid strategy type, target CPA,
            target ROAS, bid strategy name (portfolio strategy label)
         The Reports builder does NOT expose budget amount or bid
         strategy fields. The grid download does.

    3. Reports > Reports > Ad Group (daily=None)
         -> ad group name, id, status, type, campaign association

SCHEMA NOTES
    campaigns.account_id        = INTEGER accounts.id (resolved once per run)
    campaigns.campaign_id       = Base campaign ID stripped of brackets
    campaigns.campaign_type     = "SEARCH" (normalized to Google enum)
    campaigns.advertising_channel_sub_type
                                = "Search & content" for Hybrid campaigns
                                  (Microsoft native label; NULL otherwise)
    campaigns.status            = ENABLED / PAUSED / REMOVED (Google enum)
    campaigns.daily_budget_micros
                                = grid Budget (dollars) * 1,000,000
    campaigns.is_shared_budget  = 1 if Budget Type = SharedBudget, else 0
    campaigns.bidding_strategy_type, target_cpa_micros, target_roas
                                = from grid CSV

    ad_groups.account_id        = INTEGER accounts.id
    ad_groups.campaign_id       = Base campaign ID (bracket stripped)
    ad_groups.ad_group_id       = stripped of brackets
    ad_groups.status            = ENABLED / PAUSED / REMOVED (Google enum)
    ad_groups.ad_group_type     = STANDARD (Google convention; Microsoft
                                  emits "Standard")

    FK enforcement is ON. Campaigns are written before ad groups in the
    same transaction; ad groups whose campaign_id is not in this run's
    campaigns batch are skipped with a warning.

MaxConversions STRATEGY WITH tCPA
    In Microsoft, MaxConversions can carry a tCPA value which is a
    CEILING, not a target. We store it in target_cpa_micros regardless,
    matching the grid field, and document that consumers of the field
    should check bidding_strategy_type to interpret it.

IDEMPOTENCY
    INSERT OR REPLACE on campaigns.campaign_id and ad_groups.ad_group_id.
    first_seen_date preserved on existing rows via get_first_seen().
    last_synced_at refreshed every run.

Usage:
    conda activate paid-search

    # All three CSVs provided explicitly
    python campaign_db_scripts/08_load_microsoft_campaigns.py \\
        --campaigns-csv "input/microsoft-home-campaigns-20260420.csv" \\
        --campaigns-grid-csv "input/microsoft-home-campaigns-grid-20260420.csv" \\
        --adgroups-csv "input/microsoft-home-adgroups-20260420.csv"

    # Dry run
    python campaign_db_scripts/08_load_microsoft_campaigns.py \\
        --campaigns-csv ... --campaigns-grid-csv ... --adgroups-csv ... \\
        --dry-run
"""

import sqlite3
import argparse
import logging
import re
from pathlib import Path
from datetime import date, datetime, timezone
import pandas as pd


# ── Configuration ─────────────────────────────────────────────────────────────

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
TODAY   = date.today().isoformat()

# Microsoft account details for Home Insurance — matches 04_load_microsoft_csv.py
MS_ACCOUNT_NAME   = "Home Insurance"
MS_ACCOUNT_NUMBER = "F149SN63"
MS_PLATFORM       = "microsoft"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Status normalization (shared with keyword loader) ─────────────────────────

STATUS_MAP = {
    "Active":   "ENABLED",
    "Enabled":  "ENABLED",
    "Paused":   "PAUSED",
    "Deleted":  "REMOVED",
    "Removed":  "REMOVED",
}


# ── Campaign type / subtype rules ─────────────────────────────────────────────
# All Microsoft Search & content campaigns are Hybrid (search + audience in
# one campaign). We store campaign_type normalized to Google's enum and the
# Microsoft native subtype in advertising_channel_sub_type.
# Reports-export "Campaign type" column values we have seen: "Search & content"
# Grid-export "Campaign Type" column values we have seen: "Search"
# If Microsoft introduces a new campaign type (Shopping, Audience-only, etc.)
# it will show up here and need an addition to this mapping.

CAMPAIGN_TYPE_MAP = {
    # Reports export values
    "Search & content": ("SEARCH", "Search & content"),

    # Grid export values (less specific but still seen)
    "Search":           ("SEARCH", None),
}


# ── Ad group type normalization ───────────────────────────────────────────────

AD_GROUP_TYPE_MAP = {
    "Standard":  "STANDARD",
    "Audience":  "AUDIENCE",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def strip_brackets(value) -> str | None:
    """Removes Microsoft's bracket wrapping from ID fields. "[123]" -> "123"."""
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    s = str(value).strip()
    if not s or s == "-" or s.lower() == "nan":
        return None
    return s.strip("[]")


def none_if_blank(value):
    """Return None for blank/whitespace/'-'/NaN, else the stripped value."""
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    s = str(value).strip()
    if not s or s == "-" or s.lower() == "nan":
        return None
    return s


def parse_numeric_or_none(value) -> float | None:
    """Parse a CSV cell to float, returning None for blank / '-' / NaN."""
    s = none_if_blank(value)
    if s is None:
        return None
    s = s.replace(",", "").replace("$", "").strip()
    # Strip percent sign if present (shouldn't be in these fields but defensive)
    if s.endswith("%"):
        s = s[:-1]
    try:
        return float(s)
    except ValueError:
        return None


def dollars_to_micros(dollars) -> int | None:
    """Convert a dollar float to Google's cost_micros convention."""
    f = parse_numeric_or_none(dollars)
    if f is None:
        return None
    return int(round(f * 1_000_000))


def get_account_internal_id(cursor) -> int | None:
    """Resolve accounts.id for the Microsoft Home Insurance account."""
    row = cursor.execute(
        "SELECT id FROM accounts WHERE platform_account_id = ? AND platform = ?",
        (MS_ACCOUNT_NUMBER, MS_PLATFORM),
    ).fetchone()
    return row["id"] if row else None


def get_first_seen(cursor, table: str, pk_col: str, pk_val: str) -> str:
    """Return existing first_seen_date for a row, or today for new rows."""
    row = cursor.execute(
        f"SELECT first_seen_date FROM {table} WHERE {pk_col} = ?", (pk_val,)
    ).fetchone()
    return row["first_seen_date"] if row and row["first_seen_date"] else TODAY


# ── CSV readers ───────────────────────────────────────────────────────────────

def read_reports_export(filepath: Path) -> pd.DataFrame:
    """
    Read a Microsoft Reports > Reports CSV export (tab-delimited, though
    some exports come comma-delimited with quotes). We detect the header
    by scanning for 'Account name' per the keyword loader pattern.

    Handles:
      - Metadata header rows (Report Name, Report Time, Time Zone, etc.)
      - UTF-8 BOM (encoding='utf-8-sig')
      - Both tab-delimited and comma-delimited variants
      - Footer Total row and copyright notice
    """
    if not filepath.exists():
        raise FileNotFoundError(f"CSV not found: {filepath}")

    # Probe: read raw lines, find header, detect delimiter
    with open(filepath, encoding="utf-8-sig") as f:
        lines = f.readlines()

    header_idx = None
    for i, line in enumerate(lines):
        stripped = line.lstrip('\ufeff').lstrip()
        if stripped.startswith('"Account name"') or stripped.startswith("Account name"):
            header_idx = i
            break

    if header_idx is None:
        raise ValueError(
            f"Could not find header row starting with 'Account name' in {filepath.name}. "
            "Is this the correct Microsoft Reports export?"
        )

    # Delimiter sniff: count tabs vs commas on the header line
    header_line = lines[header_idx]
    sep = "\t" if header_line.count("\t") > header_line.count(",") else ","

    df = pd.read_csv(
        filepath,
        skiprows=header_idx,
        sep=sep,
        encoding="utf-8-sig",
        dtype=str,
        thousands=",",
    )

    # Strip footer Total row
    first_col = df.columns[0]
    df = df[df[first_col].notna()]
    df = df[df[first_col] != "Total"]
    df = df[~df[first_col].astype(str).str.startswith("©")]

    # Strip whitespace
    for col in df.select_dtypes(include=["object","string"]).columns:
        df[col] = df[col].str.strip()

    return df


def read_grid_export(filepath: Path) -> pd.DataFrame:
    """
    Read a Microsoft Campaigns grid CSV export.

    Format differs from Reports exports:
      - Line 1: '"Campaign report (date range)"'
      - Line 2: 'Request Id: <uuid>'
      - Line 3: blank
      - Line 4: header row (comma-delimited)
      - Data rows follow
      - Footer includes rollup rows like "Search total", "Overall total"
        where Campaign ID is "-" instead of a bracketed number.
    """
    if not filepath.exists():
        raise FileNotFoundError(f"CSV not found: {filepath}")

    with open(filepath, encoding="utf-8-sig") as f:
        lines = f.readlines()

    # Find the header line: the first line containing both 'Campaign' and
    # 'Campaign ID' (the grid uses 'Campaign' for the name column and
    # 'Campaign ID' for the id column).
    header_idx = None
    for i, line in enumerate(lines):
        if "Campaign ID" in line and ("Status" in line or "Budget" in line):
            header_idx = i
            break

    if header_idx is None:
        raise ValueError(
            f"Could not find header row in {filepath.name}. "
            "Expected a line containing 'Campaign ID' and 'Status' or 'Budget'."
        )

    df = pd.read_csv(
        filepath,
        skiprows=header_idx,
        sep=",",
        encoding="utf-8-sig",
        dtype=str,
        thousands=",",
    )

    # Filter out footer rollup rows. These have Campaign ID = '-' or
    # a Status value like 'Search total' / 'Overall total' / 'Audience total'.
    total_status = df["Status"].astype(str).str.contains(
        "total", case=False, regex=False, na=False
    )
    dash_id = df["Campaign ID"].astype(str).str.strip() == "-"
    df = df[~(total_status | dash_id)].copy()

    # Strip whitespace
    for col in df.select_dtypes(include=["object","string"]).columns:
        df[col] = df[col].str.strip()

    return df


# ── CSV validation ─────────────────────────────────────────────────────────────

REPORTS_CAMPAIGN_REQUIRED = {
    "Account name", "Account number", "Campaign name",
    "Base campaign ID", "Campaign status", "Campaign type",
}

REPORTS_ADGROUP_REQUIRED = {
    "Account name", "Account number", "Base campaign ID",
    "Ad group", "Ad group ID", "Ad group status",
}

GRID_REQUIRED = {
    "Status", "Campaign", "Budget", "Budget Type",
    "Campaign Type", "Bid Strategy Type", "Campaign ID",
}


def validate_columns(df: pd.DataFrame, required: set, source: str):
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"{source}: missing required columns: {sorted(missing)}\n"
            f"Found columns: {sorted(df.columns)}"
        )


# ── Campaign upsert ───────────────────────────────────────────────────────────

def load_campaigns(
    conn, account_id: int,
    campaigns_df: pd.DataFrame,
    grid_df: pd.DataFrame,
    dry_run: bool,
) -> tuple[int, int, list[str]]:
    """
    Merges the Reports campaign export (structural fields) with the grid
    export (budget + bid strategy fields) on Base campaign ID and writes
    to campaigns.

    Returns (inserted, updated, campaign_ids_written).
    """
    cursor = conn.cursor()
    inserted = 0
    updated  = 0
    written_ids: list[str] = []

    # Build a grid lookup by campaign_id (stripped)
    grid_df = grid_df.copy()
    grid_df["campaign_id_clean"] = grid_df["Campaign ID"].apply(strip_brackets)
    grid_lookup = {
        row["campaign_id_clean"]: row
        for _, row in grid_df.iterrows()
        if row["campaign_id_clean"]
    }

    not_in_grid: list[str] = []

    for _, row in campaigns_df.iterrows():
        campaign_id = strip_brackets(row.get("Base campaign ID"))
        if not campaign_id:
            continue

        reports_type = none_if_blank(row.get("Campaign type"))
        status_raw   = none_if_blank(row.get("Campaign status"))
        campaign_name = none_if_blank(row.get("Campaign name"))

        if not campaign_name or not status_raw:
            log.warning(f"  Skipping campaign {campaign_id}: missing name or status")
            continue

        status = STATUS_MAP.get(status_raw, status_raw.upper())

        # Campaign type normalization. Reports export gives us "Search & content"
        # which is the native label for Hybrid. Grid-only value "Search" is a
        # fallback if Reports didn't have this row for some reason.
        ctype_normalized, subtype = CAMPAIGN_TYPE_MAP.get(
            reports_type or "", ("SEARCH", None)
        )

        # Grid fields (budget, bid strategy) — may be absent for a given campaign
        grid_row = grid_lookup.get(campaign_id)
        if grid_row is None:
            not_in_grid.append(campaign_id)
            daily_budget_micros = None
            budget_type         = None
            is_shared           = 0
            bid_strategy_type   = None
            bid_strategy_name   = None
            target_cpa_micros   = None
            target_roas         = None
        else:
            daily_budget_micros = dollars_to_micros(grid_row.get("Budget"))
            budget_type         = none_if_blank(grid_row.get("Budget Type"))
            is_shared = 1 if (budget_type or "").lower().startswith("shared") else 0
            bid_strategy_type = none_if_blank(grid_row.get("Bid Strategy Type"))
            bid_strategy_name = none_if_blank(grid_row.get("Bid Strategy Name"))
            target_cpa_micros = dollars_to_micros(grid_row.get("Bid Strategy TargetCpa"))
            target_roas       = parse_numeric_or_none(grid_row.get("Bid Strategy TargetRoas"))

            # If reports didn't give us a specific subtype but grid didn't either,
            # we keep subtype as NULL — honoring "NULL (not empty string) is
            # written for Microsoft campaigns without a meaningful subtype".

        first_seen = get_first_seen(cursor, "campaigns", "campaign_id", campaign_id)
        exists = cursor.execute(
            "SELECT 1 FROM campaigns WHERE campaign_id = ?", (campaign_id,)
        ).fetchone() is not None

        params = (
            campaign_id,
            account_id,
            campaign_name,
            status,
            ctype_normalized,
            subtype,
            # budget_id — Microsoft's grid export doesn't expose a stable budget ID
            None,
            # budget_name
            none_if_blank(grid_row.get("Budget name") if grid_row is not None else None),
            # budget_type (normalized — Microsoft uses DailyBudgetStandard etc.)
            budget_type,
            daily_budget_micros,
            is_shared,
            bid_strategy_type,
            # bidding_strategy_id — Microsoft grid exposes name not ID; NULL
            None,
            bid_strategy_name,
            target_cpa_micros,
            target_roas,
            # parsed_* set by 04_parse_campaign_names.py later
            None, None, None, None, 0,
            # labels JSON — grid has a Labels column but it's a free-text blob;
            # leaving NULL for now, consistent with the 'simple first' principle
            None,
            first_seen,
            now_utc(),
        )

        if not dry_run:
            cursor.execute("""
                INSERT OR REPLACE INTO campaigns (
                    campaign_id, account_id, campaign_name, status,
                    campaign_type, advertising_channel_sub_type,
                    budget_id, budget_name, budget_type,
                    daily_budget_micros, is_shared_budget,
                    bidding_strategy_type, bidding_strategy_id,
                    bidding_strategy_name,
                    target_cpa_micros, target_roas,
                    parsed_product_type, parsed_geo_state,
                    parsed_match_tier, parsed_intent_tier,
                    naming_convention_flag,
                    labels,
                    first_seen_date, last_synced_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, params)

        if exists:
            updated += 1
        else:
            inserted += 1
        written_ids.append(campaign_id)

    if not dry_run:
        conn.commit()

    if not_in_grid:
        log.warning(
            f"  {len(not_in_grid)} campaign(s) present in Reports export but not in grid — "
            f"budget and bid strategy will be NULL for these:"
        )
        for cid in not_in_grid[:5]:
            log.warning(f"    {cid}")
        if len(not_in_grid) > 5:
            log.warning(f"    ... and {len(not_in_grid) - 5} more")

    return inserted, updated, written_ids


# ── Ad group upsert ───────────────────────────────────────────────────────────

def load_ad_groups(
    conn, account_id: int,
    adgroups_df: pd.DataFrame,
    known_campaign_ids: set[str],
    dry_run: bool,
) -> tuple[int, int, int]:
    """
    Writes ad_groups. Rows whose campaign_id is not in known_campaign_ids
    (the set of campaigns just written in this run) are skipped with a
    warning — they would fail the FK constraint.

    Microsoft's Reports ad group export does not include CPC bid info, so
    cpc_bid_micros is written as NULL. The keyword CSV carries per-keyword
    CPC; the ad-group-level default bid lives in the Ad Groups grid view,
    which would be a future 4th CSV if ever needed.

    Returns (inserted, updated, skipped_missing_campaign).
    """
    cursor = conn.cursor()
    inserted = 0
    updated  = 0
    skipped  = 0

    for _, row in adgroups_df.iterrows():
        ad_group_id = strip_brackets(row.get("Ad group ID"))
        campaign_id = strip_brackets(row.get("Base campaign ID"))
        ad_group_name = none_if_blank(row.get("Ad group"))
        status_raw  = none_if_blank(row.get("Ad group status"))
        # Microsoft's header is "AdGroup type" (no space). Tolerate both.
        ag_type_raw = none_if_blank(
            row.get("AdGroup type") if "AdGroup type" in adgroups_df.columns
            else row.get("Ad group type")
        )

        if not ad_group_id or not campaign_id or not ad_group_name or not status_raw:
            log.warning(
                f"  Skipping ad group row with missing fields: "
                f"id={ad_group_id} campaign={campaign_id} name={ad_group_name}"
            )
            continue

        if campaign_id not in known_campaign_ids:
            skipped += 1
            continue

        status = STATUS_MAP.get(status_raw, status_raw.upper())
        ag_type = AD_GROUP_TYPE_MAP.get(ag_type_raw or "", None)

        first_seen = get_first_seen(cursor, "ad_groups", "ad_group_id", ad_group_id)
        exists = cursor.execute(
            "SELECT 1 FROM ad_groups WHERE ad_group_id = ?", (ad_group_id,)
        ).fetchone() is not None

        params = (
            ad_group_id,
            campaign_id,
            account_id,
            ad_group_name,
            status,
            ag_type,
            None,       # cpc_bid_micros — not in Reports export
            None,       # target_cpa_micros — ad group level not exposed in this export
            None,       # target_roas — same
            0,          # bid_override_flag — no overrides can be detected from this CSV
            None, None, None, 0,   # parsed_* filled by 04_parse_campaign_names.py
            first_seen,
            now_utc(),
        )

        if not dry_run:
            cursor.execute("""
                INSERT OR REPLACE INTO ad_groups (
                    ad_group_id, campaign_id, account_id, ad_group_name,
                    status, ad_group_type,
                    cpc_bid_micros, target_cpa_micros, target_roas,
                    bid_override_flag,
                    parsed_theme, parsed_match_tier, parsed_intent_tier,
                    naming_convention_flag,
                    first_seen_date, last_synced_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, params)

        if exists:
            updated += 1
        else:
            inserted += 1

    if not dry_run:
        conn.commit()

    return inserted, updated, skipped


# ── Diagnostic ────────────────────────────────────────────────────────────────

def print_diagnostic(conn, account_id: int):
    cursor = conn.cursor()
    log.info("")
    log.info("── Diagnostic: Microsoft campaigns and ad_groups ────────────")

    for table in ("campaigns", "ad_groups"):
        rows = cursor.execute(f"""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'ENABLED' THEN 1 ELSE 0 END) AS enabled,
                SUM(CASE WHEN status = 'PAUSED'  THEN 1 ELSE 0 END) AS paused,
                SUM(CASE WHEN status = 'REMOVED' THEN 1 ELSE 0 END) AS removed
            FROM {table}
            WHERE account_id = ?
        """, (account_id,)).fetchone()

        log.info(f"  {table:<12} total={rows['total']:>6,}  "
                 f"enabled={rows['enabled'] or 0:>5,}  "
                 f"paused={rows['paused'] or 0:>5,}  "
                 f"removed={rows['removed'] or 0:>5,}")

    # Budget / bid strategy fill rate
    fill = cursor.execute("""
        SELECT
            SUM(CASE WHEN daily_budget_micros   IS NOT NULL THEN 1 ELSE 0 END) AS has_budget,
            SUM(CASE WHEN bidding_strategy_type IS NOT NULL THEN 1 ELSE 0 END) AS has_strategy,
            SUM(CASE WHEN target_cpa_micros     IS NOT NULL THEN 1 ELSE 0 END) AS has_tcpa,
            SUM(CASE WHEN target_roas           IS NOT NULL THEN 1 ELSE 0 END) AS has_troas,
            SUM(CASE WHEN advertising_channel_sub_type IS NOT NULL THEN 1 ELSE 0 END) AS has_subtype,
            COUNT(*) AS total
        FROM campaigns WHERE account_id = ?
    """, (account_id,)).fetchone()

    if fill and fill["total"]:
        t = fill["total"]
        log.info(f"  Budget populated      : {fill['has_budget']:,} / {t:,}")
        log.info(f"  Bid strategy type     : {fill['has_strategy']:,} / {t:,}")
        log.info(f"  Target CPA populated  : {fill['has_tcpa']:,} / {t:,}")
        log.info(f"  Target ROAS populated : {fill['has_troas']:,} / {t:,}")
        log.info(f"  Subtype (Hybrid tag)  : {fill['has_subtype']:,} / {t:,}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Load Microsoft campaign and ad group structure from three CSVs."
    )
    parser.add_argument("--campaigns-csv", required=True,
                        help="Reports > Campaign export (daily=None)")
    parser.add_argument("--campaigns-grid-csv", required=True,
                        help="Campaigns grid export from main UI")
    parser.add_argument("--adgroups-csv", required=True,
                        help="Reports > Ad Group export (daily=None)")
    parser.add_argument("--account", default="microsoft_home",
                        help="Placeholder for future multi-account support. "
                             "Only 'microsoft_home' is currently valid — the "
                             "Microsoft account is resolved via MS_ACCOUNT_NUMBER "
                             "at the top of this script.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and validate without writing to the database")
    args = parser.parse_args()

    if args.account != "microsoft_home":
        log.error(f"Unsupported --account value: {args.account}. Use 'microsoft_home'.")
        return

    log.info(f"Database        : {DB_PATH}")
    log.info(f"Campaigns CSV   : {args.campaigns_csv}")
    log.info(f"Grid CSV        : {args.campaigns_grid_csv}")
    log.info(f"Ad groups CSV   : {args.adgroups_csv}")
    log.info(f"Mode            : {'DRY RUN' if args.dry_run else 'LIVE'}")
    log.info("")

    # ── Read and validate CSVs ─────────────────────────────────────────
    log.info("Reading Reports campaign CSV...")
    campaigns_df = read_reports_export(Path(args.campaigns_csv))
    validate_columns(campaigns_df, REPORTS_CAMPAIGN_REQUIRED, "campaigns CSV")
    log.info(f"  {len(campaigns_df):,} rows")

    log.info("Reading Campaigns grid CSV...")
    grid_df = read_grid_export(Path(args.campaigns_grid_csv))
    validate_columns(grid_df, GRID_REQUIRED, "grid CSV")
    log.info(f"  {len(grid_df):,} rows (after stripping totals)")

    log.info("Reading ad group CSV...")
    adgroups_df = read_reports_export(Path(args.adgroups_csv))
    validate_columns(adgroups_df, REPORTS_ADGROUP_REQUIRED, "ad groups CSV")
    log.info(f"  {len(adgroups_df):,} rows")

    # Sanity: confirm account numbers match across CSVs
    for df, name in (
        (campaigns_df, "campaigns CSV"),
        (adgroups_df,  "ad groups CSV"),
    ):
        acct_nums = set(df["Account number"].dropna().unique())
        if acct_nums and acct_nums != {MS_ACCOUNT_NUMBER}:
            log.warning(
                f"  {name}: Account number mismatch. Expected "
                f"{{{MS_ACCOUNT_NUMBER}}}, found {acct_nums}"
            )

    # ── Resolve account_id and load ─────────────────────────────────────
    conn = get_db_connection()
    cursor = conn.cursor()

    account_id = get_account_internal_id(cursor)
    if account_id is None:
        log.error(
            f"  accounts row not found for platform_account_id={MS_ACCOUNT_NUMBER}. "
            f"Run kw_db_scripts/04_load_microsoft_csv.py once first to seed it."
        )
        conn.close()
        return
    log.info(f"  accounts.id = {account_id}  (integer FK for Microsoft Home Insurance)")

    log.info("")
    log.info("── Loading campaigns ────────────────────────────────────────")
    c_ins, c_upd, written_ids = load_campaigns(
        conn, account_id, campaigns_df, grid_df, args.dry_run
    )
    log.info(f"  inserted={c_ins:,}  updated={c_upd:,}  total_written={len(written_ids):,}")

    log.info("")
    log.info("── Loading ad groups ────────────────────────────────────────")
    ag_ins, ag_upd, ag_skip = load_ad_groups(
        conn, account_id, adgroups_df, set(written_ids), args.dry_run
    )
    log.info(f"  inserted={ag_ins:,}  updated={ag_upd:,}  "
             f"skipped_missing_campaign={ag_skip:,}")

    print_diagnostic(conn, account_id)

    conn.close()
    log.info("")
    log.info("Done.")
    if args.dry_run:
        log.info("DRY RUN — no database writes performed.")
    log.info("Next step:")
    log.info("  python campaign_db_scripts/09_load_microsoft_campaign_performance.py \\")
    log.info("      --file <path-to-campaign-daily-performance-CSV>")


if __name__ == "__main__":
    main()
