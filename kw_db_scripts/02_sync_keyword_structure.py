"""
02_sync_keyword_structure.py  [v3 — corrected composite key]
========================================================
Syncs keyword_instances and keyword_identities from the Google Ads API.

Changes from v2:
  - keyword_instances lookup and UPDATE WHERE clause corrected from
      WHERE account_id = ? AND criterion_id = ?
    to
      WHERE account_id = ? AND ad_group_id = ? AND criterion_id = ?
    This matches the corrected UNIQUE constraint introduced in
    kw_db_scripts/01_create_schema.py v5:
      UNIQUE (account_id, ad_group_id, criterion_id)
    Without this fix, upsert_keywords() would fail to find existing rows
    when the same criterion_id appears in multiple ad groups (which is
    the normal case for state-by-state campaigns sharing ad group names),
    causing duplicate inserts and constraint violations.

  - INSERT now correctly relies on the ON CONFLICT resolution from the
    UNIQUE (account_id, ad_group_id, criterion_id) constraint. The
    explicit existence check + branch is retained for clarity and to
    maintain the inserted/updated count reporting.

Everything else from v2 is unchanged:
  - Per-campaign GAQL (WHERE campaign.id = ?) — fixes stale campaign_id
  - identity_id updated on every run — fixes stale match_type labels
  - Geo constants bootstrap, negative keyword sync, summary reporting

USAGE
─────
    conda activate paid-search

    # Sync one account:
    python kw_db_scripts/02_sync_keyword_structure.py --account home
    python kw_db_scripts/02_sync_keyword_structure.py --account medicare
    python kw_db_scripts/02_sync_keyword_structure.py --account auto
    python kw_db_scripts/02_sync_keyword_structure.py --account term_life
    python kw_db_scripts/02_sync_keyword_structure.py --account auto_quote
"""

import os
import re
import sqlite3
import argparse
from pathlib import Path
from datetime import date

from dotenv import load_dotenv
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

load_dotenv()

# ── Constants ─────────────────────────────────────────────────────
DB_PATH   = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
YAML_PATH = Path(r"C:\Projects\google-ads-ops\google-ads.yaml")
TODAY     = date.today().isoformat()

MATCH_TYPE_MAP = {2: "EXACT", 3: "PHRASE", 4: "BROAD"}  # Google enum: 2=EXACT,3=PHRASE,4=BROAD (was swapped)

ACCOUNT_MAP = {
    "home":       {
        "customer_id_env":       "GOOGLE_ADS_CUSTOMER_ID_HOME",
        "login_customer_id_env": None,
        "display_name":          "Home Insurance",
    },
    "medicare":   {
        "customer_id_env":       "GOOGLE_ADS_CUSTOMER_ID_MEDICARE",
        "login_customer_id_env": None,
        "display_name":          "Medicare",
    },
    "auto":       {
        "customer_id_env":       "GOOGLE_ADS_CUSTOMER_ID_AUTO",
        "login_customer_id_env": None,
        "display_name":          "Auto Insurance (autoinsurance.org)",
    },
    "term_life":  {
        "customer_id_env":       "GOOGLE_ADS_CUSTOMER_ID_TERM_LIFE",
        "login_customer_id_env": None,
        "display_name":          "Term Life",
    },
    "auto_quote": {
        "customer_id_env":       "GOOGLE_ADS_CUSTOMER_ID_AUTO_QUOTE",
        "login_customer_id_env": "GOOGLE_ADS_LOGIN_CUSTOMER_ID_AUTO_QUOTE",
        "display_name":          "Auto Insurance (auto.quote.com)",
    },
}


# ── DB helpers ────────────────────────────────────────────────────
def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


def get_account_id(cursor, platform_account_id: str) -> int:
    row = cursor.execute(
        "SELECT id FROM accounts "
        "WHERE platform_account_id = ? AND platform = 'google'",
        (platform_account_id,),
    ).fetchone()
    if not row:
        raise ValueError(
            f"Account '{platform_account_id}' not found. "
            "Run 01_create_schema.py first."
        )
    return row["id"]


def normalize_keyword_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).lower()


def make_identity_key(normalized_text: str, match_type: str) -> str:
    return f"{normalized_text}::{match_type.upper()}"


def get_or_create_identity(cursor, normalized_text: str,
                           match_type: str) -> int:
    key = make_identity_key(normalized_text, match_type)
    row = cursor.execute(
        "SELECT id FROM keyword_identities WHERE identity_key = ?",
        (key,)
    ).fetchone()
    if row:
        return row["id"]
    cursor.execute(
        "INSERT INTO keyword_identities "
        "(normalized_text, match_type, identity_key) VALUES (?, ?, ?)",
        (normalized_text, match_type.upper(), key),
    )
    return cursor.lastrowid


# ── API helpers ───────────────────────────────────────────────────
def get_google_ads_client(login_customer_id=None):
    client = (GoogleAdsClient.load_from_storage(str(YAML_PATH))
              if YAML_PATH.exists()
              else GoogleAdsClient.load_from_storage())
    if login_customer_id:
        client.login_customer_id = login_customer_id
    return client


def discover_campaigns(ga_service, customer_id: str) -> list[dict]:
    """
    Returns all active SEARCH campaigns for the account.
    Drives the per-campaign keyword fetch loop.
    """
    query = """
        SELECT
            campaign.id,
            campaign.name,
            campaign.status
        FROM campaign
        WHERE campaign.advertising_channel_type = 'SEARCH'
          AND campaign.status != 'REMOVED'
        ORDER BY campaign.name
    """
    results = []
    try:
        response = ga_service.search_stream(customer_id=customer_id,
                                            query=query)
        for batch in response:
            for row in batch.results:
                results.append({
                    "campaign_id":   str(row.campaign.id),
                    "campaign_name": row.campaign.name,
                    "status":        row.campaign.status.name,
                })
    except GoogleAdsException as ex:
        print(f"  ERROR discovering campaigns: {ex.error.code().name}")
        for error in ex.failure.errors:
            print(f"    {error.message}")
        raise
    return results


def fetch_keywords_for_campaign(ga_service, customer_id: str,
                                campaign_id: str) -> list[dict]:
    """
    Fetches all ENABLED/PAUSED keywords for a single campaign.
    WHERE campaign.id = ? ensures each keyword slot is attributed
    to its owning campaign, eliminating cross-campaign contamination.
    """
    query = f"""
        SELECT
            ad_group_criterion.criterion_id,
            ad_group_criterion.keyword.text,
            ad_group_criterion.keyword.match_type,
            ad_group_criterion.status,
            ad_group_criterion.cpc_bid_micros,
            ad_group_criterion.quality_info.quality_score,
            campaign.id,
            campaign.name,
            ad_group.id,
            ad_group.name
        FROM keyword_view
        WHERE
            campaign.id = {campaign_id}
            AND ad_group_criterion.type = 'KEYWORD'
            AND ad_group_criterion.status IN ('ENABLED', 'PAUSED')
            AND campaign.advertising_channel_type = 'SEARCH'
            AND campaign.status != 'REMOVED'
            AND ad_group.status != 'REMOVED'
    """
    results = []
    try:
        response = ga_service.search_stream(customer_id=customer_id,
                                            query=query)
        for batch in response:
            for row in batch.results:
                match_int = row.ad_group_criterion.keyword.match_type
                bid = row.ad_group_criterion.cpc_bid_micros
                qs  = row.ad_group_criterion.quality_info.quality_score
                results.append({
                    "criterion_id":  str(row.ad_group_criterion.criterion_id),
                    "keyword_text":  row.ad_group_criterion.keyword.text,
                    "match_type":    MATCH_TYPE_MAP.get(
                                         match_int,
                                         f"UNKNOWN_{match_int}"),
                    "status":        row.ad_group_criterion.status.name,
                    "cpc_bid_micros":bid if bid > 0 else None,
                    "quality_score": qs  if qs  > 0 else None,
                    "campaign_id":   str(row.campaign.id),
                    "campaign_name": row.campaign.name,
                    "ad_group_id":   str(row.ad_group.id),
                    "ad_group_name": row.ad_group.name,
                })
    except GoogleAdsException as ex:
        print(f"    ERROR fetching keywords: {ex.error.code().name}")
        for error in ex.failure.errors:
            print(f"      {error.message}")
        raise
    return results


def fetch_negative_keywords(ga_service, customer_id: str) -> list[dict]:
    """Fetches campaign-level and ad-group-level negative keywords."""
    results = []

    # Campaign-level negatives
    camp_query = """
        SELECT
            campaign_criterion.keyword.text,
            campaign_criterion.keyword.match_type,
            campaign.name
        FROM campaign_criterion
        WHERE
            campaign_criterion.type = 'KEYWORD'
            AND campaign_criterion.negative = true
            AND campaign.status != 'REMOVED'
    """
    try:
        response = ga_service.search_stream(customer_id=customer_id,
                                            query=camp_query)
        for batch in response:
            for row in batch.results:
                match_int = row.campaign_criterion.keyword.match_type
                results.append({
                    "keyword_text":  row.campaign_criterion.keyword.text,
                    "match_type":    MATCH_TYPE_MAP.get(match_int, "UNKNOWN"),
                    "level":         "campaign",
                    "campaign_name": row.campaign.name,
                    "ad_group_name": None,
                })
    except GoogleAdsException as ex:
        print(f"  WARNING: could not fetch campaign negatives: "
              f"{ex.error.code().name}")

    # Ad-group-level negatives
    ag_query = """
        SELECT
            ad_group_criterion.keyword.text,
            ad_group_criterion.keyword.match_type,
            campaign.name,
            ad_group.name
        FROM ad_group_criterion
        WHERE
            ad_group_criterion.type = 'KEYWORD'
            AND ad_group_criterion.negative = true
            AND campaign.status != 'REMOVED'
            AND ad_group.status != 'REMOVED'
    """
    try:
        response = ga_service.search_stream(customer_id=customer_id,
                                            query=ag_query)
        for batch in response:
            for row in batch.results:
                match_int = row.ad_group_criterion.keyword.match_type
                results.append({
                    "keyword_text":  row.ad_group_criterion.keyword.text,
                    "match_type":    MATCH_TYPE_MAP.get(match_int, "UNKNOWN"),
                    "level":         "ad_group",
                    "campaign_name": row.campaign.name,
                    "ad_group_name": row.ad_group.name,
                })
    except GoogleAdsException as ex:
        print(f"  WARNING: could not fetch ad-group negatives: "
              f"{ex.error.code().name}")

    return results


def fetch_geo_target_constants(ga_service, customer_id: str) -> list[dict]:
    query = """
        SELECT
            geo_target_constant.id,
            geo_target_constant.name,
            geo_target_constant.canonical_name,
            geo_target_constant.country_code,
            geo_target_constant.target_type
        FROM geo_target_constant
        WHERE geo_target_constant.country_code = 'US'
    """
    results = []
    try:
        response = ga_service.search_stream(customer_id=customer_id,
                                            query=query)
        for batch in response:
            for row in batch.results:
                g = row.geo_target_constant
                results.append({
                    "criterion_id":   g.id,
                    "name":           g.name,
                    "canonical_name": g.canonical_name,
                    "country_code":   g.country_code,
                    "target_type":    g.target_type,
                })
    except GoogleAdsException as ex:
        print(f"  WARNING: could not fetch geo constants: "
              f"{ex.error.code().name}")
    return results


# ── Upsert helpers ────────────────────────────────────────────────
def upsert_keywords(conn, account_id: int,
                    keywords: list[dict]) -> dict:
    """
    Upserts keyword_instances and keyword_identities.

    v3 change: existence check and UPDATE WHERE clause now use the
    corrected composite key (account_id, ad_group_id, criterion_id),
    matching the UNIQUE constraint in keyword_instances v5.

    Why this matters: the same criterion_id can appear in multiple
    ad groups across different campaigns (e.g. the same ad group name
    pattern used in 100+ state campaigns). The old two-column lookup
    (account_id, criterion_id) would find a different campaign's row
    and overwrite it, or fail to find the right row entirely.
    The three-column lookup is unambiguous.
    """
    cursor = conn.cursor()
    counts = {"inserted": 0, "updated": 0}

    for kw in keywords:
        normalized  = normalize_keyword_text(kw["keyword_text"])
        identity_id = get_or_create_identity(cursor, normalized,
                                             kw["match_type"])

        # v3: lookup on corrected composite key
        existing = cursor.execute(
            """
            SELECT id FROM keyword_instances
            WHERE account_id  = ?
              AND ad_group_id = ?
              AND criterion_id = ?
            """,
            (account_id, kw["ad_group_id"], kw["criterion_id"]),
        ).fetchone()

        if existing is None:
            cursor.execute(
                """
                INSERT INTO keyword_instances
                    (account_id, identity_id, criterion_id, keyword_text,
                     match_type, campaign_id, ad_group_id,
                     ad_group_criterion_id,
                     campaign_name, ad_group_name, status,
                     cpc_bid_micros, quality_score,
                     first_seen_date, last_seen_date, platform)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'google')
                """,
                (
                    account_id, identity_id,
                    kw["criterion_id"], kw["keyword_text"], kw["match_type"],
                    kw["campaign_id"], kw["ad_group_id"],
                    # per-placement edit key: '{ad_group_id}~{criterion_id}'
                    f'{kw["ad_group_id"]}~{kw["criterion_id"]}',
                    kw["campaign_name"], kw["ad_group_name"],
                    kw["status"], kw["cpc_bid_micros"], kw["quality_score"],
                    TODAY, TODAY,
                ),
            )
            counts["inserted"] += 1
        else:
            # v2: identity_id updated so stale match_type labels are corrected
            # v3: WHERE clause uses corrected composite key
            cursor.execute(
                """
                UPDATE keyword_instances SET
                    identity_id     = ?,
                    match_type      = ?,
                    status          = ?,
                    cpc_bid_micros  = ?,
                    quality_score   = ?,
                    campaign_id     = ?,
                    ad_group_id     = ?,
                    ad_group_criterion_id = ?,
                    campaign_name   = ?,
                    ad_group_name   = ?,
                    last_seen_date  = ?
                WHERE account_id  = ?
                  AND ad_group_id = ?
                  AND criterion_id = ?
                """,
                (
                    identity_id, kw["match_type"],
                    kw["status"], kw["cpc_bid_micros"], kw["quality_score"],
                    kw["campaign_id"], kw["ad_group_id"],
                    # keep the edit key populated (defensive; Step 1 backfilled existing rows)
                    f'{kw["ad_group_id"]}~{kw["criterion_id"]}',
                    kw["campaign_name"], kw["ad_group_name"],
                    TODAY,
                    account_id, kw["ad_group_id"], kw["criterion_id"],
                ),
            )
            counts["updated"] += 1

    conn.commit()
    return counts


def upsert_negatives(conn, account_id: int,
                     negatives: list[dict]) -> dict:
    cursor = conn.cursor()
    counts = {"inserted": 0, "updated": 0}
    for neg in negatives:
        existing = cursor.execute(
            """
            SELECT id FROM negative_keywords
            WHERE account_id = ? AND platform = 'google'
              AND level = ? AND campaign_name = ?
              AND COALESCE(ad_group_name, '') = COALESCE(?, '')
              AND keyword_text = ? AND match_type = ?
            """,
            (account_id, neg["level"], neg["campaign_name"],
             neg["ad_group_name"], neg["keyword_text"], neg["match_type"]),
        ).fetchone()
        if existing is None:
            cursor.execute(
                """
                INSERT INTO negative_keywords
                    (account_id, keyword_text, match_type, level,
                     campaign_name, ad_group_name,
                     first_seen_date, last_seen_date, platform)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'google')
                """,
                (account_id, neg["keyword_text"], neg["match_type"],
                 neg["level"], neg["campaign_name"], neg["ad_group_name"],
                 TODAY, TODAY),
            )
            counts["inserted"] += 1
        else:
            cursor.execute(
                "UPDATE negative_keywords SET last_seen_date = ? WHERE id = ?",
                (TODAY, existing["id"]),
            )
            counts["updated"] += 1
    conn.commit()
    return counts


def upsert_geo_constants(conn, rows: list[dict]):
    cursor = conn.cursor()
    for r in rows:
        cursor.execute(
            """
            INSERT OR IGNORE INTO geo_target_constants
                (criterion_id, name, canonical_name, country_code, target_type)
            VALUES (?, ?, ?, ?, ?)
            """,
            (r["criterion_id"], r["name"], r["canonical_name"],
             r["country_code"], r["target_type"]),
        )
    conn.commit()


# ── Args ──────────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(
        description="Sync keyword structure per campaign (v3 — corrected "
                    "composite key)."
    )
    p.add_argument("--account", required=True,
                   choices=list(ACCOUNT_MAP.keys()),
                   help="Account to sync.")
    return p.parse_args()


# ── Main ──────────────────────────────────────────────────────────
def main():
    args        = parse_args()
    account_cfg = ACCOUNT_MAP[args.account]

    customer_id = os.getenv(account_cfg["customer_id_env"])
    if not customer_id:
        raise ValueError(
            f"{account_cfg['customer_id_env']} is not set in your .env file."
        )
    login_customer_id = (
        os.getenv(account_cfg["login_customer_id_env"])
        if account_cfg["login_customer_id_env"] else None
    )

    print("=" * 65)
    print("02_sync_keyword_structure.py  [v3 — corrected composite key]")
    print(f"  Account : {account_cfg['display_name']} ({customer_id})")
    print(f"  Date    : {TODAY}")
    print("=" * 65)

    conn   = get_db_connection()
    cursor = conn.cursor()
    account_id = get_account_id(cursor, customer_id)
    print(f"\n  Database account_id (integer): {account_id}")

    print("\nConnecting to Google Ads API...")
    client     = get_google_ads_client(login_customer_id)
    ga_service = client.get_service("GoogleAdsService")
    print("  Connected.")

    # ── Step 1: Geo target constants ──────────────────────────────
    geo_count = cursor.execute(
        "SELECT COUNT(*) FROM geo_target_constants"
    ).fetchone()[0]
    if geo_count == 0:
        print("\n[1/4] Fetching geo target constants (one-time bootstrap)...")
        geo_rows = fetch_geo_target_constants(ga_service, customer_id)
        upsert_geo_constants(conn, geo_rows)
        print(f"  Loaded {len(geo_rows):,} US geo constants")
    else:
        print(f"\n[1/4] Geo constants already loaded "
              f"({geo_count:,} rows) — skipping")

    # ── Step 2: Discover campaigns ────────────────────────────────
    print("\n[2/4] Discovering active SEARCH campaigns...")
    campaigns = discover_campaigns(ga_service, customer_id)
    print(f"  Found {len(campaigns):,} campaigns")
    for c in campaigns:
        print(f"    {c['campaign_id']:<15} [{c['status']:<7}]  "
              f"{c['campaign_name']}")

    # ── Step 3: Keywords — per campaign ──────────────────────────
    print(f"\n[3/4] Syncing keywords per campaign "
          f"({len(campaigns)} campaigns)...")
    total_counts = {"inserted": 0, "updated": 0}

    for i, camp in enumerate(campaigns, 1):
        cid   = camp["campaign_id"]
        cname = camp["campaign_name"]
        print(f"\n  [{i}/{len(campaigns)}] {cname} ({cid})")

        keywords = fetch_keywords_for_campaign(ga_service, customer_id, cid)
        print(f"    API returned {len(keywords):,} keyword rows", end=" ")

        counts = upsert_keywords(conn, account_id, keywords)
        print(f"→ {counts['inserted']:,} inserted, "
              f"{counts['updated']:,} updated")

        total_counts["inserted"] += counts["inserted"]
        total_counts["updated"]  += counts["updated"]

    print(f"\n  Total: {total_counts['inserted']:,} inserted, "
          f"{total_counts['updated']:,} updated")

    # ── Step 4: Negative keywords ─────────────────────────────────
    print("\n[4/4] Fetching negative keywords (account-wide)...")
    negatives   = fetch_negative_keywords(ga_service, customer_id)
    camp_neg    = sum(1 for n in negatives if n["level"] == "campaign")
    ag_neg      = sum(1 for n in negatives if n["level"] == "ad_group")
    print(f"  API returned {len(negatives):,} negatives "
          f"({camp_neg:,} campaign-level, {ag_neg:,} ad-group-level)")

    neg_counts = upsert_negatives(conn, account_id, negatives)
    print(f"  Inserted : {neg_counts['inserted']:,} new negatives")
    print(f"  Updated  : {neg_counts['updated']:,} existing negatives")

    # ── Summary ───────────────────────────────────────────────────
    print("\n--- Database summary ---")
    summary = [
        ("keyword_identities total",
         "SELECT COUNT(*) FROM keyword_identities"),
        (f"keyword_instances ({args.account})",
         f"SELECT COUNT(*) FROM keyword_instances "
         f"WHERE account_id = {account_id}"),
        ("  -- ENABLED",
         f"SELECT COUNT(*) FROM keyword_instances "
         f"WHERE account_id = {account_id} AND status = 'ENABLED'"),
        ("  -- PAUSED",
         f"SELECT COUNT(*) FROM keyword_instances "
         f"WHERE account_id = {account_id} AND status = 'PAUSED'"),
        ("  -- with campaign_id populated",
         f"SELECT COUNT(*) FROM keyword_instances "
         f"WHERE account_id = {account_id} AND campaign_id IS NOT NULL"),
        (f"negative_keywords ({args.account})",
         f"SELECT COUNT(*) FROM negative_keywords "
         f"WHERE account_id = {account_id}"),
        ("geo_target_constants",
         "SELECT COUNT(*) FROM geo_target_constants"),
    ]
    for label, q in summary:
        n = cursor.execute(q).fetchone()[0]
        print(f"  {label:<45} {n:>7,}")

    conn.close()
    import os as _os
    size_mb = _os.path.getsize(DB_PATH) / (1024 * 1024)
    print(f"\nDatabase size: {size_mb:.2f} MB  |  Path: {DB_PATH}")
    print(f"\nDone.")
    print(f"\nNext step:")
    print(f"  python kw_db_scripts/sync_all_campaigns_home.py "
          f"--refresh 90 --skip-kw-struct")


if __name__ == "__main__":
    main()
