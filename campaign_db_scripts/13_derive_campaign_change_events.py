"""
13_derive_campaign_change_events.py
===================================
Derives a queryable campaign_change_events table from the accumulated
microsoft_change_grid_raw ledger. Scope (v1): TargetCPA and daily-budget
changes only, Microsoft Home, resolved to immutable campaign_id.

WHY
The raw grid ledger holds every change Microsoft logged, but it's id-less
(name-keyed) and noisy (intraday budget adjustments logged as separate rows).
This derives a clean, id-keyed, one-event-per-campaign-per-day-per-type table
that analysis and visualization can join to the performance series to ask
"what changed when, and what did performance do after."

Because it's keyed on campaign_id (resolved via current campaigns +
campaign_name_history for any pre-rename names), it also fixes the
visualization script's change-marker-dropping bug: name-based matching drops
markers across a rename boundary; id-based matching does not.

SCOPE & SEMANTICS (verified against real data)
  - TargetCPA: each grid row is a clean old->new tCPA transition. One event
    per change. Values are dollars ('25', '39.01'); empty old_value means
    tCPA was first set (NULL old).
  - IncrementalBudgetAmt: despite the "Incremental" name, values are ABSOLUTE
    daily budget settings (confirmed: Tier 1's chain 532->798->958->1150 on
    2026-06-03 reflects intraday budget walk-ups, ending at the current $1,150
    daily budget). Multiple same-day rows for one campaign are intraday
    adjustments; they are COLLAPSED to a single net daily transition
    (start_value -> end_value) via value-linking (the old not appearing as any
    new = start; the new not appearing as any old = end). raw_event_count
    records how many rows collapsed.

  NOT in scope (different grain — attach to ad groups / keywords, not
  campaigns): PauseStatusId, LifeCycleStatusId. A separate ad-group/keyword
  status-change derivation is the natural follow-up.

HONEST SCOPE: 11 tCPA events + ~40 net budget events over ~6 months across
multiple campaigns. This is a CHANGE-EVENT TIMELINE for visual correlation,
NOT a statistical impact model — too few events per campaign for significance
testing. Read budget-change impact from the day AFTER the change (the change
day itself is a partial-day blend of old and new budget).

CONVENTIONS
  - Single DB; account_id resolved at runtime from F149SN63 (no hardcode).
  - Idempotent derive-from-scratch: delete Microsoft rows, rebuild from the
    full raw ledger. Performance facts never touched.
  - Dry-run default; --apply to write. PRAGMA foreign_keys=ON, WAL.

Usage:
    python 13_derive_campaign_change_events.py            # dry-run
    python 13_derive_campaign_change_events.py --apply
"""

import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
PLATFORM = "microsoft"
MS_PLATFORM_ACCOUNT_ID = "F149SN63"
CONVENTION_VERSION = "v1-tcpa-budget-2026-06"

# Grid attribute -> our change_type label
ATTR_TO_TYPE = {
    "TargetCPA":            "TARGET_CPA",
    "IncrementalBudgetAmt": "DAILY_BUDGET",
}

DDL = """
CREATE TABLE IF NOT EXISTS campaign_change_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    platform            TEXT NOT NULL,
    account_id          INTEGER NOT NULL,
    campaign_id         TEXT NOT NULL,
    change_date         TEXT NOT NULL,        -- YYYY-MM-DD
    change_type         TEXT NOT NULL,        -- 'TARGET_CPA' | 'DAILY_BUDGET'
    old_value           REAL,                 -- NULL if first-set
    new_value           REAL NOT NULL,
    raw_event_count     INTEGER NOT NULL,     -- rows collapsed into this event
    resolved_via        TEXT NOT NULL,        -- 'current' | 'name_history' | 'name_history_any'
    source              TEXT NOT NULL,        -- 'grid_csv'
    convention_version  TEXT NOT NULL,
    derived_at          TEXT NOT NULL,
    FOREIGN KEY (account_id) REFERENCES accounts(id),
    UNIQUE (platform, account_id, campaign_id, change_date, change_type)
);
CREATE INDEX IF NOT EXISTS ix_cce_lookup
    ON campaign_change_events (campaign_id, change_date, change_type);
CREATE INDEX IF NOT EXISTS ix_cce_account
    ON campaign_change_events (account_id, platform, change_type);
"""


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.row_factory = sqlite3.Row
    return conn


def resolve_ms_account_id(conn) -> int:
    row = conn.execute(
        "SELECT id FROM accounts WHERE platform='microsoft' AND platform_account_id=?",
        (MS_PLATFORM_ACCOUNT_ID,)).fetchone()
    if row is None:
        raise SystemExit(f"FATAL: no accounts row for {MS_PLATFORM_ACCOUNT_ID!r}.")
    return row["id"]


def require_tables(conn) -> bool:
    needed = ["microsoft_change_grid_raw", "campaigns",
              "campaign_name_history", "accounts"]
    missing = [t for t in needed if not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (t,)
    ).fetchone()]
    if missing:
        print(f"FATAL: required tables missing: {missing}")
        return False
    return True


def parse_value(s: str):
    """'25' -> 25.0 ; '39.01' -> 39.01 ; '' -> None."""
    s = (s or "").strip()
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def resolve_campaign_id(conn, account_id, name, change_date):
    """Resolve a campaign name (as logged in the grid) to campaign_id.
    Grid logs the current name, so 'current' resolves nearly always; the
    name_history fallbacks handle any name that is no longer current."""
    if not name:
        return None, None
    row = conn.execute(
        "SELECT campaign_id FROM campaigns WHERE account_id=? AND campaign_name=?",
        (account_id, name)).fetchone()
    if row:
        return row["campaign_id"], "current"
    # name as it was on the change date
    row = conn.execute("""
        SELECT campaign_id FROM campaign_name_history
        WHERE account_id=? AND platform=? AND campaign_name=?
          AND ? BETWEEN valid_from AND COALESCE(valid_to,'9999-12-31')
    """, (account_id, PLATFORM, name, change_date)).fetchone()
    if row:
        return row["campaign_id"], "name_history"
    # name appeared in some interval
    row = conn.execute("""
        SELECT campaign_id FROM campaign_name_history
        WHERE account_id=? AND platform=? AND campaign_name=? LIMIT 1
    """, (account_id, PLATFORM, name)).fetchone()
    if row:
        return row["campaign_id"], "name_history_any"
    return None, None


def collapse_chain(rows):
    """rows: list of (old_str, new_str) for one (campaign_id, date, type).
    Returns (start_value_str, end_value_str, count) collapsing intraday chains
    by value-linking, or None if the chain is ambiguous (e.g. same-day
    round-trip). Single-row groups pass through unchanged."""
    if len(rows) == 1:
        return rows[0][0], rows[0][1], 1
    olds = [r[0] for r in rows]
    news = [r[1] for r in rows]
    old_set, new_set = set(olds), set(news)
    starts = [o for o in olds if o not in new_set]
    ends = [n for n in news if n not in old_set]
    if len(set(starts)) == 1 and len(set(ends)) == 1:
        return starts[0], ends[0], len(rows)
    return None  # ambiguous


def derive(conn, account_id, apply: bool):
    derived_at = datetime.now().isoformat(timespec="seconds")

    # Pull all in-scope raw change rows from the full accumulated ledger.
    placeholders = ",".join("?" for _ in ATTR_TO_TYPE)
    raw = conn.execute(f"""
        SELECT change_date, attribute_changed, item_changed, old_value, new_value
        FROM microsoft_change_grid_raw
        WHERE account_id=? AND attribute_changed IN ({placeholders})
          AND change_date IS NOT NULL AND change_date != ''
        ORDER BY change_date
    """, (account_id, *ATTR_TO_TYPE.keys())).fetchall()

    # Group by (campaign_name, change_date, attribute) for same-day collapse.
    groups = defaultdict(list)
    for r in raw:
        key = (r["item_changed"], r["change_date"], r["attribute_changed"])
        groups[key].append((r["old_value"], r["new_value"]))

    events = []
    warnings = []
    unresolved = []

    for (name, date, attr), rows in groups.items():
        change_type = ATTR_TO_TYPE[attr]
        cid, via = resolve_campaign_id(conn, account_id, name, date)
        if cid is None:
            unresolved.append((name, date, attr))
            continue

        collapsed = collapse_chain(rows)
        if collapsed is None:
            warnings.append(f"Ambiguous same-day {change_type} chain for "
                            f"{name!r} on {date}: {rows}. Skipped.")
            continue
        old_s, new_s, count = collapsed
        old_v, new_v = parse_value(old_s), parse_value(new_s)
        if new_v is None:
            warnings.append(f"{change_type} for {name!r} on {date} has empty "
                            f"new_value; skipped.")
            continue

        events.append((PLATFORM, account_id, cid, date, change_type,
                       old_v, new_v, count, via, "grid_csv",
                       CONVENTION_VERSION, derived_at))

    # Report
    n_tcpa = sum(1 for e in events if e[4] == "TARGET_CPA")
    n_budget = sum(1 for e in events if e[4] == "DAILY_BUDGET")
    print(f"\n  In-scope raw rows: {len(raw)}")
    print(f"  Distinct (campaign,date,type) groups: {len(groups)}")
    print(f"  Events derived: {len(events)}  (TARGET_CPA={n_tcpa}, DAILY_BUDGET={n_budget})")
    collapsed_groups = [e for e in events if e[7] > 1]
    if collapsed_groups:
        print(f"  Same-day chains collapsed: {len(collapsed_groups)}")
        for e in collapsed_groups:
            print(f"    {e[2]} {e[3]} {e[4]}: {e[7]} rows -> net "
                  f"{e[5]} -> {e[6]}")
    if unresolved:
        print(f"\n  UNRESOLVED names ({len(unresolved)}) — not written:")
        for name, date, attr in unresolved:
            print(f"    {date} {attr} {name!r}")
    if warnings:
        print(f"\n  WARNINGS ({len(warnings)}):")
        for w in warnings:
            print(f"    - {w}")

    # Sample of derived events
    print(f"\n  Sample derived events:")
    for e in sorted(events, key=lambda x: (x[4], x[3]))[:14]:
        ov = "(first-set)" if e[5] is None else f"{e[5]:g}"
        print(f"    {e[3]}  {e[4]:<12} {e[2]:<14} {ov:>11} -> {e[6]:g}"
              f"{'  ['+str(e[7])+' rows]' if e[7] > 1 else ''}")

    if not apply:
        print(f"\n  [dry-run] would DELETE Microsoft rows from "
              f"campaign_change_events and INSERT {len(events)} events.")
        return

    # Apply: ensure table, back up, delete-rebuild
    conn.executescript(DDL)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    existing = conn.execute(
        "SELECT COUNT(*) FROM campaign_change_events WHERE platform=? AND account_id=?",
        (PLATFORM, account_id)).fetchone()[0]
    if existing:
        conn.execute(f"""CREATE TABLE campaign_change_events_backup_{ts} AS
            SELECT * FROM campaign_change_events
            WHERE platform=? AND account_id=?""", (PLATFORM, account_id))
        print(f"\n  Backed up {existing} existing rows -> "
              f"campaign_change_events_backup_{ts}")
    conn.execute(
        "DELETE FROM campaign_change_events WHERE platform=? AND account_id=?",
        (PLATFORM, account_id))
    conn.executemany("""
        INSERT INTO campaign_change_events
          (platform, account_id, campaign_id, change_date, change_type,
           old_value, new_value, raw_event_count, resolved_via, source,
           convention_version, derived_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, events)
    conn.commit()
    print(f"\n  Wrote {len(events)} events to campaign_change_events.")


def main():
    ap = argparse.ArgumentParser(description="Derive campaign_change_events (dry-run default).")
    ap.add_argument("--apply", action="store_true", help="Actually write.")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"ERROR: DB not found at {DB_PATH}")
        return 1

    print("=" * 70)
    print(f"  Derive campaign_change_events  ({'APPLY' if args.apply else 'DRY RUN'})")
    print("=" * 70)

    conn = get_connection()
    if not require_tables(conn):
        conn.close()
        return 1

    # Create table in dry-run too (harmless) so resolution queries are uniform.
    if not args.apply:
        conn.executescript(DDL)

    account_id = resolve_ms_account_id(conn)
    print(f"  Resolved Microsoft Home account_id: {account_id} (from {MS_PLATFORM_ACCOUNT_ID})")

    derive(conn, account_id, args.apply)

    conn.close()
    print("\nDone.")
    if not args.apply:
        print("Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
