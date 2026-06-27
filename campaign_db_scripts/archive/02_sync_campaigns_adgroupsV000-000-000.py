"""
02_sync_campaigns_adgroups.py
==============================
Campaign Structure Analytics — Quote.com Google Ads
Folder: C:\\Projects\\google-ads-ops\\campaign_db_scripts\\

Syncs campaign and ad group structure from the Google Ads API for ALL active
accounts defined in the .env file.

Alignment with existing keyword tracking schema (kw_db_scripts/):
  - load_dotenv() with no path arg — matches 02_sync_keyword_structure.py pattern
  - GoogleAdsClient.load_from_storage() with no arguments — matches kw scripts
  - get_db_connection() enables PRAGMA foreign_keys=ON and journal_mode=WAL
  - get_account_id() looks up accounts.id by platform_account_id — same helper
    pattern as kw scripts; used for the accounts table upsert check only
  - campaign/ad_group tables store platform_account_id string in account_id
    column (no FK declaration — consistent with Option A decision and with the
    join pattern in 10_tagging_diagnostic.py)
  - accounts INSERT uses correct column names:
    account_name, platform_account_id, platform, mcc_id

Account discovery:
  - All GOOGLE_ADS_CUSTOMER_ID_* vars from .env (commented lines auto-excluded)
  - Accounts with GOOGLE_ADS_LOGIN_CUSTOMER_ID_* are standalone (no MCC)

Active accounts as of current .env:
  MCC-managed : HOME (8886502147), MEDICARE (5310820901),
                AUTO (3863660890), TERM_LIFE (1671155176)
  Standalone  : AUTO_QUOTE (1006729211)

Run:
  python 02_sync_campaigns_adgroups.py
"""

import sqlite3
import os
import json
import logging
from pathlib import Path
from datetime import date, datetime, timezone
from dotenv import load_dotenv
from google.ads.googleads.client import GoogleAdsClient


# ── Configuration ─────────────────────────────────────────────────────────────
# load_dotenv() with no path — matches the pattern in kw_db_scripts
load_dotenv()

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
MCC_ID  = os.getenv("GOOGLE_ADS_MCC_ID")
TODAY   = date.today().isoformat()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Account → domain mapping ───────────────────────────────────────────────────
# Maps platform_account_id → domain for business_unit_id wiring.
# This mirrors ACCOUNT_WIRING in 00_seed_business_units.py and must be kept
# in sync with it. vertical is already known from the .env var suffix;
# domain is the only additional piece of information needed here.
# Add new accounts here when they are created.
CUSTOMER_DOMAIN_MAP: dict[str, str] = {
    "8886502147": "quote.com",          # Home Insurance
    "3863660890": "autoinsurance.org",  # Auto Insurance — autoinsurance.org
    "1006729211": "auto.quote.com",     # Auto Insurance — auto.quote.com
    "5310820901": "quote.com",          # Medicare
    "1671155176": "quote.com",          # Term Life
}


# ── Database connection ────────────────────────────────────────────────────────

def get_db_connection():
    """Matches the connection helper pattern across all kw_db_scripts."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


def get_account_internal_id(cursor, platform_account_id: str) -> int | None:
    """
    Returns accounts.id (integer PK) for a given platform_account_id.
    Mirrors get_account_id() in kw_db_scripts/02_sync_keyword_structure.py.
    Returns None (rather than raising) so the caller can decide whether to
    insert a new account row or raise an error.
    """
    row = cursor.execute(
        "SELECT id FROM accounts WHERE platform_account_id = ? AND platform = 'google'",
        (platform_account_id,),
    ).fetchone()
    return row["id"] if row else None


# ── Account discovery from .env ───────────────────────────────────────────────

def discover_accounts() -> list[dict]:
    """
    Loads all active GOOGLE_ADS_CUSTOMER_ID_* entries from the environment.
    Commented-out lines in .env are not loaded by dotenv — automatically excluded.

    Returns a list of dicts:
      {
        "customer_id"      : "8886502147",       # platform_account_id
        "vertical"         : "home",             # derived from env var suffix
        "login_customer_id": "6983050143",       # MCC ID, or own ID if standalone
        "is_standalone"    : False,
      }
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
    return accounts


# ── Google Ads client ──────────────────────────────────────────────────────────

def get_google_ads_client(login_customer_id: str) -> GoogleAdsClient:
    """
    Loads a GoogleAdsClient from google-ads.yaml (path set via env var
    GOOGLE_ADS_CONFIGURATION_FILE_PATH — same pattern as kw_db_scripts).
    Overrides login_customer_id for standalone accounts without a separate yaml.
    """
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

AD_GROUP_QUERY = """
    SELECT
        ad_group.id,
        ad_group.campaign,
        ad_group.name,
        ad_group.status,
        ad_group.type,
        ad_group.cpc_bid_micros,
        ad_group.target_cpa_micros,
        ad_group.target_roas
    FROM ad_group
    WHERE ad_group.status != 'REMOVED'
    ORDER BY ad_group.id
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def resource_id(resource_name: str) -> str | None:
    """Extract trailing numeric ID from a Google Ads resource name string.
    e.g. 'customers/123/biddingStrategies/456' -> '456'
    """
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
    Ensures a row exists in the accounts table for this customer and that
    business_unit_id is set on it.

    Two-phase approach:
      1. INSERT OR IGNORE — creates the row if it doesn't exist yet.
         Never overwrites rows already populated by the keyword tracking
         system (which may have richer account_name values).
      2. Wire business_unit_id — looks up business_units by (vertical, domain)
         and UPDATE SET business_unit_id if the column is currently NULL.
         This covers both newly inserted rows and pre-existing rows that were
         seeded by kw_db_scripts before 00_seed_business_units.py was run.

    Column names match the actual schema from kw_db_scripts/01_create_schema.py:
        account_name        TEXT NOT NULL
        platform_account_id TEXT NOT NULL
        platform            TEXT NOT NULL DEFAULT 'google'
        mcc_id              TEXT          (NULL for standalone accounts)
        business_unit_id    INTEGER       (FK to business_units.id, added by 00_seed_business_units.py)
        UNIQUE (platform_account_id, platform)
    """
    mcc_id = None if account["is_standalone"] else account["login_customer_id"]

    # Phase 1: insert if not already present
    cursor.execute("""
        INSERT OR IGNORE INTO accounts
            (account_name, platform_account_id, platform, mcc_id)
        VALUES (?, ?, 'google', ?)
    """, (
        account["vertical"],        # e.g. "home", "medicare" — label until enriched
        account["customer_id"],     # platform_account_id
        mcc_id,
    ))

    # Phase 2: wire business_unit_id if not already set
    # Looks up domain from CUSTOMER_DOMAIN_MAP, then resolves business_units.id.
    # Logs a warning if business_units table doesn't exist yet (00 not run)
    # or if no matching business unit row is found, rather than raising.
    domain = CUSTOMER_DOMAIN_MAP.get(account["customer_id"])
    if not domain:
        log.warning(
            f"  No domain mapping for {account['customer_id']} in CUSTOMER_DOMAIN_MAP "
            f"— business_unit_id will not be set. Add to CUSTOMER_DOMAIN_MAP and re-run."
        )
        return

    # Check business_units table exists (requires 00_seed_business_units.py)
    tbl_exists = cursor.execute("""
        SELECT 1 FROM sqlite_master WHERE type='table' AND name='business_units'
    """).fetchone()
    if not tbl_exists:
        log.warning(
            "  business_units table not found — run 00_seed_business_units.py first. "
            "business_unit_id will not be set on this run."
        )
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

    # Only update if NULL — never overwrite a value already set by 00 or a prior run
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
) -> int:
    request = client.get_type("SearchGoogleAdsRequest")
    request.customer_id = account["customer_id"]
    request.query = CAMPAIGN_QUERY

    cursor = conn.cursor()
    synced = 0

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
            account["customer_id"],         # platform_account_id string — no FK
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
            None,                           # bidding_strategy_name: enrichment pass
            tCPA,
            tROAS,
            None, None, None, None, 0,      # parsed_* — set by 04_parse_campaign_names.py
            json.dumps(label_list) if label_list else None,
            first_seen,
            now_utc(),
        ))
        synced += 1

    conn.commit()
    return synced


# ── Sync: ad groups ───────────────────────────────────────────────────────────

def sync_ad_groups(
    client,
    ga_service,
    conn,
    account: dict,
) -> int:
    request = client.get_type("SearchGoogleAdsRequest")
    request.customer_id = account["customer_id"]
    request.query = AD_GROUP_QUERY

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
            account["customer_id"],         # platform_account_id string — no FK
            ag.name,
            ag.status.name,
            ag.type_.name if ag.type_ else None,
            ag.cpc_bid_micros or None,
            ag_tCPA,
            ag_tROAS,
            bid_override_flag,
            None, None, None, 0,            # parsed_* — set by 04_parse_campaign_names.py
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
    cursor.execute("""
        SELECT ag.ad_group_id, ag.ad_group_name, c.campaign_name,
               c.account_id, ag.target_cpa_micros, ag.target_roas
        FROM   ad_groups ag
        JOIN   campaigns c ON ag.campaign_id = c.campaign_id
        WHERE  ag.bid_override_flag = 1
        ORDER  BY c.account_id, c.campaign_name, ag.ad_group_name
    """)
    overrides = cursor.fetchall()

    if overrides:
        log.warning(
            f"  {len(overrides)} ad group(s) have bid strategy targets set "
            f"(bid_override_flag = 1) — structural anomaly, review required:"
        )
        for row in overrides:
            tCPA_str  = f"tCPA=${row['target_cpa_micros']/1e6:.2f}" if row['target_cpa_micros'] else ""
            tROAS_str = f"tROAS={row['target_roas']:.2f}"           if row['target_roas']        else ""
            flags = ", ".join(filter(None, [tCPA_str, tROAS_str]))
            log.warning(
                f"    acct={row['account_id']}  "
                f"{row['campaign_name']} > {row['ad_group_name']}  ({flags})"
            )
    else:
        log.info("  No ad group-level bid overrides found across any account.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    accounts = discover_accounts()
    log.info(f"Discovered {len(accounts)} active account(s) from .env:")
    for a in accounts:
        mode = "standalone" if a["is_standalone"] else f"MCC {a['login_customer_id']}"
        log.info(f"  {a['vertical']:<20} {a['customer_id']}  ({mode})")

    conn   = get_db_connection()
    cursor = conn.cursor()

    total_campaigns = 0
    total_adgroups  = 0

    for account in accounts:
        log.info(f"\n── {account['vertical'].upper()} ({account['customer_id']}) ────────────")

        # Ensure account row exists (required before campaign inserts reference it)
        upsert_account(cursor, account)
        conn.commit()

        # Confirm account is present and log its internal id for traceability
        internal_id = get_account_internal_id(cursor, account["customer_id"])
        log.info(f"  accounts.id = {internal_id}")

        client     = get_google_ads_client(account["login_customer_id"])
        ga_service = client.get_service("GoogleAdsService")

        log.info("  Syncing campaigns...")
        n_c = sync_campaigns(client, ga_service, conn, account)
        log.info(f"  {n_c} campaigns upserted.")
        total_campaigns += n_c

        log.info("  Syncing ad groups...")
        n_ag = sync_ad_groups(client, ga_service, conn, account)
        log.info(f"  {n_ag} ad groups upserted.")
        total_adgroups += n_ag

    log.info(f"\n── Summary ───────────────────────────────────────────────────────")
    log.info(f"  Total campaigns upserted : {total_campaigns:,}")
    log.info(f"  Total ad groups upserted : {total_adgroups:,}")
    log.info("")
    report_bid_overrides(conn)

    conn.close()
    log.info("Done.")
    log.info("Next step: python 03_sync_campaign_performance.py --daily-start 2025-01-01 --hourly-days 90")


if __name__ == "__main__":
    main()
