"""
refresh_orchestrator.py
=======================
Microsoft data refresh — full pipeline.

Folder: C:\\Projects\\google-ads-ops\\campaign_db_scripts\\

Runs the trailing-window refresh end-to-end:
  1. Inspect what will be cleared
  2. (Optional) Refresh structure tables via 08_load_microsoft_campaigns.py
  3. Clear the trailing-N-day window from performance tables
  4. Run 09_load_microsoft_campaign_performance.py for the campaign CSV
  5. Run 10_load_microsoft_geo.py for the geo CSV
  6. (Optional) Run 04_load_microsoft_csv.py for the keyword CSV
  7. Print the post-refresh diagnostic

TWO REFRESH MODES

  --mode performance (default, daily or weekly)
      Refreshes performance tables only:
        - campaign_performance_daily
        - campaign_performance_network_daily
        - geo_performance_daily
        - keyword_performance_daily         (only if --kw-csv provided)
        - keyword_instances (UPSERTed by 04 when --kw-csv is provided)
      Skips structure refresh entirely. Microsoft campaigns and ad_groups
      change slowly enough that the existing rows are accurate.
      Required CSVs: --camp-perf-csv and --geo-csv. --kw-csv is optional.
      Daily cadence: omit --kw-csv (faster; campaign + geo only).
      Weekly cadence: include --kw-csv to also refresh keyword data.

  --mode full (less frequent, monthly or as needed)
      Same as --mode performance, plus refreshes:
        - campaigns        (UPSERTed by 08)
        - ad_groups        (UPSERTed by 08)
      Required CSVs: --camp-perf-csv, --geo-csv, plus the 3 structure CSVs.
      --kw-csv remains optional; include it for full perf+structure refresh.
      Use when campaigns or ad groups have been added/paused/renamed
      and you want the structure tables to reflect that.

WHAT IT DOES NOT TOUCH

  - Google data — every operation scopes to the resolved Microsoft account_id.
  - Historical performance data outside the trailing window — bit-for-bit
    untouched. Set --window 0 to skip the delete entirely (load-only).
  - Schema — never modifies tables, only data.

USAGE

    conda activate paid-search

    # Daily performance refresh (campaign + geo, no keyword)
    python campaign_db_scripts/refresh_orchestrator.py \\
        --mode performance \\
        --camp-perf-csv input/microsoft-campaign-daily/microsoft-home-camp-perf-YYYYMMDD.csv \\
        --geo-csv       input/microsoft-geo-daily/microsoft-home-geo-YYYYMMDD.csv

    # Weekly performance refresh (adds keyword)
    python campaign_db_scripts/refresh_orchestrator.py \\
        --mode performance \\
        --camp-perf-csv input/microsoft-campaign-daily/microsoft-home-camp-perf-YYYYMMDD.csv \\
        --geo-csv       input/microsoft-geo-daily/microsoft-home-geo-YYYYMMDD.csv \\
        --kw-csv        input/microsoft-kw/microsoft-home-kw-YYYYMMDD.csv

    # Monthly full refresh (structure + performance, with or without keyword)
    python campaign_db_scripts/refresh_orchestrator.py \\
        --mode full \\
        --campaigns-csv      input/microsoft_meta/microsoft-home-campaigns-YYYYMMDD.csv \\
        --campaigns-grid-csv input/microsoft-budget-bid/microsoft-home-campaign-budget-bid-YYYYMMDD.csv \\
        --adgroups-csv       input/microsoft_meta/microsoft-home-adgroups-YYYYMMDD.csv \\
        --camp-perf-csv      input/microsoft-campaign-daily/microsoft-home-camp-perf-YYYYMMDD.csv \\
        --geo-csv            input/microsoft-geo-daily/microsoft-home-geo-YYYYMMDD.csv \\
        --kw-csv             input/microsoft-kw/microsoft-home-kw-YYYYMMDD.csv

    # Dry run any mode — inspects the deletion window, validates CSV
    # paths exist, but does not delete or load
    python campaign_db_scripts/refresh_orchestrator.py --mode performance \\
        --camp-perf-csv ... --geo-csv ... --dry-run

    # Custom window (default 7)
    python campaign_db_scripts/refresh_orchestrator.py --mode performance \\
        --camp-perf-csv ... --geo-csv ... --window 14

    # Skip the interactive confirmation prompt (use in scripts/cron only)
    python campaign_db_scripts/refresh_orchestrator.py --mode performance \\
        --camp-perf-csv ... --geo-csv ... --yes

EXIT CODES

  0   Success
  1   User cancelled, or a step failed (DB rolled back where applicable)
  2   Argument error (missing file, invalid mode, etc.)
"""

import argparse
import logging
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from refresh_helper import (
    ACCOUNT_REGISTRY, DEFAULT_ACCOUNT_KEY,
    DEFAULT_WINDOW_DAYS, PERFORMANCE_TABLES,
    account_label, compute_cutoff, delete_window, fmt_window_report,
    get_connection, get_max_date, inspect_window, resolve_account_id,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Resolve script paths relative to this file ────────────────────────────────
# refresh_orchestrator.py lives in campaign_db_scripts/, so:
#   - 08, 09 are siblings
#   - 04 is in ../kw_db_scripts/
SCRIPTS_DIR     = Path(__file__).parent                          # campaign_db_scripts/
KW_SCRIPTS_DIR  = SCRIPTS_DIR.parent / "kw_db_scripts"           # kw_db_scripts/

LOADER_CAMPAIGNS = SCRIPTS_DIR / "08_load_microsoft_campaigns.py"
LOADER_CAMP_PERF = SCRIPTS_DIR / "09_load_microsoft_campaign_performance.py"
LOADER_GEO       = SCRIPTS_DIR / "10_load_microsoft_geo.py"
LOADER_KW        = KW_SCRIPTS_DIR / "04_load_microsoft_csv.py"


# ── CSV existence check ───────────────────────────────────────────────────────

def check_csvs_exist(paths: dict) -> list[str]:
    """Returns list of missing CSV paths, empty if all present."""
    missing = []
    for label, p in paths.items():
        if not Path(p).exists():
            missing.append(f"{label}: {p}")
    return missing


# ── Subprocess wrapper for loaders ────────────────────────────────────────────

def run_loader(cmd: list[str], label: str) -> int:
    """
    Runs a loader script as a subprocess and streams output. Returns
    the loader's exit code.
    """
    log.info("")
    log.info("─" * 65)
    log.info(f"  LOADER: {label}")
    log.info(f"  CMD: {' '.join(cmd)}")
    log.info("─" * 65)
    result = subprocess.run(cmd)
    if result.returncode != 0:
        log.error(f"  Loader '{label}' exited with code {result.returncode}")
    return result.returncode


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Microsoft data refresh — orchestrator (delete window + run loaders).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--mode", choices=("performance", "full"), default="performance",
        help="performance (weekly): perf tables only. full (monthly): structure + perf.",
    )
    parser.add_argument(
        "--account", default=DEFAULT_ACCOUNT_KEY,
        choices=list(ACCOUNT_REGISTRY.keys()),
        help=f"Microsoft account key (default: {DEFAULT_ACCOUNT_KEY}). "
             f"Valid: {', '.join(sorted(ACCOUNT_REGISTRY))}",
    )
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW_DAYS,
                        help=f"Trailing days to clear (default: {DEFAULT_WINDOW_DAYS}). "
                             f"Set 0 to skip delete and load-only.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Inspect and validate paths; no DB changes, no loaders run.")
    parser.add_argument("--yes", action="store_true",
                        help="Skip the interactive confirmation prompt.")

    # Performance-mode CSVs (camp-perf and geo are required for both modes)
    parser.add_argument("--camp-perf-csv", required=False,
                        help="Microsoft campaign daily performance CSV. Required for both modes.")
    parser.add_argument("--geo-csv", required=False,
                        help="Microsoft geo (Geographic Location Report) CSV. Required for both modes.")
    parser.add_argument("--kw-csv", required=False,
                        help="Microsoft keyword CSV. Optional in both modes — "
                             "omit for daily refresh (campaign + geo only); "
                             "include for weekly/monthly refresh that also covers keyword data.")

    # Full-mode CSVs (only required when --mode=full)
    parser.add_argument("--campaigns-csv", required=False,
                        help="Reports > Campaign export. Required for --mode=full.")
    parser.add_argument("--campaigns-grid-csv", required=False,
                        help="Campaigns grid export. Required for --mode=full.")
    parser.add_argument("--adgroups-csv", required=False,
                        help="Reports > Ad Group export. Required for --mode=full.")

    args = parser.parse_args()

    if args.window < 0:
        log.error("--window must be >= 0")
        return 2

    # Validate required CSVs per mode
    # camp-perf and geo are required in both modes; kw is optional in both
    csv_paths = {
        "camp-perf-csv": args.camp_perf_csv,
        "geo-csv":       args.geo_csv,
    }
    if args.kw_csv:
        csv_paths["kw-csv"] = args.kw_csv
    if args.mode == "full":
        csv_paths["campaigns-csv"]      = args.campaigns_csv
        csv_paths["campaigns-grid-csv"] = args.campaigns_grid_csv
        csv_paths["adgroups-csv"]       = args.adgroups_csv

    missing_args = [label for label, p in csv_paths.items() if not p]
    if missing_args:
        log.error(f"Missing required arguments for --mode={args.mode}: "
                  f"{', '.join('--' + m for m in missing_args)}")
        return 2

    missing_files = check_csvs_exist(csv_paths)
    if missing_files:
        log.error("CSV files not found:")
        for m in missing_files:
            log.error(f"  {m}")
        return 2

    # Validate loader scripts exist
    for label, p in (
        ("08", LOADER_CAMPAIGNS),
        ("09", LOADER_CAMP_PERF),
        ("10", LOADER_GEO),
        ("04", LOADER_KW),
    ):
        if not p.exists():
            log.error(f"Loader script not found: {p}")
            return 2

    log.info("Microsoft refresh orchestrator")
    log.info(f"  Account : {args.account}  ({account_label(args.account)})")
    log.info(f"  Mode    : {args.mode}")
    log.info(f"  Window  : {args.window} day(s)")
    log.info(f"  Dry run : {args.dry_run}")
    for label, p in csv_paths.items():
        log.info(f"  {label:<22}: {p}")

    # ── Step 1: Inspect deletion window ──────────────────────────────
    conn = get_connection()
    try:
        account_id = resolve_account_id(conn, args.account)
    except ValueError as e:
        log.error(str(e))
        conn.close()
        return 2
    log.info(f"  Resolved accounts.id: {account_id}")
    inspection = {}
    if args.window > 0:
        for table, date_col in PERFORMANCE_TABLES.items():
            max_d = get_max_date(conn, table, date_col, account_id)
            if max_d is None:
                inspection[table] = {
                    "max_date_in_db": None, "cutoff": None,
                    "row_count": 0, "spend": 0.0,
                    "date_col": date_col,
                }
                continue
            cutoff = compute_cutoff(max_d, args.window)
            info = inspect_window(conn, table, date_col, cutoff, account_id)
            info["max_date_in_db"] = max_d
            info["cutoff"]   = cutoff
            info["date_col"] = date_col
            inspection[table] = info

        print(fmt_window_report(inspection, args.window))
        total_rows = sum(r["row_count"] for r in inspection.values())
        total_spend = sum(r["spend"] for r in inspection.values())
        log.info(f"  Total rows in window: {total_rows:,}  (${total_spend:,.2f} spend)")
    else:
        log.info("  --window 0 specified: skipping delete step.")

    if args.dry_run:
        log.info("")
        log.info("DRY RUN — no deletions or loader runs will be performed.")
        log.info("All CSV paths exist; loader scripts found; window inspection complete.")
        conn.close()
        return 0

    # ── Step 2: Confirmation ─────────────────────────────────────────
    if not args.yes:
        log.info("")
        prompt_lines = [f"  Proceeding will:"]
        step = 1
        if args.mode == "full":
            prompt_lines.append(
                f"    {step}. UPSERT structure tables (campaigns, ad_groups) via loader 08"
            )
            step += 1
        if args.window > 0:
            prompt_lines.append(
                f"    {step}. DELETE {total_rows:,} rows from performance tables "
                f"(window: {args.window} days)"
            )
            step += 1
        prompt_lines.append(
            f"    {step}. RUN loader 09 for campaign performance"
        )
        step += 1
        prompt_lines.append(
            f"    {step}. RUN loader 10 for geo performance"
        )
        step += 1
        if args.kw_csv:
            prompt_lines.append(
                f"    {step}. RUN loader 04 for keyword performance"
            )
        else:
            prompt_lines.append(
                f"       (loader 04 SKIPPED — no --kw-csv provided; "
                f"daily-cadence refresh)"
            )
        for line in prompt_lines:
            log.info(line)
        log.info("")
        try:
            resp = input("  Continue? [y/N] ").strip().lower()
        except EOFError:
            resp = ""
        if resp != "y":
            log.info("  Cancelled.")
            conn.close()
            return 1

    # ── Step 3: Run structure loader (full mode only) ────────────────
    if args.mode == "full":
        rc = run_loader(
            [
                sys.executable, str(LOADER_CAMPAIGNS),
                "--account",            args.account,
                "--campaigns-csv",      args.campaigns_csv,
                "--campaigns-grid-csv", args.campaigns_grid_csv,
                "--adgroups-csv",       args.adgroups_csv,
            ],
            "08 — campaigns + ad_groups",
        )
        if rc != 0:
            log.error("Structure loader failed; aborting before delete/load.")
            conn.close()
            return 1

    # ── Step 4: Delete the window ────────────────────────────────────
    if args.window > 0 and total_rows > 0:
        log.info("")
        log.info("─" * 65)
        log.info("  DELETE window from performance tables")
        log.info("─" * 65)
        try:
            for table, info in inspection.items():
                if info["row_count"] == 0:
                    log.info(f"  {table:<38} 0 rows in window (skipped)")
                    continue
                n = delete_window(
                    conn, table, info["date_col"], info["cutoff"], account_id, dry_run=False,
                )
                log.info(f"  {table:<38} deleted {n:,} rows (>= {info['cutoff']})")
            conn.commit()
        except Exception as e:
            conn.rollback()
            log.error(f"Delete failed, transaction rolled back: {e}")
            conn.close()
            return 1
    elif args.window > 0:
        log.info("  No rows in delete window — proceeding to loaders.")

    # Close our handle so loaders can open their own connections cleanly
    conn.close()

    # ── Step 5: Run campaign performance loader ──────────────────────
    rc = run_loader(
        [
            sys.executable, str(LOADER_CAMP_PERF),
            "--account", args.account,
            "--file",    args.camp_perf_csv,
        ],
        "09 — campaign performance",
    )
    if rc != 0:
        log.error("Campaign performance loader failed.")
        log.error("DB state: window deleted but campaign perf NOT reloaded.")
        log.error("Re-run loader 09 manually with the same CSV to recover.")
        return 1

    # ── Step 6: Run geo loader ───────────────────────────────────────
    # Pass --window 0 because we already deleted the window across all
    # performance tables (including geo_performance_daily) in Step 4.
    rc = run_loader(
        [
            sys.executable, str(LOADER_GEO),
            "--account", args.account,
            "--file",    args.geo_csv,
            "--window",  "0",
        ],
        "10 — geo performance",
    )
    if rc != 0:
        log.error("Geo loader failed.")
        log.error("DB state: window deleted, campaign perf reloaded, "
                  "but geo NOT reloaded.")
        log.error(
            f"Re-run loader 10 manually with: "
            f"python campaign_db_scripts/10_load_microsoft_geo.py "
            f"--account {args.account} --file {args.geo_csv} --window 0"
        )
        return 1

    # ── Step 7: Run keyword loader (optional — only if --kw-csv provided) ──
    if args.kw_csv:
        rc = run_loader(
            [
                sys.executable, str(LOADER_KW),
                "--account", args.account,
                "--file",    args.kw_csv,
            ],
            "04 — keyword structure + performance",
        )
        if rc != 0:
            log.error("Keyword loader failed.")
            log.error("DB state: campaign + geo data refreshed but "
                      "keyword data NOT reloaded.")
            log.error(
                f"Re-run loader 04 manually with: "
                f"python kw_db_scripts/04_load_microsoft_csv.py "
                f"--account {args.account} --file {args.kw_csv}"
            )
            return 1
    else:
        log.info("")
        log.info("─" * 65)
        log.info("  Loader 04 (keyword) SKIPPED — no --kw-csv provided.")
        log.info("─" * 65)

    # ── Step 8: Post-refresh summary ─────────────────────────────────
    log.info("")
    log.info("=" * 65)
    log.info("  REFRESH COMPLETE")
    log.info("=" * 65)

    conn = get_connection()
    log.info("")
    log.info(f"Final state of Microsoft performance tables for account '{args.account}':")
    log.info("")
    log.info(f"  {'Table':<38} {'Min date':<12} {'Max date':<12} {'Rows':>10}")
    log.info(f"  {'-'*38} {'-'*12} {'-'*12} {'-'*10}")

    for table, date_col in PERFORMANCE_TABLES.items():
        if table == "keyword_performance_daily":
            sql = f"""
                SELECT MIN(kpd.{date_col}) AS min_d, MAX(kpd.{date_col}) AS max_d,
                       COUNT(*) AS n
                FROM keyword_performance_daily kpd
                JOIN keyword_instances ki ON ki.id = kpd.instance_id
                WHERE ki.account_id = ?
            """
        else:
            sql = f"""
                SELECT MIN({date_col}) AS min_d, MAX({date_col}) AS max_d,
                       COUNT(*) AS n
                FROM {table} WHERE account_id = ?
            """
        r = conn.execute(sql, (account_id,)).fetchone()
        log.info(f"  {table:<38} {r['min_d'] or '—':<12} {r['max_d'] or '—':<12} "
                 f"{(r['n'] or 0):>10,}")

    conn.close()
    log.info("")
    log.info("Recommended next step: run refresh_check.py to verify reconciliation.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
