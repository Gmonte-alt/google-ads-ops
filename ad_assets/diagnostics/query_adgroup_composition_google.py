#!/usr/bin/env python3
"""
Google Home Insurance - Ad Group Composition Diagnostic

Read-only diagnostic that informs ad_assets\\exports work. Identifies which
active ad groups are single-brand vs. multi-brand, which determines whether
they can use [Carrier] placeholders or must rely on DKI / generic copy in
RSA assembly.

INTENDED PATH
-------------
    C:\\Projects\\google-ads-ops\\ad_assets\\diagnostics\\query_adgroup_composition_google.py

This is a read-only diagnostic that informs the design of the bulk import
generator (bulk_import_ads_google.py at ad_assets\\exports\\). Lives in
diagnostics\\ because the script has a different lifecycle than the generator:
re-run when ad group structure changes, not every time a bulk import is built.

OUTPUTS (date-stamped, in --output-dir)
---------------------------------------
1. keywords_active_<account_slug>_<YYYY-MM-DD>.csv
       One row per active keyword + identity tags
2. adgroup_composition_<account_slug>_<YYYY-MM-DD>.csv
       One row per ad group, classified as single-brand / multi-brand / untagged

The account_slug is derived from accounts.account_name (lowercased, non-alphanum
characters replaced with underscores). Date-stamping makes drift visible across
snapshots.

FILTERING (active status hierarchy only)
----------------------------------------
- accounts.id = 1 (Google Home Insurance) -- integer FK, NOT platform_account_id string
- campaigns.status        = 'ENABLED'
- ad_groups.status        = 'ENABLED'
- keyword_instances.status = 'ENABLED'
- keyword_instances.platform = 'google' (defensive; table is cross-platform)

The database architect recommended adding `ki.last_seen_date >= date('now','-7 days')`
to handle sync-latency edge cases. Per explicit direction, this filter is NOT applied.
If keyword sync has not run recently, status='ENABLED' rows may reflect stale state.

CONVENTIONS (per ad_assets directory standards)
-----------------------------------------------
- Read-only against paid_search.db (no INSERT / UPDATE / DELETE)
- Same shared database as campaign_db_scripts and kw_db_scripts
- Defensive stdout reconfigure for Unicode under Windows piping
- Defensive mkdir on --output-dir for fresh checkouts
- No imports from sibling script directories
- Output defaults under ad_assets\\diagnostics\\output\\

USAGE
-----
    python query_adgroup_composition_google.py
    python query_adgroup_composition_google.py --account 1
    python query_adgroup_composition_google.py --db-path "D:\\custom\\paid_search.db"
    python query_adgroup_composition_google.py --output-dir ".\\diagnostics"
    python query_adgroup_composition_google.py --min-keywords-per-adgroup 3
"""

import argparse
import re
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pandas as pd

# Defensive UTF-8 reconfigure for Windows piping (script may print Unicode)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


DEFAULT_DB_PATH = r"C:\Projects\google-ads-ops\db\paid_search.db"
DEFAULT_OUTPUT_DIR = r"C:\Projects\google-ads-ops\ad_assets\diagnostics\output"
DEFAULT_ACCOUNT_ID = 1  # Google Home Insurance


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

KEYWORD_QUERY = """
SELECT
    c.campaign_id,
    c.campaign_name,
    c.parsed_geo_state,
    c.parsed_intent_tier,
    ag.ad_group_id,
    ag.ad_group_name,
    ag.parsed_theme,
    ki.criterion_id,
    ki.keyword_text,
    ki.match_type,
    ki_id.normalized_text,
    ki_id.brand_category,
    ki_id.intent_type,
    ki_id.intent_action,
    ki_id.modifier_brand
FROM campaigns c
JOIN ad_groups ag         ON ag.campaign_id  = c.campaign_id
JOIN keyword_instances ki ON ki.ad_group_id  = ag.ad_group_id
LEFT JOIN keyword_identities ki_id ON ki_id.id = ki.identity_id
WHERE c.account_id = ?
  AND c.status     = 'ENABLED'
  AND ag.status    = 'ENABLED'
  AND ki.status    = 'ENABLED'
  AND ki.platform  = 'google'
ORDER BY c.campaign_name, ag.ad_group_name, ki.keyword_text
"""

COMPOSITION_QUERY = """
SELECT
    c.campaign_name,
    ag.ad_group_id,
    ag.ad_group_name,
    ag.parsed_theme,
    COUNT(DISTINCT ki.keyword_text)             AS keyword_count,
    COUNT(DISTINCT ki_id.modifier_brand)        AS distinct_brands,
    GROUP_CONCAT(DISTINCT ki_id.modifier_brand) AS brands_list,
    SUM(CASE WHEN ki_id.modifier_brand IS NULL THEN 1 ELSE 0 END) AS untagged_keyword_count,
    CASE
        WHEN COUNT(DISTINCT ki_id.modifier_brand) = 0 THEN 'untagged'
        WHEN COUNT(DISTINCT ki_id.modifier_brand) = 1 THEN 'single-brand'
        ELSE 'multi-brand'
    END AS composition
FROM ad_groups ag
JOIN campaigns c          ON c.campaign_id   = ag.campaign_id
JOIN keyword_instances ki ON ki.ad_group_id  = ag.ad_group_id
LEFT JOIN keyword_identities ki_id ON ki_id.id = ki.identity_id
WHERE c.account_id = ?
  AND c.status     = 'ENABLED'
  AND ag.status    = 'ENABLED'
  AND ki.status    = 'ENABLED'
  AND ki.platform  = 'google'
GROUP BY ag.ad_group_id, ag.ad_group_name, ag.parsed_theme, c.campaign_name
ORDER BY composition, distinct_brands DESC, c.campaign_name, ag.ad_group_name
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_account_slug(conn: sqlite3.Connection, account_id: int) -> str:
    """
    Look up accounts.account_name for the given id and convert to a
    filename-safe slug (lowercase, non-alphanum -> underscore, collapsed).
    Falls back to 'account_<id>' if the row is missing or name is empty.
    """
    row = conn.execute(
        "SELECT account_name FROM accounts WHERE id = ?", [account_id]
    ).fetchone()
    if not row or not row[0]:
        return f"account_{account_id}"
    name = row[0].lower()
    slug = re.sub(r"[^a-z0-9]+", "_", name).strip("_")
    return slug or f"account_{account_id}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--db-path",
        default=DEFAULT_DB_PATH,
        help=f"Path to paid_search.db (default: {DEFAULT_DB_PATH})",
    )
    p.add_argument(
        "--account",
        type=int,
        default=DEFAULT_ACCOUNT_ID,
        help=(
            f"Account ID to query as integer FK accounts.id "
            f"(default: {DEFAULT_ACCOUNT_ID} = Google Home Insurance)"
        ),
    )
    p.add_argument(
        "--min-keywords-per-adgroup",
        type=int,
        default=0,
        help="Exclude ad groups with fewer than N keywords (default: 0 = no filter)",
    )
    p.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory to write output CSVs (default: {DEFAULT_OUTPUT_DIR})",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    db_path = Path(args.db_path)
    if not db_path.exists():
        print(f"ERROR: Database not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))

    # Resolve account slug for filenames
    account_slug = get_account_slug(conn, args.account)
    snapshot_date = date.today().isoformat()  # YYYY-MM-DD

    print(f"Database:      {db_path}")
    print(f"Account:       {args.account}  (slug: {account_slug})")
    print(f"Snapshot date: {snapshot_date}")
    print(f"Output dir:    {output_dir.resolve()}")
    print()

    # Query 1: per-keyword detail
    print("Querying active keywords ...")
    df_keywords = pd.read_sql_query(KEYWORD_QUERY, conn, params=[args.account])
    print(f"  -> {len(df_keywords):,} keyword rows returned")

    # Query 2: per-ad-group composition
    print("Querying ad group composition ...")
    df_composition = pd.read_sql_query(COMPOSITION_QUERY, conn, params=[args.account])
    print(f"  -> {len(df_composition):,} ad groups returned")

    if args.min_keywords_per_adgroup > 0:
        before = len(df_composition)
        df_composition = df_composition[
            df_composition["keyword_count"] >= args.min_keywords_per_adgroup
        ].copy()
        after = len(df_composition)
        print(
            f"  -> Filtered to {after:,} ad groups "
            f"(>= {args.min_keywords_per_adgroup} keywords); dropped {before - after:,}"
        )

    # Write outputs (date-stamped, account-slugged)
    keyword_path = output_dir / f"keywords_active_{account_slug}_{snapshot_date}.csv"
    composition_path = output_dir / f"adgroup_composition_{account_slug}_{snapshot_date}.csv"
    df_keywords.to_csv(keyword_path, index=False)
    df_composition.to_csv(composition_path, index=False)

    # Summary stats
    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Account ID:              {args.account}")
    print(f"Active campaigns:        {df_keywords['campaign_name'].nunique():,}")
    print(f"Active ad groups:        {df_keywords['ad_group_name'].nunique():,}")
    print(f"Active keywords:         {len(df_keywords):,}")
    print()
    print("Ad group composition breakdown:")
    comp_counts = df_composition["composition"].value_counts()
    for label, count in comp_counts.items():
        pct = 100.0 * count / len(df_composition) if len(df_composition) else 0
        print(f"  {label:15s}  {count:5,d}  ({pct:5.1f}%)")
    print()
    print("Multi-brand ad groups (top 10 by distinct brand count):")
    multi = df_composition[df_composition["composition"] == "multi-brand"]
    if len(multi):
        for _, row in multi.head(10).iterrows():
            print(f"  [{row['distinct_brands']} brands] {row['ad_group_name']}")
            print(f"           brands: {row['brands_list']}")
    else:
        print("  (none)")
    print()
    print("Output files:")
    print(f"  - {keyword_path.resolve()}")
    print(f"  - {composition_path.resolve()}")
    print()
    print("NOTE: Filter is active-status-hierarchy only; last_seen_date not used.")
    print("      If keyword sync has not run recently, status='ENABLED' rows may be stale.")

    conn.close()


if __name__ == "__main__":
    main()
