"""
inspect_settings_changes.py
===========================
Show the real shape of the settings-change rows we want to derive into
a campaign_change_events table: TargetCPA, IncrementalBudgetAmt, and the
status changes. Lets us design the derive against actual data rather than
assumptions about Microsoft's formatting.

For each attribute of interest, prints every row's:
  change_date, item_changed (the entity name), old_value, new_value

Critically this shows:
  - what entity each change attaches to (campaign? account? shared budget?)
  - how old/new values are formatted (dollars? micros? with symbols?)
  - whether the item_changed names are pre- or post-rename (affects how we
    resolve them to campaign_id via campaign_name_history)

Run:
    python inspect_settings_changes.py
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
MS_ACCOUNT_ID = 2

ATTRS_OF_INTEREST = [
    "TargetCPA",
    "IncrementalBudgetAmt",
    "LimitAmt",
    "BidStrategyName",
    "BiddingSchemeId",
    "ConversionRateAdjustmentPct",
    "PauseStatusId",
    "LifeCycleStatusId",
]


def main():
    if not DB_PATH.exists():
        print(f"ERROR: db not found at {DB_PATH}")
        return 1
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    for attr in ATTRS_OF_INTEREST:
        rows = conn.execute("""
            SELECT change_date, item_changed, old_value, new_value, changed_by
            FROM microsoft_change_grid_raw
            WHERE account_id = ? AND attribute_changed = ?
            ORDER BY change_date
        """, (MS_ACCOUNT_ID, attr)).fetchall()

        print("=" * 78)
        print(f"{attr}  ({len(rows)} rows)")
        print("=" * 78)
        if not rows:
            print("  (none)\n")
            continue
        for r in rows:
            item = (r["item_changed"] or "")[:48]
            print(f"  {r['change_date']}  item={item!r}")
            print(f"      {r['old_value']!r}  ->  {r['new_value']!r}")
        print()

    # Cross-reference: do the item_changed names match current campaigns,
    # the snapshot (old names), or neither?
    print("=" * 78)
    print("Name resolution check for TargetCPA + IncrementalBudgetAmt items")
    print("=" * 78)
    current = {r["campaign_name"] for r in conn.execute(
        "SELECT campaign_name FROM campaigns WHERE account_id = ?", (MS_ACCOUNT_ID,))}
    snap_tbl = "campaigns_snapshot_pre_restructure_20260603"
    snap = set()
    if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (snap_tbl,)).fetchone():
        snap = {r["campaign_name"] for r in conn.execute(
            f"SELECT campaign_name FROM {snap_tbl} WHERE account_id = ?", (MS_ACCOUNT_ID,))}

    items = conn.execute("""
        SELECT DISTINCT item_changed
        FROM microsoft_change_grid_raw
        WHERE account_id = ? AND attribute_changed IN ('TargetCPA','IncrementalBudgetAmt')
    """, (MS_ACCOUNT_ID,)).fetchall()
    for r in items:
        item = r["item_changed"]
        in_current = "current" if item in current else ""
        in_snap = "snapshot(old)" if item in snap else ""
        tags = ", ".join(t for t in (in_current, in_snap) if t) or "NEITHER — may be account-level or non-campaign"
        print(f"  {item!r:<60} [{tags}]")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
