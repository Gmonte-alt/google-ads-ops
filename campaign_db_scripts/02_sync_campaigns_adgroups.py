"""
02_sync_campaigns_adgroups.py  [v2 — integer account_id, MCC scope fix]
==============================
Campaign Structure Analytics — Quote.com Google Ads
Folder: C:\\Projects\\google-ads-ops\\campaign_db_scripts\\

Syncs campaign and ad group structure from the Google Ads API for ALL active
accounts defined in the .env file.

Changes from v1:
  - account_id written as INTEGER (accounts.id) in campaigns and ad_groups,
    matching the corrected schema from 01_create_campaign_tables.py v2.
    The integer is resolved once per account via get_account_internal_id()
    before any rows are written — same helper already used for traceability.
  - AD_GROUP_QUERY now includes `campaign.id` in the SELECT clause and adds
    WHERE campaign.id IN (...) filtering via a per-account campaign ID list.
    This prevents the MCC-scoped query from pulling ad groups belonging to
    other accounts under the same MCC (the root cause of the 109K spurious
    ad_group rows that required repair_adgroups_home.py).
  - report_bid_overrides() updated to join via accounts.id instead of
    matching on the string account_id column.

Active accounts as of current .env:
  MCC-managed : HOME (8886502147), MEDICARE (5310820901),
                AUTO (3863660890), TERM_LIFE (1671155176)
  Standalone  : AUTO_QUOTE (1006729211)

Run:
  conda activate paid-search

  # All accounts (default — same as v2 behavior):
  python campaign_db_scripts/02_sync_campaigns_adgroups.py

  # Single account (aligned with --account flag on 03_sync_keyword_performance.py
  # and other per-account scripts):
  python campaign_db_scripts/02_sync_campaigns_adgroups.py --account home
  python campaign_db_scripts/02_sync_campaigns_adgroups.py --account medicare
  python campaign_db_scripts/02_sync_campaigns_adgroups.py --account auto
  python campaign_db_scripts/02_sync_campaigns_adgroups.py --account auto_quote
  python campaign_db_scripts/02_sync_campaigns_adgroups.py --account term_life
"""

import sqlite3
import os
import json
import argparse
import logging
from pathlib import Path
from datetime import date, datetime, timezone
from dotenv import load_dotenv
from google.ads.googleads.client import GoogleAdsClient


# ── Configuration ─────────────────────────────────────────────────────────────
load_dotenv()

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
MCC_ID  = os.getenv("GOOGLE_ADS_MCC_ID")
TODAY   = date.today().isoformat()

# Force our handler onto the root logger BEFORE the Google Ads client library
# loads and potentially calls basicConfig itself (subsequent basicConfig calls
# are no-ops in Python's logging module — first one wins).
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter(
    "%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S"
))
logging.root.setLevel(logging.INFO)
logging.root.addHandler(_handler)
log = logging.getLogger(__name__)

# ── Account → domain mapping ───────────────────────────────────────────────────
# Mirrors ACCOUNT_WIRING in 00_seed_business_units.py — keep in sync.
CUSTOMER_DOMAIN_MAP: dict[str, str] = {
    "8886502147": "quote.com",          # Home Insurance
    "3863660890": "autoinsurance.org",  # Auto Insurance — autoinsurance.org
    "1006729211": "auto.quote.com",     # Auto Insurance — auto.quote.com
    "5310820901": "quote.com",          # Medicare
    "1671155176": "quote.com",          # Term Life
}


# ── Database connection ────────────────────────────────────────────────────────

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


def get_account_internal_id(cursor, platform_account_id: str) -> int | None:
    """
    Returns accounts.id (integer PK) for a given platform_account_id.
    Returns None if the account row doesn't exist yet.
    """
    row = cursor.execute(
        "SELECT id FROM accounts WHERE platform_account_id = ? AND platform = 'google'",
        (platform_account_id,),
    ).fetchone()
    return row["id"] if row else None


# ── Account discovery from .env ───────────────────────────────────────────────

def discover_accounts(account_filter: str | None = None) -> list[dict]:
    """
    Loads all active GOOGLE_ADS_CUSTOMER_ID_* entries from the environment.
    Commented-out lines in .env are not loaded by dotenv — automatically excluded.

    If account_filter is provided (e.g. "home", "medicare"), returns only
    that single account. Matching is case-insensitive on the vertical label
    (the suffix after GOOGLE_ADS_CUSTOMER_ID_ in .env).
    """
    accounts = []
    prefix   = "GOOGLE_ADS_CUSTOMER_ID_"

    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        suffix      = key[len(prefix):]
        vertical    = suffix.lower()
        customer_id = value.strip()

        login_key = f"GOOGLE_ADS_LOGIN_CUSTOMER_ID_{suffix}"
        login_id  = os.getenv(login_key, "").strip()

        accounts.append({
            "customer_id":       customer_id,
            "vertical":          vertical,
            "login_customer_id": login_id if login_id else MCC_ID,
            "is_standalone":     bool(login_id),
        })

    accounts.sort(key=lambda a: a["vertical"])

    if account_filter:
        wanted = account_filter.lower()
        accounts = [a for a in accounts if a["vertical"] == wanted]

    return accounts


# ── Google Ads client ──────────────────────────────────────────────────────────

def get_google_ads_client(login_customer_id: str) -> GoogleAdsClient:
    client = GoogleAdsClient.load_from_storage()
    client.login_customer_id = login_customer_id
    return client


# ── GAQL Queries ──────────────────────────────────────────────────────────────

CAMPAIGN_QUERY = """
    SELECT
        campaign.id,
        campaign.name,
        campaign.status,
        campaign.advertising_channel_type,
        campaign.advertising_channel_sub_type,
        campaign_budget.id,
        campaign_budget.name,
        campaign_budget.period,
        campaign_budget.amount_micros,
        campaign_budget.explicitly_shared,
        campaign.bidding_strategy_type,
        campaign.bidding_strategy,
        campaign.target_cpa.target_cpa_micros,
        campaign.target_roas.target_roas,
        campaign.labels
    FROM campaign
    WHERE campaign.status != 'REMOVED'
    ORDER BY campaign.id
"""

# AD_GROUP_QUERY: campaign.id is included in SELECT so we can extract it
# directly from the row rather than parsing the campaign resource name string.
# The WHERE clause scopes the query to THIS account's campaigns only,
# preventing MCC-level queries from pulling ad groups across sibling accounts.
# The campaign ID list is injected at sync time — see sync_ad_groups().
AD_GROUP_QUERY_TEMPLATE = """
    SELECT
        ad_group.id,
        ad_group.campaign,
        campaign.id,
        ad_group.name,
        ad_group.status,
        ad_group.type,
        ad_group.cpc_bid_micros,
        ad_group.target_cpa_micros,
        ad_group.target_roas
    FROM ad_group
    WHERE ad_group.status != 'REMOVED'
      AND campaign.id IN ({campaign_ids})
    ORDER BY ad_group.id
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def resource_id(resource_name: str) -> str | None:
    """Extract trailing numeric ID from a Google Ads resource name string."""
    if not resource_name:
        return None
    parts = resource_name.split("/")
    return parts[-1] if parts and parts[-1].isdigit() else None

def get_first_seen(cursor, table: str, pk_col: str, pk_val: str) -> str:
    """Return existing first_seen_date for a row, or today for new rows."""
    row = cursor.execute(
        f"SELECT first_seen_date FROM {table} WHERE {pk_col} = ?", (pk_val,)
    ).fetchone()
    return row["first_seen_date"] if row and row["first_seen_date"] else TODAY


# ── accounts table upsert ─────────────────────────────────────────────────────

def upsert_account(cursor, account: dict):
    """
    Ensures a row exists in accounts for this customer and that
    business_unit_id is wired.

    Two-phase:
      1. INSERT OR IGNORE — creates the row if not already present.
         Does not overwrite existing rows (richer data may already exist
         from kw_db_scripts seeding).
      2. Wire business_unit_id — only sets if currently NULL.
    """
    mcc_id = None if account["is_standalone"] else account["login_customer_id"]

    cursor.execute("""
        INSERT OR IGNORE INTO accounts
            (account_name, platform_account_id, platform, mcc_id)
        VALUES (?, ?, 'google', ?)
    """, (
        account["vertical"],
        account["customer_id"],
        mcc_id,
    ))

    domain = CUSTOMER_DOMAIN_MAP.get(account["customer_id"])
    if not domain:
        log.warning(
            f"  No domain mapping for {account['customer_id']} — "
            f"business_unit_id will not be set. Add to CUSTOMER_DOMAIN_MAP."
        )
        return

    tbl_exists = cursor.execute("""
        SELECT 1 FROM sqlite_master WHERE type='table' AND name='business_units'
    """).fetchone()
    if not tbl_exists:
        log.warning("  business_units table not found — run 00_seed_business_units.py first.")
        return

    bu_row = cursor.execute("""
        SELECT id FROM business_units
        WHERE vertical = ? AND domain = ?
    """, (account["vertical"], domain)).fetchone()

    if not bu_row:
        log.warning(
            f"  No business_units row for vertical='{account['vertical']}' "
            f"domain='{domain}' — add to 00_seed_business_units.py and re-run."
        )
        return

    cursor.execute("""
        UPDATE accounts
        SET business_unit_id = ?
        WHERE platform_account_id = ?
          AND platform = 'google'
          AND business_unit_id IS NULL
    """, (bu_row["id"], account["customer_id"]))


# ── Sync: campaigns ───────────────────────────────────────────────────────────

def sync_campaigns(
    client,
    ga_service,
    conn,
    account: dict,
    account_id: int,            # integer accounts.id — resolved before this call
) -> tuple[int, list[str]]:
    """
    Syncs campaigns for one account. Returns (count_synced, [campaign_id_strings]).
    The campaign_id list is passed to sync_ad_groups() to scope the ad group query.
    """
    request = client.get_type("SearchGoogleAdsRequest")
    request.customer_id = account["customer_id"]
    request.query = CAMPAIGN_QUERY

    cursor = conn.cursor()
    synced = 0
    campaign_ids = []

    for row in ga_service.search(request=request):
        c  = row.campaign
        cb = row.campaign_budget

        campaign_id           = str(c.id)
        first_seen            = get_first_seen(cursor, "campaigns", "campaign_id", campaign_id)
        label_list            = [str(lbl) for lbl in c.labels] if c.labels else []
        portfolio_strategy_id = resource_id(c.bidding_strategy)
        tCPA  = c.target_cpa.target_cpa_micros if c.target_cpa.target_cpa_micros else None
        tROAS = c.target_roas.target_roas       if c.target_roas.target_roas       else None

        cursor.execute("""
            INSERT OR REPLACE INTO campaigns (
                campaign_id,
                account_id,
                campaign_name,
                status,
                campaign_type,
                advertising_channel_sub_type,
                budget_id,
                budget_name,
                budget_type,
                daily_budget_micros,
                is_shared_budget,
                bidding_strategy_type,
                bidding_strategy_id,
                bidding_strategy_name,
                target_cpa_micros,
                target_roas,
                parsed_product_type,
                parsed_geo_state,
                parsed_match_tier,
                parsed_intent_tier,
                naming_convention_flag,
                labels,
                first_seen_date,
                last_synced_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            campaign_id,
            account_id,                 # INTEGER accounts.id — corrected from v1
            c.name,
            c.status.name,
            c.advertising_channel_type.name,
            c.advertising_channel_sub_type.name if c.advertising_channel_sub_type else None,
            str(cb.id) if cb.id else None,
            cb.name or None,
            cb.period.name if cb.period else None,
            cb.amount_micros or None,
            1 if cb.explicitly_shared else 0,
            c.bidding_strategy_type.name if c.bidding_strategy_type else None,
            portfolio_strategy_id,
            None,                       # bidding_strategy_name: enrichment pass
            tCPA,
            tROAS,
            None, None, None, None, 0,  # parsed_* set by 04_parse_campaign_names.py
            json.dumps(label_list) if label_list else None,
            first_seen,
            now_utc(),
        ))
        synced += 1
        campaign_ids.append(campaign_id)

    conn.commit()
    return synced, campaign_ids


# ── Sync: ad groups ───────────────────────────────────────────────────────────

def sync_ad_groups(
    client,
    ga_service,
    conn,
    account: dict,
    account_id: int,            # integer accounts.id
    campaign_ids: list[str],    # IDs returned by sync_campaigns — scopes this query
) -> int:
    """
    Syncs ad groups for one account, scoped to the campaigns just synced.
    Using the campaign_ids list as a WHERE IN filter prevents the MCC-scoped
    request from returning ad groups from sibling accounts.
    """
    if not campaign_ids:
        log.warning("  No campaigns found — skipping ad group sync.")
        return 0

    # Inject campaign IDs into the WHERE clause
    id_list = ", ".join(campaign_ids)
    query   = AD_GROUP_QUERY_TEMPLATE.format(campaign_ids=id_list)

    request = client.get_type("SearchGoogleAdsRequest")
    request.customer_id = account["customer_id"]
    request.query = query

    cursor = conn.cursor()
    synced = 0

    for row in ga_service.search(request=request):
        ag = row.ad_group

        ad_group_id       = str(ag.id)
        campaign_id       = resource_id(ag.campaign)
        first_seen        = get_first_seen(cursor, "ad_groups", "ad_group_id", ad_group_id)
        ag_tCPA           = ag.target_cpa_micros if ag.target_cpa_micros else None
        ag_tROAS          = ag.target_roas        if ag.target_roas        else None
        bid_override_flag = 1 if (ag_tCPA is not None or ag_tROAS is not None) else 0

        cursor.execute("""
            INSERT OR REPLACE INTO ad_groups (
                ad_group_id,
                campaign_id,
                account_id,
                ad_group_name,
                status,
                ad_group_type,
                cpc_bid_micros,
                target_cpa_micros,
                target_roas,
                bid_override_flag,
                parsed_theme,
                parsed_match_tier,
                parsed_intent_tier,
                naming_convention_flag,
                first_seen_date,
                last_synced_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            ad_group_id,
            campaign_id,
            account_id,                 # INTEGER accounts.id — corrected from v1
            ag.name,
            ag.status.name,
            ag.type_.name if ag.type_ else None,
            ag.cpc_bid_micros or None,
            ag_tCPA,
            ag_tROAS,
            bid_override_flag,
            None, None, None, 0,        # parsed_* set by 04_parse_campaign_names.py
            first_seen,
            now_utc(),
        ))
        synced += 1

    conn.commit()
    return synced


# ── Bid override report ────────────────────────────────────────────────────────

def report_bid_overrides(conn):
    """Prints ad groups with unexpected bid strategy targets across all accounts."""
    cursor = conn.cursor()
    rows = cursor.execute("""
        SELECT
            ag.ad_group_id,
            ag.ad_group_name,
            c.campaign_name,
            a.platform_account_id,
            a.account_name,
            ag.target_cpa_micros,
            ag.target_roas
        FROM   ad_groups ag
        JOIN   campaigns c  ON ag.campaign_id  = c.campaign_id
        JOIN   accounts a   ON ag.account_id   = a.id
        WHERE  ag.bid_override_flag = 1
        ORDER  BY a.platform_account_id, c.campaign_name, ag.ad_group_name
    """).fetchall()

    if rows:
        log.warning(
            f"  {len(rows)} ad group(s) have bid strategy targets set "
            f"(bid_override_flag = 1) — structural anomaly, review required:"
        )
        for row in rows:
            tCPA_str  = f"tCPA=${row['target_cpa_micros']/1e6:.2f}" if row['target_cpa_micros'] else ""
            tROAS_str = f"tROAS={row['target_roas']:.2f}"           if row['target_roas']        else ""
            flags = ", ".join(filter(None, [tCPA_str, tROAS_str]))
            log.warning(
                f"    {row['account_name']} ({row['platform_account_id']})  "
                f"{row['campaign_name']} > {row['ad_group_name']}  ({flags})"
            )
    else:
        log.info("  No ad group-level bid overrides found across any account.")


# ── Post-sync diagnostic ───────────────────────────────────────────────────────

def print_diagnostic(conn):
    """Row counts per account for campaigns and ad_groups."""
    cursor = conn.cursor()
    log.info("")
    log.info("── Diagnostic: campaigns and ad_groups per account ───────────")

    for table in ("campaigns", "ad_groups"):
        log.info(f"  {table}:")
        rows = cursor.execute(f"""
            SELECT
                a.account_name,
                a.platform_account_id,
                COUNT(*)                                        AS total,
                SUM(CASE WHEN t.status = 'ENABLED' THEN 1 ELSE 0 END) AS enabled,
                SUM(CASE WHEN t.status = 'PAUSED'  THEN 1 ELSE 0 END) AS paused
            FROM {table} t
            JOIN accounts a ON t.account_id = a.id
            GROUP BY t.account_id
            ORDER BY a.platform_account_id
        """).fetchall()

        if not rows:
            log.info("    (no data)")
            continue

        log.info(f"  {'Account':<28} {'Total':>7} {'Enabled':>8} {'Paused':>7}")
        log.info(f"  {'─'*28} {'─'*7} {'─'*8} {'─'*7}")
        for r in rows:
            log.info(
                f"  {r['account_name'][:27]:<28} "
                f"{r['total']:>7,} "
                f"{r['enabled']:>8,} "
                f"{r['paused']:>7,}"
            )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Sync Google Ads campaigns and ad groups into paid_search.db."
    )
    parser.add_argument(
        "--account",
        default=None,
        metavar="VERTICAL",
        help="Restrict the sync to a single account by vertical label "
             "(home, auto, auto_quote, medicare, term_life). "
             "Default: all accounts in .env.",
    )
    args = parser.parse_args()

    print("=" * 65)
    print("02_sync_campaigns_adgroups.py  [v2.1 — --account flag]")
    print(f"Database : {DB_PATH}")
    print(f"Started  : {now_utc()}")
    if args.account:
        print(f"Account  : {args.account} (single-account mode)")
    print("=" * 65)

    accounts = discover_accounts(account_filter=args.account)
    print(f"\nDiscovered {len(accounts)} active account(s) from .env:")
    if not accounts:
        if args.account:
            print(f"  ERROR: No GOOGLE_ADS_CUSTOMER_ID_{args.account.upper()} "
                  f"variable found in .env.")
            print(f"  Available accounts:")
            for a in discover_accounts():
                print(f"    {a['vertical']}")
        else:
            print("  ERROR: No GOOGLE_ADS_CUSTOMER_ID_* variables found.")
            print("  Check your .env file and re-run from the project root.")
        return
    for a in accounts:
        mode = "standalone" if a["is_standalone"] else f"MCC {a['login_customer_id']}"
        print(f"  {a['vertical']:<20} {a['customer_id']}  ({mode})")

    conn   = get_db_connection()
    cursor = conn.cursor()

    total_campaigns = 0
    total_adgroups  = 0
    failures        = []

    for account in accounts:
        print(f"\n── {account['vertical'].upper()} ({account['customer_id']}) ──────────────────")

        upsert_account(cursor, account)
        conn.commit()

        account_id = get_account_internal_id(cursor, account["customer_id"])
        if account_id is None:
            print(f"  ERROR: Could not resolve accounts.id for "
                  f"{account['customer_id']} — skipping.")
            failures.append(account["customer_id"])
            continue
        print(f"  accounts.id = {account_id}  (integer FK confirmed)")

        try:
            client     = get_google_ads_client(account["login_customer_id"])
            ga_service = client.get_service("GoogleAdsService")
        except Exception as e:
            print(f"  ERROR: Could not create Google Ads client: {e}")
            failures.append(account["customer_id"])
            continue

        print("  Syncing campaigns...", end=" ", flush=True)
        try:
            n_c, campaign_ids = sync_campaigns(
                client, ga_service, conn, account, account_id
            )
            print(f"{n_c:,} upserted  |  {len(campaign_ids):,} IDs captured")
            total_campaigns += n_c
        except Exception as e:
            print(f"\n  ERROR during campaign sync: {e}")
            failures.append(account["customer_id"])
            continue

        if not campaign_ids:
            print("  WARNING: 0 campaigns returned from API — skipping ad group sync.")
            continue

        print("  Syncing ad groups (campaign-scoped)...", end=" ", flush=True)
        try:
            n_ag = sync_ad_groups(
                client, ga_service, conn, account, account_id, campaign_ids
            )
            print(f"{n_ag:,} upserted")
            total_adgroups += n_ag
        except Exception as e:
            print(f"\n  ERROR during ad group sync: {e}")
            failures.append(account["customer_id"])

    print(f"\n{'='*65}")
    print("SYNC COMPLETE")
    print(f"{'='*65}")
    print(f"  Total campaigns upserted : {total_campaigns:,}")
    print(f"  Total ad groups upserted : {total_adgroups:,}")
    if failures:
        print(f"  Failures ({len(failures)}):")
        for f in failures:
            print(f"    {f}")

    report_bid_overrides(conn)
    print_diagnostic(conn)

    conn.close()
    print(f"\nDone. {now_utc()}")
    print("Next step:")
    print("  python campaign_db_scripts/04_parse_campaign_names.py")


if __name__ == "__main__":
    main()