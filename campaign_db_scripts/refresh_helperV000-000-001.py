"""
refresh_helper.py
=================
Shared utilities for the Microsoft data refresh workflow.

Used by:
    refresh_clear_window.py   (deletes the rolling refresh window)
    refresh_orchestrator.py   (delete + run loaders end-to-end)
    refresh_check.py          (read-only daily monitoring)

Imported, not run directly. No CLI of its own.

DESIGN NOTES

The refresh window is computed from MAX(date) currently in the
performance tables, NOT from date.today(). Reason: Microsoft CSV
exports lag actual time by 1-3 days for full settlement. Using
"today minus 7" can either miss new data (if the export is older
than expected) or erase data with no replacement (if the export is
slower than 7 days behind today).

Anchoring on MAX(date) means: "delete the most recent N days that
exist in the DB, then load whatever the new CSV provides." If the
new CSV's date range overlaps the deleted window, those rows are
restored. If the CSV extends beyond the deleted window, new rows
are added. If the CSV is older than expected, the loader's
diagnostic surfaces the gap clearly.

ASSUMPTIONS

  - Microsoft account_id = 2 in the accounts table
  - All four performance tables key on a `date` or `report_date` TEXT
    column with YYYY-MM-DD format
  - Structure tables (campaigns, ad_groups, keyword_instances) are
    NOT cleared by these helpers. They UPSERT in place via the
    existing loaders, preserving first_seen_date semantics.
"""

import sqlite3
from pathlib import Path
from datetime import date, datetime, timedelta


# ── Configuration ─────────────────────────────────────────────────────────────

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
DEFAULT_WINDOW_DAYS = 7

# Multi-account registry for Microsoft Ads.
#
# Each entry maps a CLI-friendly key (e.g. "home", "auto") to its
# Microsoft platform-side account number. Account row IDs are NOT
# hardcoded — resolve_account_id() looks them up from the accounts
# table at runtime via platform_account_id, which is the durable
# identifier across DB rebuilds.
#
# To add a new Microsoft account:
#   1. INSERT into accounts table with the new platform_account_id
#   2. Add a new entry to ACCOUNT_REGISTRY below
#   3. Pass --account <key> to any refresh or loader script
ACCOUNT_REGISTRY = {
    "home": {
        "platform_account_id": "F149SN63",
        "label": "Microsoft Home Insurance (quote.com)",
    },
    "auto": {
        "platform_account_id": "F149Y8EN",
        "label": "Microsoft Auto Insurance (autoinsurance.org)",
    },
}

DEFAULT_ACCOUNT_KEY = "home"   # back-compat: scripts default to home if --account not passed


# ── Account resolution ───────────────────────────────────────────────────────

def resolve_account_id(conn: sqlite3.Connection, account_key: str) -> int:
    """
    Resolves a registry key (e.g. "home", "auto") to the integer
    accounts.id used for FK joins. Hard-fails if the key is unknown
    or if no accounts row exists for the registered platform_account_id.

    The two-step lookup (registry -> platform_account_id -> accounts.id)
    keeps the integer ID out of code. accounts.id values can change if
    the DB is rebuilt; platform_account_id is Microsoft-side and stable.
    """
    if account_key not in ACCOUNT_REGISTRY:
        valid = ", ".join(sorted(ACCOUNT_REGISTRY))
        raise ValueError(
            f"Unknown account key: '{account_key}'. Valid keys: {valid}"
        )
    platform_acct = ACCOUNT_REGISTRY[account_key]["platform_account_id"]
    row = conn.execute(
        "SELECT id FROM accounts WHERE platform_account_id = ? AND platform = 'microsoft'",
        (platform_acct,),
    ).fetchone()
    if row is None:
        raise ValueError(
            f"No accounts row for platform_account_id={platform_acct} (key='{account_key}'). "
            f"Insert the row before running this script."
        )
    return row[0] if not hasattr(row, "keys") else row["id"]


def account_label(account_key: str) -> str:
    """Human-readable label for log output."""
    if account_key not in ACCOUNT_REGISTRY:
        return account_key
    return ACCOUNT_REGISTRY[account_key]["label"]



# ── Connection ────────────────────────────────────────────────────────────────

def get_connection() -> sqlite3.Connection:
    """Standard project connection with WAL + FK enforcement."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


# ── Table inventory for refresh ───────────────────────────────────────────────
# Maps performance table -> the date column name used by that table.
# Note: campaign tables call it `date`; keyword table calls it `report_date`.

PERFORMANCE_TABLES = {
    "campaign_performance_daily":         "date",
    "campaign_performance_network_daily": "date",
    "keyword_performance_daily":          "report_date",
}

# For structure tables, no date-window deletion is performed. The loaders
# UPSERT in place. Listed here for documentation / monitoring queries.
STRUCTURE_TABLES = {
    "campaigns":         None,
    "ad_groups":         None,
    "keyword_instances": None,
}


# ── Date window computation ───────────────────────────────────────────────────

def get_max_date(conn: sqlite3.Connection, table: str, date_col: str, account_id: int) -> str | None:
    """
    Returns the latest date present in `table` for the given account.

    keyword_performance_daily references account_id via keyword_instances,
    so it needs a JOIN. Other tables have account_id directly.
    """
    if table == "keyword_performance_daily":
        sql = f"""
            SELECT MAX(kpd.{date_col}) AS d
            FROM keyword_performance_daily kpd
            JOIN keyword_instances ki ON ki.id = kpd.instance_id
            WHERE ki.account_id = ?
        """
    else:
        sql = f"""
            SELECT MAX({date_col}) AS d
            FROM {table}
            WHERE account_id = ?
        """
    row = conn.execute(sql, (account_id,)).fetchone()
    return row["d"] if row and row["d"] else None


def get_anchor_date(conn: sqlite3.Connection, account_id: int) -> str | None:
    """
    Returns the latest date present across ALL performance tables for
    the given account. This is the anchor used to compute the refresh
    window.

    If the tables disagree (e.g. campaign data through Apr 20, keyword
    data through Apr 19), this picks the latest. The window for each
    table is computed individually so each table refreshes its own
    last-N-days regardless of global anchor.
    """
    candidates = []
    for table, date_col in PERFORMANCE_TABLES.items():
        d = get_max_date(conn, table, date_col, account_id)
        if d:
            candidates.append(d)
    return max(candidates) if candidates else None


def compute_cutoff(max_date: str, window_days: int) -> str:
    """
    Given a MAX(date) string and a window in days, returns the
    cutoff date (inclusive lower bound for deletion) as YYYY-MM-DD.

    Example: max_date = '2026-04-20', window_days = 7
             cutoff = '2026-04-14' (delete rows with date >= 2026-04-14)
    """
    d = datetime.strptime(max_date, "%Y-%m-%d").date()
    cutoff = d - timedelta(days=window_days - 1)
    return cutoff.isoformat()


# ── Per-table inspection ──────────────────────────────────────────────────────

def inspect_window(
    conn: sqlite3.Connection,
    table: str,
    date_col: str,
    cutoff: str,
    account_id: int,
) -> dict:
    """
    Returns metrics for the rows that WOULD be deleted in `table`
    for `date_col >= cutoff` for the given account. Read-only.

    Returns dict with: row_count, min_date, max_date, spend_dollars
    (or 0 if table has no cost column).
    """
    if table == "keyword_performance_daily":
        sql = f"""
            SELECT
                COUNT(*) AS n,
                MIN(kpd.{date_col}) AS min_d,
                MAX(kpd.{date_col}) AS max_d,
                COALESCE(SUM(kpd.cost_micros) / 1e6, 0) AS spend
            FROM keyword_performance_daily kpd
            JOIN keyword_instances ki ON ki.id = kpd.instance_id
            WHERE ki.account_id = ?
              AND kpd.{date_col} >= ?
        """
    else:
        sql = f"""
            SELECT
                COUNT(*) AS n,
                MIN({date_col}) AS min_d,
                MAX({date_col}) AS max_d,
                COALESCE(SUM(cost_micros) / 1e6, 0) AS spend
            FROM {table}
            WHERE account_id = ?
              AND {date_col} >= ?
        """
    row = conn.execute(sql, (account_id, cutoff)).fetchone()
    return {
        "row_count": row["n"] or 0,
        "min_date":  row["min_d"],
        "max_date":  row["max_d"],
        "spend":     row["spend"] or 0.0,
    }


def inspect_all(conn: sqlite3.Connection, window_days: int, account_id: int) -> dict:
    """
    For each performance table, compute cutoff and inspect the window
    that would be deleted. Returns a dict keyed by table name.
    """
    results = {}
    for table, date_col in PERFORMANCE_TABLES.items():
        max_d = get_max_date(conn, table, date_col, account_id)
        if max_d is None:
            results[table] = {
                "max_date": None, "cutoff": None,
                "row_count": 0, "min_date": None,
                "spend": 0.0,
            }
            continue
        cutoff = compute_cutoff(max_d, window_days)
        info = inspect_window(conn, table, date_col, cutoff, account_id)
        info["max_date_in_db"] = max_d
        info["cutoff"] = cutoff
        info["date_col"] = date_col
        results[table] = info
    return results


# ── Per-table deletion ────────────────────────────────────────────────────────

def delete_window(
    conn: sqlite3.Connection,
    table: str,
    date_col: str,
    cutoff: str,
    account_id: int,
    dry_run: bool = False,
) -> int:
    """
    Deletes rows from `table` where date_col >= cutoff for the given
    account. Returns the number of rows deleted (or that WOULD be
    deleted, in dry-run mode).

    keyword_performance_daily requires a different shape because
    it doesn't store account_id directly; rows are scoped through
    keyword_instances.
    """
    if table == "keyword_performance_daily":
        # Two-step: identify instance_ids belonging to the account,
        # then delete kpd rows in window for those instances.
        if dry_run:
            sql = f"""
                SELECT COUNT(*) FROM keyword_performance_daily
                WHERE {date_col} >= ?
                  AND instance_id IN (
                      SELECT id FROM keyword_instances WHERE account_id = ?
                  )
            """
            n = conn.execute(sql, (cutoff, account_id)).fetchone()[0]
            return n
        sql = f"""
            DELETE FROM keyword_performance_daily
            WHERE {date_col} >= ?
              AND instance_id IN (
                  SELECT id FROM keyword_instances WHERE account_id = ?
              )
        """
        cur = conn.execute(sql, (cutoff, account_id))
        return cur.rowcount

    # Regular path: account_id is on the table itself
    if dry_run:
        sql = f"""
            SELECT COUNT(*) FROM {table}
            WHERE account_id = ? AND {date_col} >= ?
        """
        return conn.execute(sql, (account_id, cutoff)).fetchone()[0]

    sql = f"""
        DELETE FROM {table}
        WHERE account_id = ? AND {date_col} >= ?
    """
    cur = conn.execute(sql, (account_id, cutoff))
    return cur.rowcount


# ── Pretty printing ───────────────────────────────────────────────────────────

def fmt_window_report(results: dict, window_days: int) -> str:
    """
    Format a human-readable report of what's in each refresh window.
    """
    lines = []
    lines.append("")
    lines.append("─" * 78)
    lines.append(f"  Refresh window inspection: trailing {window_days} days per table")
    lines.append("─" * 78)
    lines.append("")
    lines.append(
        f"  {'Table':<38} {'Anchor':<12} {'Cutoff':<12} {'Rows':>8} {'Spend':>12}"
    )
    lines.append(
        f"  {'-'*38} {'-'*12} {'-'*12} {'-'*8} {'-'*12}"
    )
    for table, info in results.items():
        anchor = info.get("max_date_in_db") or "(no data)"
        cutoff = info.get("cutoff") or "—"
        rows   = info.get("row_count", 0)
        spend  = info.get("spend", 0.0)
        lines.append(
            f"  {table:<38} {anchor:<12} {cutoff:<12} "
            f"{rows:>8,} ${spend:>11,.2f}"
        )
    lines.append("")
    return "\n".join(lines)
