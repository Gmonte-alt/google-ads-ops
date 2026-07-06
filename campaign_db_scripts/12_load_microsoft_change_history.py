"""
12_load_microsoft_change_history.py
===================================
Loads the Microsoft UI grid ChangeHistoryDetailReport CSV into the raw
staging table, then derives the campaign_name_history SCD2 dimension.

Scope (v1): campaign RENAMES only, Microsoft Home (account 2).

WHY THIS EXISTS
A geo-tier restructure renamed Microsoft campaigns. campaign_id is
immutable but campaign_name was overwritten, so any report that groups or
displays by name shows the current (tier) name across all historical dates,
mislabeling pre-rename performance. This builds a point-in-time name history
so reports resolve the name a campaign had on a given date:

    SELECT cnh.campaign_name
    FROM campaign_name_history cnh
    WHERE cnh.campaign_id = ?
      AND ? BETWEEN cnh.valid_from AND COALESCE(cnh.valid_to, '9999-12-31')

Performance fact tables are NEVER touched. This is a pure dimension.

DATA SOURCES (all verified against real exports)
  Grid CSV (this loader's --grid-csv input):
    - 3 preamble lines (title, request id, blank); header on line 4
    - Columns: Date, Changed by, Tool, Campaign, Ad group, Entity type,
      Entity name, Attribute changed, How changed, Old value, New value
    - Campaign renames appear as rows where Attribute changed == 'CampaignName'
    - Date format M/D/YYYY
    - NO campaign_id (grid is name-only). The 'Campaign' and 'Entity name'
      columns show the CURRENT (post-rename) name on the rename row, so the
      new name resolves directly to a current campaigns.campaign_id.
    - Microsoft only retains ~6 months; earliest data here is 2026-01-08.

  Snapshot table (campaigns_snapshot_pre_restructure_20260603):
    - Pre-restructure names mapped to current campaign_ids. Authoritative
      source for OLD names. Used as a cross-check, not the date source.

  campaigns table:
    - Current names + ids. Source for resolving new_name -> campaign_id
      and for the current (open) interval's name.

  campaign_performance_daily:
    - MIN(date) per campaign_id = the historical floor (valid_from of the
      earliest interval). Data-derived, not arbitrary.

DERIVATION (per renamed campaign)
  Two intervals:
    1. (old_name,  floor       -> rename_date - 1 day)   [the state-name era]
    2. (new_name,  rename_date -> NULL/open)             [the tier-name era]
  where:
    - new_name -> campaign_id via current campaigns table (exact match)
    - rename_date from the grid row's Date
    - floor from MIN(campaign_performance_daily.date) for that campaign_id
    - old_name from the grid row's Old value (cross-checked vs snapshot)

  Never-renamed campaigns: optionally get a single open interval
  (current_name, floor -> NULL) so every campaign resolves. Controlled by
  --include-unchanged (default ON) so reports never miss a name.

IDEMPOTENCY
  - Raw staging: INSERT OR REPLACE on natural key. Reloading same CSV = no-op.
  - SCD2 derive: DELETE all Microsoft rows from campaign_name_history, then
    rebuild from scratch. Derive is a pure function of (raw + snapshot +
    campaigns + perf-floor + overrides). Re-running yields identical output.
  - Performance fact tables never appear in any write path.

SAFETY
  - Dry-run default. --apply to write.
  - PRAGMA foreign_keys=ON, busy_timeout, WAL.
  - Backs up campaign_name_history (Microsoft rows) before a re-derive.

USAGE / WEEKLY CADENCE

    Files live in:
      campaign_db_scripts\\input\\microsoft-changehist\\
        microsoft-home-changehist-grid-YYYYMMDD.csv    (renames live here)
        microsoft-home-changehist-custom-YYYYMMDD.csv  (ids, no renames; not read yet)

    Initial setup (load the full 6-month pull, or every file present):
        python 12_load_microsoft_change_history.py --backfill
        python 12_load_microsoft_change_history.py --backfill --apply

    Weekly (load just this week's incremental export):
        python 12_load_microsoft_change_history.py \\
            --grid-csv "campaign_db_scripts\\input\\microsoft-changehist\\microsoft-home-changehist-grid-YYYYMMDD.csv" --apply

    Model A (accumulating ledger): every weekly file is upserted into raw
    staging (INSERT OR REPLACE dedupes overlap). The derive step always reads
    renames from the FULL accumulated raw staging table, so a rename captured
    months ago keeps contributing to the history even after it ages out of
    Microsoft's 6-month export window. Re-running --backfill rebuilds the same
    ledger from all archived CSVs.
"""

import argparse
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
SNAPSHOT_TABLE = "campaigns_snapshot_pre_restructure_20260603"

# Weekly change-history exports accumulate here. The grid file carries renames.
INPUT_DIR = Path(r"C:\Projects\google-ads-ops\campaign_db_scripts\input\microsoft-changehist")
# Matches microsoft-home-changehist-grid-YYYYMMDD.csv ; the date is the export date.
GRID_FILENAME_RE = re.compile(r"^microsoft-home-changehist-grid-(\d{8})\.csv$", re.IGNORECASE)

PLATFORM = "microsoft"
# Microsoft Home account is resolved at runtime from platform_account_id,
# matching the convention in refresh_helper.py — the integer accounts.id is
# NOT hardcoded, because it can change across DB rebuilds while the
# platform-side id (F149SN63) is stable. MS_ACCOUNT_ID is populated once at
# startup by resolve_ms_account_id() and used thereafter.
MS_PLATFORM_ACCOUNT_ID = "F149SN63"   # Microsoft Home Insurance (quote.com)
MS_ACCOUNT_ID = None                  # resolved at runtime; see resolve_ms_account_id()
CONVENTION_VERSION = "v1-names-2026-06"
GRID_SKIPROWS = 3            # title, request id, blank => header on line 4
PERF_FLOOR_EPOCH = "2025-01-01"   # fallback floor if a campaign has no perf data

# Grid column names (verified against real export)
COL_DATE = "Date"
COL_ENTITY_TYPE = "Entity type"
COL_ATTR = "Attribute changed"
COL_OLD = "Old value"
COL_NEW = "New value"


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.row_factory = sqlite3.Row
    return conn


def resolve_ms_account_id(conn) -> int:
    """
    Resolve Microsoft Home's integer accounts.id from its stable
    platform_account_id (F149SN63), matching the runtime-resolution
    convention used across the loaders. Hard-fails if not found, rather
    than silently scoping queries to a wrong/empty account.
    """
    row = conn.execute(
        "SELECT id FROM accounts WHERE platform = 'microsoft' AND platform_account_id = ?",
        (MS_PLATFORM_ACCOUNT_ID,),
    ).fetchone()
    if row is None:
        raise SystemExit(
            f"FATAL: no accounts row for platform='microsoft' "
            f"platform_account_id={MS_PLATFORM_ACCOUNT_ID!r}. "
            f"Cannot resolve Microsoft Home account_id.")
    return row["id"]


def require_tables(conn):
    """Hard-fail if the schema hasn't been created yet."""
    needed = ["microsoft_change_grid_raw", "campaign_name_history",
              "campaign_rename_overrides", "campaigns",
              "campaign_performance_daily", SNAPSHOT_TABLE]
    missing = [t for t in needed
               if not conn.execute(
                   "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                   (t,)).fetchone()]
    if missing:
        print(f"FATAL: required tables missing: {missing}")
        print("Run 11_create_change_history_schema.py --apply first "
              "(and confirm the snapshot table exists).")
        return False
    return True


def normalize_date(mdy: str) -> str:
    """M/D/YYYY -> YYYY-MM-DD."""
    return datetime.strptime(mdy.strip(), "%m/%d/%Y").date().isoformat()


# ── File discovery (weekly-incremental accumulation) ───────────────────────────

def find_all_grid_csvs(input_dir: Path) -> list[Path]:
    """All grid CSVs in the input folder, sorted chronologically by export date
    embedded in the filename (microsoft-home-changehist-grid-YYYYMMDD.csv)."""
    if not input_dir.exists():
        return []
    matches = []
    for p in input_dir.iterdir():
        m = GRID_FILENAME_RE.match(p.name)
        if m:
            matches.append((m.group(1), p))   # (YYYYMMDD, path)
    matches.sort(key=lambda t: t[0])           # chronological by export date
    return [p for _, p in matches]


def resolve_input_files(args) -> list[Path]:
    """Returns the ordered list of grid CSVs to load, based on CLI mode."""
    if args.backfill:
        files = find_all_grid_csvs(INPUT_DIR)
        if not files:
            raise SystemExit(
                f"FATAL: --backfill found no files matching "
                f"microsoft-home-changehist-grid-YYYYMMDD.csv in {INPUT_DIR}")
        return files
    # single-file mode
    p = Path(args.grid_csv)
    if not p.exists():
        raise SystemExit(f"FATAL: grid CSV not found at {p}")
    return [p]


# ── Load grid CSV(s) into raw staging ──────────────────────────────────────────

def load_grid_raw(conn, grid_csvs: list[Path], apply: bool) -> pd.DataFrame:
    """
    Load one or more grid CSVs into microsoft_change_grid_raw, accumulating
    via INSERT OR REPLACE (Model A). Overlapping rows across weekly files
    dedupe on the natural key. Returns the combined DataFrame (union of all
    files, deduped) for the derive step.

    Raw staging is the permanent change-event ledger: a rename that ages past
    Microsoft's 6-month reporting window stays here forever once captured.
    """
    expected = {COL_DATE, COL_ENTITY_TYPE, COL_ATTR, COL_OLD, COL_NEW}
    frames = []
    loaded_at = datetime.now().isoformat(timespec="seconds")
    total_upserted = 0

    for grid_csv in grid_csvs:
        df = pd.read_csv(grid_csv, skiprows=GRID_SKIPROWS, dtype=str,
                         encoding="utf-8-sig").fillna("")
        missing = expected - set(df.columns)
        if missing:
            raise SystemExit(f"FATAL: {grid_csv.name} missing expected columns: "
                             f"{missing}. Found: {list(df.columns)}")
        print(f"  {grid_csv.name}: {len(df):,} rows")
        frames.append(df)

        if apply:
            n = 0
            for _, r in df.iterrows():
                raw_date = r[COL_DATE].strip()
                try:
                    norm = normalize_date(raw_date) if raw_date else None
                except ValueError:
                    norm = None
                conn.execute("""
                    INSERT OR REPLACE INTO microsoft_change_grid_raw
                      (account_id, change_datetime, changed_by, item_changed,
                       attribute_changed, old_value, new_value, change_date, loaded_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    MS_ACCOUNT_ID, raw_date, r.get("Changed by", ""),
                    r.get("Entity name", ""), r[COL_ATTR],
                    r[COL_OLD], r[COL_NEW], norm, loaded_at,
                ))
                n += 1
            conn.commit()
            total_upserted += n

    if apply:
        raw_total = conn.execute(
            "SELECT COUNT(*) FROM microsoft_change_grid_raw WHERE account_id = ?",
            (MS_ACCOUNT_ID,)).fetchone()[0]
        print(f"  Raw staging: upserted {total_upserted:,} rows across "
              f"{len(grid_csvs)} file(s). Ledger now holds {raw_total:,} distinct events.")
    else:
        combined_rows = sum(len(f) for f in frames)
        print(f"  [dry-run] would upsert {combined_rows:,} rows across "
              f"{len(grid_csvs)} file(s) into microsoft_change_grid_raw")

    # Combined, deduped DataFrame for the derive step.
    combined = pd.concat(frames, ignore_index=True)
    combined = combined.drop_duplicates(
        subset=[COL_DATE, COL_ATTR, COL_OLD, COL_NEW])
    return combined


# ── Derive SCD2 name history ───────────────────────────────────────────────────

def get_renames_from_raw_staging(conn) -> list[dict]:
    """
    Read all CampaignName change events from the FULL accumulated raw staging
    table — not just the files loaded this run. This is what makes Model A work:
    a rename captured months ago stays in raw staging and keeps contributing to
    the derived history even after it ages out of Microsoft's 6-month export.

    Returns list of dicts: {old_value, new_value, change_date}.
    """
    rows = conn.execute("""
        SELECT old_value, new_value, change_date
        FROM microsoft_change_grid_raw
        WHERE account_id = ? AND attribute_changed = 'CampaignName'
        ORDER BY change_date
    """, (MS_ACCOUNT_ID,)).fetchall()
    return [{"old_value": r["old_value"], "new_value": r["new_value"],
             "change_date": r["change_date"]} for r in rows]


def get_renames_from_dataframe(grid_df: pd.DataFrame) -> list[dict]:
    """Dry-run path: read renames from the parsed DataFrame (raw staging not
    written yet). Normalizes the date to match the apply path's format."""
    renames = grid_df[grid_df[COL_ATTR] == "CampaignName"].copy()
    out = []
    for _, r in renames.iterrows():
        try:
            cd = normalize_date(r[COL_DATE])
        except ValueError:
            cd = None
        out.append({"old_value": r[COL_OLD].strip(),
                    "new_value": r[COL_NEW].strip(),
                    "change_date": cd})
    return out

def resolve_perf_floor(conn, campaign_id: str) -> str:
    row = conn.execute("""
        SELECT MIN(date) FROM campaign_performance_daily
        WHERE account_id = ? AND campaign_id = ?
    """, (MS_ACCOUNT_ID, campaign_id)).fetchone()
    return row[0] if row and row[0] else PERF_FLOOR_EPOCH


def derive_name_history(conn, grid_df: pd.DataFrame, apply: bool,
                        include_unchanged: bool):
    # 1. Source of rename events depends on mode:
    #    - apply: read from FULL accumulated raw staging (Model A — includes
    #      renames from earlier weekly files that may have aged out of the
    #      current CSV).
    #    - dry-run: read from the parsed DataFrame, since raw staging hasn't
    #      been written yet this run.
    if apply:
        renames = get_renames_from_raw_staging(conn)
        print(f"\n  Rename events in full raw staging ledger: {len(renames)}")
    else:
        renames = get_renames_from_dataframe(grid_df)
        print(f"\n  Rename events found in this run's grid file(s): {len(renames)}")
        print(f"  (apply mode would derive from the full accumulated ledger,"
              f" which may include additional aged-out renames)")

    # 2. Current campaigns: name -> id (for resolving new_name)
    current = {
        r["campaign_name"]: r["campaign_id"]
        for r in conn.execute(
            "SELECT campaign_id, campaign_name FROM campaigns WHERE account_id = ?",
            (MS_ACCOUNT_ID,)).fetchall()
    }
    # Snapshot: id -> old_name (cross-check)
    snap = {
        r["campaign_id"]: r["campaign_name"]
        for r in conn.execute(
            f"SELECT campaign_id, campaign_name FROM {SNAPSHOT_TABLE} WHERE account_id = ?",
            (MS_ACCOUNT_ID,)).fetchall()
    }

    intervals = []        # rows to insert
    renamed_ids = set()
    warnings = []

    for rec in renames:
        old_name = (rec["old_value"] or "").strip()
        new_name = (rec["new_value"] or "").strip()
        rename_date = rec["change_date"]
        if not rename_date:
            warnings.append(f"Missing/unparseable date for rename "
                            f"{old_name!r} -> {new_name!r}; skipped.")
            continue

        # Resolve new_name -> current campaign_id
        cid = current.get(new_name)
        if cid is None:
            warnings.append(f"New name not found in current campaigns: {new_name!r}. "
                            f"Rename {old_name!r} -> {new_name!r} could not resolve to an id; skipped.")
            continue

        # Cross-check against snapshot
        if cid in snap and snap[cid] != old_name:
            warnings.append(
                f"Snapshot/grid old-name mismatch for id {cid}: "
                f"snapshot={snap[cid]!r} grid={old_name!r}. Using grid value.")

        floor = resolve_perf_floor(conn, cid)
        day_before = (datetime.strptime(rename_date, "%Y-%m-%d").date()
                      - timedelta(days=1)).isoformat()

        # Guard against inverted intervals: if the campaign's earliest perf
        # date is on/after the rename date, floor -> day_before would be
        # inverted (valid_from > valid_to), which makes BETWEEN queries
        # silently return nothing. Fall back to the epoch floor; if even that
        # is on/after the rename, skip the old-name interval entirely and
        # record only the new-name interval from rename_date forward.
        if floor > day_before:
            if PERF_FLOOR_EPOCH <= day_before:
                warnings.append(
                    f"Floor {floor} is after rename boundary {day_before} for id {cid} "
                    f"({old_name!r}); falling back to epoch floor {PERF_FLOOR_EPOCH}.")
                floor = PERF_FLOOR_EPOCH
                intervals.append((PLATFORM, MS_ACCOUNT_ID, cid, old_name,
                                  floor, day_before, "grid_csv", CONVENTION_VERSION))
            else:
                warnings.append(
                    f"Both perf floor ({resolve_perf_floor(conn, cid)}) and epoch "
                    f"({PERF_FLOOR_EPOCH}) are on/after rename boundary {day_before} "
                    f"for id {cid} ({old_name!r}); SKIPPING old-name interval. "
                    f"Only the new-name interval (from {rename_date}) will be recorded. "
                    f"If pre-rename reporting for this campaign is needed, add a "
                    f"campaign_rename_overrides row with an explicit earlier floor.")
        else:
            # Normal case: old name, floor -> day before rename
            intervals.append((PLATFORM, MS_ACCOUNT_ID, cid, old_name,
                              floor, day_before, "grid_csv", CONVENTION_VERSION))

        # Interval 2: new name, rename_date -> open (always recorded)
        intervals.append((PLATFORM, MS_ACCOUNT_ID, cid, new_name,
                          rename_date, None, "grid_csv", CONVENTION_VERSION))
        renamed_ids.add(cid)

    # 3. Optionally add single open interval for never-renamed campaigns
    if include_unchanged:
        all_current = conn.execute(
            "SELECT campaign_id, campaign_name FROM campaigns WHERE account_id = ?",
            (MS_ACCOUNT_ID,)).fetchall()
        for row in all_current:
            cid = row["campaign_id"]
            if cid in renamed_ids:
                continue
            floor = resolve_perf_floor(conn, cid)
            intervals.append((PLATFORM, MS_ACCOUNT_ID, cid, row["campaign_name"],
                              floor, None, "perf_floor", CONVENTION_VERSION))

    # Report
    print(f"  Renamed campaigns resolved: {len(renamed_ids)}")
    print(f"  Total intervals to write: {len(intervals)}")
    if warnings:
        print(f"\n  WARNINGS ({len(warnings)}):")
        for w in warnings:
            print(f"    - {w}")

    # Show the rename intervals specifically
    print(f"\n  Rename intervals (the reporting fix):")
    for iv in intervals:
        if iv[6] == "grid_csv":
            vto = iv[5] if iv[5] else "(open)"
            print(f"    {iv[2]:<14} {iv[4]} -> {vto:<12} {iv[3]}")

    if not apply:
        print(f"\n  [dry-run] would DELETE existing Microsoft rows from "
              f"campaign_name_history and INSERT {len(intervals)} fresh intervals.")
        return

    # Apply: back up existing Microsoft rows, delete, reinsert
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    existing = conn.execute(
        "SELECT COUNT(*) FROM campaign_name_history WHERE platform=? AND account_id=?",
        (PLATFORM, MS_ACCOUNT_ID)).fetchone()[0]
    if existing:
        conn.execute(f"""
            CREATE TABLE campaign_name_history_backup_{ts} AS
            SELECT * FROM campaign_name_history
            WHERE platform=? AND account_id=?
        """, (PLATFORM, MS_ACCOUNT_ID))
        print(f"\n  Backed up {existing} existing Microsoft rows -> "
              f"campaign_name_history_backup_{ts}")

    conn.execute(
        "DELETE FROM campaign_name_history WHERE platform=? AND account_id=?",
        (PLATFORM, MS_ACCOUNT_ID))
    conn.executemany("""
        INSERT INTO campaign_name_history
          (platform, account_id, campaign_id, campaign_name,
           valid_from, valid_to, source, convention_version)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, intervals)
    conn.commit()
    print(f"  Wrote {len(intervals)} intervals to campaign_name_history.")


# ── Validation ─────────────────────────────────────────────────────────────────

def validate(conn):
    """Post-write sanity checks: no overlapping intervals per campaign_id."""
    print(f"\n  Validation:")
    # Overlap check: for each campaign, intervals shouldn't overlap
    rows = conn.execute("""
        SELECT campaign_id, valid_from, COALESCE(valid_to,'9999-12-31') AS vto
        FROM campaign_name_history
        WHERE platform=? AND account_id=?
        ORDER BY campaign_id, valid_from
    """, (PLATFORM, MS_ACCOUNT_ID)).fetchall()
    overlaps = 0
    by_cid = {}
    for r in rows:
        by_cid.setdefault(r["campaign_id"], []).append((r["valid_from"], r["vto"]))
    for cid, ivs in by_cid.items():
        for i in range(len(ivs) - 1):
            if ivs[i][1] >= ivs[i + 1][0]:
                print(f"    OVERLAP on {cid}: {ivs[i]} vs {ivs[i+1]}")
                overlaps += 1
    if overlaps == 0:
        print(f"    No overlapping intervals. {len(by_cid)} campaigns covered.")

    # Sample resolution test for the four renames
    print(f"\n  Resolution spot-check (historical vs current name):")
    sample = conn.execute("""
        SELECT campaign_id, campaign_name, valid_from, valid_to
        FROM campaign_name_history
        WHERE platform=? AND account_id=? AND source='grid_csv'
        ORDER BY campaign_id, valid_from
    """, (PLATFORM, MS_ACCOUNT_ID)).fetchall()
    for r in sample:
        vto = r["valid_to"] if r["valid_to"] else "(current)"
        print(f"    {r['campaign_id']:<14} [{r['valid_from']} .. {vto:<12}] {r['campaign_name']}")


def main():
    ap = argparse.ArgumentParser(description="Load + derive Microsoft campaign name history.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--grid-csv",
                     help="Path to a single grid CSV (the weekly cadence).")
    src.add_argument("--backfill", action="store_true",
                     help=f"Load ALL grid CSVs in {INPUT_DIR} chronologically "
                          f"(initial setup or full rebuild).")
    ap.add_argument("--apply", action="store_true",
                    help="Actually write (default is dry-run).")
    ap.add_argument("--include-unchanged", dest="include_unchanged",
                    action="store_true", default=True,
                    help="Also add open intervals for never-renamed campaigns (default ON).")
    ap.add_argument("--no-include-unchanged", dest="include_unchanged",
                    action="store_false",
                    help="Only write intervals for renamed campaigns.")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"ERROR: DB not found at {DB_PATH}")
        return 1

    try:
        grid_files = resolve_input_files(args)
    except SystemExit as e:
        print(e)
        return 1

    print("=" * 70)
    print(f"  Microsoft campaign name history  ({'APPLY' if args.apply else 'DRY RUN'})")
    if args.backfill:
        print(f"  Mode: BACKFILL — {len(grid_files)} grid file(s) from {INPUT_DIR.name}\\")
    else:
        print(f"  Mode: single file — {grid_files[0].name}")
    print(f"  Include never-renamed campaigns: {args.include_unchanged}")
    print("=" * 70)

    conn = get_connection()
    if not require_tables(conn):
        conn.close()
        return 1

    # Resolve Microsoft Home's integer account_id at runtime (no hardcode).
    global MS_ACCOUNT_ID
    try:
        MS_ACCOUNT_ID = resolve_ms_account_id(conn)
    except SystemExit as e:
        print(e)
        conn.close()
        return 1
    print(f"  Resolved Microsoft Home account_id: {MS_ACCOUNT_ID} "
          f"(from {MS_PLATFORM_ACCOUNT_ID})")

    print("\n--- Step 1: load grid file(s) into raw staging ---")
    grid_df = load_grid_raw(conn, grid_files, args.apply)

    print("\n--- Step 2: derive SCD2 name history ---")
    derive_name_history(conn, grid_df, args.apply, args.include_unchanged)

    if args.apply:
        validate(conn)

    conn.close()
    print("\nDone.")
    if not args.apply:
        print("Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
