"""
05_sync_search_queries.py  [v7 — + lock pre-flight & busy_timeout]
=============================================================================
Syncs search_query_performance for one account by looping over every SEARCH
campaign that served traffic in the pull window and querying search_term_view
with WHERE campaign.id = ? for each one.

Replaces and archives:
  - 05_sync_search_queries.py  [v1/v2 — account-wide, old composite key]
  - sync_search_queries_by_campaign.py — logic absorbed here

CHANGELOG v6 → v7
─────────────────
Concurrency hardening. The script previously set no busy_timeout, so if another
process held the DB write lock (e.g. a Ctrl-C'd run that lingered on Windows),
a commit would hang silently and indefinitely instead of erroring.
  * PRAGMA busy_timeout = 15000 on the connection: a contended lock now waits up
    to 15s, then raises a clear 'database is locked' instead of freezing.
  * Pre-flight write-lock check in main(): before the probe + API loop, the
    script does BEGIN IMMEDIATE / ROLLBACK. If it can't get the lock it prints a
    clear message (kill the stray python / run db_unlock_check.py) and exits
    without doing any work — so a lock is caught in ~15s at startup, not after
    minutes of API calls wedging at the first commit.
  * Startup WAL checkpoint(TRUNCATE) so a long backfill begins with a compact WAL.
  NOTE: this does not PREVENT a collision — only one writer should touch
  paid_search.db at a time. It makes a collision fast and obvious instead of a
  silent hang. When wiring the weekly job into the orchestrator, ensure it can't
  overlap the daily refresh.

CHANGELOG v5 → v6
─────────────────
sqp UNIQUE key widened to include match_type_served (migration:
migrate_sqp_add_matchtype_key.py). The ON CONFLICT target below now matches the
5-col key. Previously the 4-col key (query_id, instance_id, report_date, device)
collapsed rows that differed only by match type (last-write-wins), silently
losing one match type's clicks/cost and mislabeling the survivor — corrupting
any per-match-type analysis. RUN THE MIGRATION FIRST; writes will fail against
the old constraint otherwise. After migration, re-pull all windows to recover
the previously-collapsed rows.

CHANGELOG v4 → v5
─────────────────
Added a cheap campaign-level probe (discover_active_campaigns) that runs ONE
query over the pull window and returns only campaigns with impressions > 0,
so the expensive per-campaign search_term_view loop skips the ~110 paused/empty
state shells (each of which otherwise fired 2 wasted API calls).

  * Safe: campaign impressions are a strict superset of search-term rows, so a
    campaign with 0 impressions in the window provably has 0 search terms — no
    false negatives. Google omits all-zero-metric rows, so empty campaigns
    don't come back at all; the impressions>0 guard is a defensive backstop.
  * Keys off traffic IN THE WINDOW, not current status, so a campaign PAUSED
    today but live in 2025 is still picked up for a 2025 backfill.
  * Benefits both invocations: backfill skips dead shells; the weekly
    --refresh 14 run drops from ~2 min to ~15 s (~7 campaigns served).
  * --campaign-id bypasses the probe (single-campaign mode unchanged).

CHANGELOG v3 → v4
─────────────────
Pass 1b previously selected metrics.all_conversions_by_conversion_date and
metrics.all_conversions_value_by_conversion_date from search_term_view and
filtered on the former in WHERE. Both are rejected by the API for
search_term_view (PROHIBITED_METRIC_IN_SELECT_OR_WHERE_CLAUSE); every Pass 1b
call raised, the exception unwound sync_campaign before aggregate_and_write
ran, and the already-fetched Pass 1a traffic was discarded → zero rows written.

  * PRIMARY PATH (preferred): same per-conversion_action segmentation, now using
    the click-date metrics metrics.all_conversions / metrics.all_conversions_value.
    Routing via conv_map / include_mode is unchanged. Click-date attribution is
    correct for query-level analysis and is what search_term_view forces anyway.

  * FALLBACK PATH (fires ONLY if the segmented query is rejected): re-queries
    WITHOUT segments.conversion_action.

      ┌──────────────────────────────────────────────────────────────────────┐
      │ GOOGLE-HOME-SPECIFIC SHORTCUT — DO NOT GENERALIZE.                      │
      │ Maps metrics.conversions → primary. Valid ONLY because Google Home has │
      │ exactly one primary conversion action, so metrics.conversions == that  │
      │ one primary action's count. NOT a routing convention:                  │
      │   • Other Google accounts (auto=2, auto_quote=2 primary) → metrics.    │
      │     conversions SUMS across multiple primaries.                        │
      │   • Microsoft has no Primary/Secondary designation and no equivalent   │
      │     metrics.conversions. Microsoft sqp MUST route explicitly through   │
      │     conversion_actions.include_mode — never a platform-native primary  │
      │     metric. The conversion_actions table exists to abstract this; the  │
      │     fallback bypasses it and is scoped to single-primary Google Home.  │
      └──────────────────────────────────────────────────────────────────────┘

      Fallback semantics (Google Home):
        primary_conv_count   = metrics.conversions          (exact: 1 primary action)
        primary_conv_value   = metrics.conversions_value     (exact)
        secondary_conv_count = 0                             (not separable w/o segmentation)
        all_conv_count       = metrics.all_conversions       (see divergence note)
        all_conv_value       = metrics.all_conversions_value
      Divergence note: metrics.all_conversions counts EVERY conversion action,
      including include_mode='excluded' ones. Home currently has TWO excluded
      actions ('qw home gbraids', 'qw submit lead'), so on the fallback path
      all_conv_* will run higher than the segmented path (which skips excluded).
      primary_conv_* is unaffected. The segmented path handles all five actions
      correctly; the divergence only applies if segmentation is rejected.

KNOWN PRE-EXISTING ISSUE — NOT FIXED IN v4 (needs a grain decision)
────────────────────────────────────────────────────────────────────
The sqp PK is (query_id, instance_id, report_date, device); match_type_served
is NOT in the key. If one search term serves under >1 match type for the same
keyword+date, the in-memory traffic rows (keyed WITH match type) collide on
write and last-match-type-wins — clicks/cost overwrite rather than sum.
Conversion matching on key_base[:5] likewise ignores match type. Preserved
as-is. Resolve by deciding whether match_type_served belongs in the grain.

WHY PER-CAMPAIGN
────────────────
Account-wide search_term_view queries produce ambiguous campaign attribution
for accounts with shared ad groups. Per-campaign scoping matches the Google
Ads UI and correctly attributes each query to the campaign that served it.
(NOTE: this rationale is under review — campaign.id is in every row's SELECT
and ad_group_id is embedded in the criterion resource, so per-row attribution
may be fully determined without the per-campaign WHERE filter. If confirmed,
account-level chunking would cut API calls ~10x for Home. Validate before
changing.)

COMPOSITE KEY FIX
─────────────────
The old get_instance_id() used (account_id, criterion_id) — ambiguous when
the same criterion_id appears in multiple ad groups. The corrected lookup
uses (account_id, ad_group_id, criterion_id).

ad_group_id is extracted from the ad_group_criterion resource name, which
the API returns in the format:
  customers/{cid}/adGroupCriteria/{ag_id}~{criterion_id}
We parse it as: resource.split("/")[3] = ag_id, resource.split("~")[-1] = criterion_id

DATE MODES
──────────
  --start / --end   explicit range (recommended for backfills)
  --incremental     from last synced date with 3-day overlap
  --refresh N       last N days

USAGE
─────
    conda activate paid-search

    # Full backfill from Jan 2025:
    python kw_db_scripts/05_sync_search_queries.py --account home --start 2025-01-01

    # Last 90 days (default incremental):
    python kw_db_scripts/05_sync_search_queries.py --account home --incremental

    # Specific date range:
    python kw_db_scripts/05_sync_search_queries.py \\
        --account home --start 2026-01-01 --end 2026-04-14

    # Single campaign only:
    python kw_db_scripts/05_sync_search_queries.py \\
        --account home --start 2026-01-01 --campaign-id 23496334752

    # Other accounts:
    python kw_db_scripts/05_sync_search_queries.py --account medicare --incremental
    python kw_db_scripts/05_sync_search_queries.py --account auto --incremental
"""

import os
import sys
import sqlite3
import argparse
import time
from pathlib import Path
from datetime import date, timedelta

from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

load_dotenv()

# ── Configuration ─────────────────────────────────────────────────────────────

DB_PATH          = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
YAML_PATH        = Path(r"C:\Projects\google-ads-ops\google-ads.yaml")
DEFAULT_DAYS     = 90
INCREMENTAL_DAYS = 3

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


# ── Args ──────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Sync search_query_performance per campaign for one account."
    )
    p.add_argument("--account", required=True, choices=list(ACCOUNT_MAP.keys()),
                   help="Account to sync.")

    date_group = p.add_mutually_exclusive_group(required=True)
    date_group.add_argument("--start", default=None, metavar="YYYY-MM-DD",
                            help="Start date. Use with optional --end.")
    date_group.add_argument("--incremental", action="store_true",
                            help="Pull from last synced date with 3-day overlap.")
    date_group.add_argument("--refresh", type=int, metavar="N",
                            help="Re-pull last N days.")

    p.add_argument("--end", default=None, metavar="YYYY-MM-DD",
                   help="End date (default: yesterday). Only used with --start.")
    p.add_argument("--campaign-id", default=None, metavar="ID",
                   help="Sync a single campaign only. Omit to probe + loop all served campaigns.")
    return p.parse_args()


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=15.0)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA cache_size = -64000")
    conn.execute("PRAGMA busy_timeout = 15000")  # wait ≤15s for a lock, then error
    conn.row_factory = sqlite3.Row
    return conn


def get_account_id(cursor, platform_account_id: str) -> int:
    row = cursor.execute(
        "SELECT id FROM accounts WHERE platform_account_id = ? AND platform = 'google'",
        (platform_account_id,),
    ).fetchone()
    if not row:
        raise ValueError(
            f"Account '{platform_account_id}' not found. Run 01_create_schema.py first."
        )
    return row["id"]


def parse_ad_group_criterion_resource(resource: str) -> tuple[str, str]:
    """
    Extracts (ad_group_id, criterion_id) from the resource name.
    Format: customers/{cid}/adGroupCriteria/{ag_id}~{criterion_id}
    """
    try:
        parts        = resource.split("/")
        ag_and_crit  = parts[3]                      # "{ag_id}~{criterion_id}"
        ag_id        = ag_and_crit.split("~")[0]
        criterion_id = ag_and_crit.split("~")[1]
        return ag_id, criterion_id
    except (IndexError, AttributeError):
        return "", resource.split("~")[-1] if "~" in resource else ""


def get_instance_id(cursor, account_id: int,
                    ad_group_id: str, criterion_id: str) -> int | None:
    """
    Resolves keyword_instances.id via the corrected composite key
    (account_id, ad_group_id, criterion_id).
    """
    row = cursor.execute(
        """
        SELECT id FROM keyword_instances
        WHERE account_id   = ?
          AND ad_group_id  = ?
          AND criterion_id = ?
        """,
        (account_id, ad_group_id, criterion_id),
    ).fetchone()
    return row["id"] if row else None


def get_or_create_query(cursor, query_text: str,
                        report_date: str, platform: str) -> int:
    normalized = query_text.lower().strip()
    row = cursor.execute(
        "SELECT id FROM search_queries WHERE normalized_text = ?",
        (normalized,),
    ).fetchone()
    if row is None:
        cursor.execute(
            "INSERT INTO search_queries "
            "(query_text, normalized_text, first_seen_date, last_seen_date, first_seen_platform) "
            "VALUES (?, ?, ?, ?, ?)",
            (query_text, normalized, report_date, report_date, platform),
        )
        return cursor.lastrowid
    cursor.execute(
        "UPDATE search_queries SET last_seen_date = MAX(last_seen_date, ?) WHERE id = ?",
        (report_date, row["id"]),
    )
    return row["id"]


def load_conversion_action_map(cursor, account_id: int) -> dict:
    rows = cursor.execute(
        "SELECT LOWER(conversion_name) AS name, include_mode "
        "FROM conversion_actions WHERE account_id = ?",
        (account_id,),
    ).fetchall()
    if not rows:
        print("  WARNING: No conversion actions found — all conversions will be 'monitor'.")
    return {r["name"]: r["include_mode"] for r in rows}


def get_monthly_chunks(start: date, end: date) -> list[tuple[str, str]]:
    chunks, chunk_start = [], start.replace(day=1)
    while chunk_start <= end:
        chunk_end = min(
            (chunk_start + relativedelta(months=1)) - timedelta(days=1), end
        )
        chunks.append((chunk_start.isoformat(), chunk_end.isoformat()))
        chunk_start += relativedelta(months=1)
    return chunks


def get_last_synced_date(conn, account_id: int) -> date | None:
    row = conn.cursor().execute("""
        SELECT MAX(sqp.report_date) AS last_date
        FROM search_query_performance sqp
        JOIN keyword_instances ki ON sqp.instance_id = ki.id
        WHERE ki.account_id = ? AND sqp.platform = 'google' AND sqp.device = 'ALL'
    """, (account_id,)).fetchone()
    if row and row["last_date"]:
        return date.fromisoformat(row["last_date"])
    return None


# ── Google Ads client ─────────────────────────────────────────────────────────

def get_google_ads_client(login_customer_id=None):
    client = (GoogleAdsClient.load_from_storage(str(YAML_PATH))
              if YAML_PATH.exists()
              else GoogleAdsClient.load_from_storage())
    if login_customer_id:
        client.login_customer_id = login_customer_id
    return client


def _print_api_error(label, ex):
    print(f"\n  API ERROR in {label}:")
    for error in ex.failure.errors:
        print(f"    [{error.error_code}] {error.message}")


def _is_prohibited_combo_error(ex: GoogleAdsException) -> bool:
    """
    True iff the failure is a prohibited metric / segment / field-combination
    error — i.e., the segmented Pass 1b is unsupported on search_term_view and
    we should drop to the fallback. Any other error returns False so the caller
    re-raises and fails loud.
    """
    for err in ex.failure.errors:
        try:
            qe_name = err.error_code.query_error.name  # UNSPECIFIED if not a query_error
        except Exception:
            qe_name = ""
        if "PROHIBITED" in qe_name:
            return True
        if "PROHIBITED" in (err.message or "").upper():
            return True
    return False


# ── Campaign discovery ────────────────────────────────────────────────────────

def discover_campaigns(ga_service, customer_id: str) -> list[dict]:
    """
    Returns all non-REMOVED SEARCH campaigns — ENABLED and PAUSED.
    Retained for reference; superseded by discover_active_campaigns() for the
    looped path (which filters to campaigns that served in the window).
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
        response = ga_service.search_stream(customer_id=customer_id, query=query)
        for batch in response:
            for row in batch.results:
                results.append({
                    "campaign_id":   str(row.campaign.id),
                    "campaign_name": row.campaign.name,
                    "status":        row.campaign.status.name,
                })
    except GoogleAdsException as ex:
        _print_api_error("discover_campaigns", ex)
        raise
    return results


def discover_active_campaigns(ga_service, customer_id: str,
                              start: str, end: str) -> list[dict]:
    """
    Cheap pre-filter: one campaign-level query returns total impressions per
    SEARCH campaign over [start, end]. Returns only campaigns that served
    (impressions > 0) in the window — INCLUDING campaigns PAUSED today but live
    during the window (e.g. 2025 state campaigns sunset in the 2026
    restructure). Avoids firing the per-campaign search_term_view loop against
    dead shells.

    Safe because campaign impressions are a superset of search-term rows:
    0 impressions in the window ⇒ 0 search terms (no false negatives). Google
    omits all-zero-metric rows, so empty campaigns simply don't come back; the
    impressions>0 guard is a defensive backstop.
    """
    query = f"""
        SELECT
            campaign.id,
            campaign.name,
            campaign.status,
            metrics.impressions
        FROM campaign
        WHERE
            segments.date BETWEEN '{start}' AND '{end}'
            AND campaign.advertising_channel_type = 'SEARCH'
            AND campaign.status != 'REMOVED'
        ORDER BY campaign.name
    """
    served = []
    try:
        response = ga_service.search_stream(customer_id=customer_id, query=query)
        for batch in response:
            for row in batch.results:
                if row.metrics.impressions > 0:
                    served.append({
                        "campaign_id":   str(row.campaign.id),
                        "campaign_name": row.campaign.name,
                        "status":        row.campaign.status.name,
                    })
    except GoogleAdsException as ex:
        _print_api_error("discover_active_campaigns", ex)
        raise
    return served


# ── Conversion action name resolution ─────────────────────────────────────────

def resolve_conversion_action_name(resource, cache, ga_service, customer_id):
    if resource in cache:
        return cache[resource]
    try:
        q = (f"SELECT conversion_action.name FROM conversion_action "
             f"WHERE conversion_action.resource_name = '{resource}'")
        response = ga_service.search_stream(customer_id=customer_id, query=q)
        for batch in response:
            for row in batch.results:
                name = row.conversion_action.name.lower()
                cache[resource] = name
                return name
    except Exception:
        pass
    cache[resource] = "unknown"
    return "unknown"


# ── GAQL queries (campaign-scoped) ────────────────────────────────────────────

def fetch_query_traffic(ga_service, customer_id: str, campaign_id: str,
                        start: str, end: str) -> list[dict]:
    """Pass 1a: clicks/impressions/cost for search queries in this campaign."""
    query = f"""
        SELECT
            search_term_view.search_term,
            segments.keyword.ad_group_criterion,
            campaign.id,
            segments.date,
            segments.search_term_match_type,
            segments.ad_network_type,
            metrics.clicks,
            metrics.impressions,
            metrics.cost_micros
        FROM search_term_view
        WHERE
            campaign.id = {campaign_id}
            AND segments.date BETWEEN '{start}' AND '{end}'
            AND campaign.advertising_channel_type = 'SEARCH'
            AND segments.ad_network_type IN ('SEARCH', 'SEARCH_PARTNERS')
    """
    results = []
    try:
        response = ga_service.search_stream(customer_id=customer_id, query=query)
        for batch in response:
            for row in batch.results:
                resource     = row.segments.keyword.ad_group_criterion
                ag_id, crit  = parse_ad_group_criterion_resource(resource)
                results.append({
                    "query_text":        row.search_term_view.search_term,
                    "ad_group_id":       ag_id,
                    "criterion_id":      crit,
                    "campaign_id":       str(row.campaign.id),
                    "report_date":       row.segments.date,
                    "match_type_served": row.segments.search_term_match_type.name,
                    "clicks":            row.metrics.clicks,
                    "impressions":       row.metrics.impressions,
                    "cost_micros":       row.metrics.cost_micros,
                })
    except GoogleAdsException as ex:
        _print_api_error("Pass 1a (query traffic)", ex)
        raise
    return results


def fetch_query_conversions(ga_service, customer_id: str, campaign_id: str,
                            start: str, end: str) -> tuple[list[dict], str]:
    """
    Pass 1b: conversions for search queries in this campaign.

    Returns (rows, path):
      path == 'segmented' → rows carry segments.conversion_action and are routed
                            downstream via conv_map (primary/secondary_count/
                            monitor/excluded). Preferred.
      path == 'fallback'  → rows are pre-bucketed (primary_* / all_*) using the
                            Google-Home single-primary shortcut. See module header.
    """
    segmented_query = f"""
        SELECT
            search_term_view.search_term,
            segments.keyword.ad_group_criterion,
            campaign.id,
            segments.date,
            segments.ad_network_type,
            segments.conversion_action,
            metrics.all_conversions,
            metrics.all_conversions_value
        FROM search_term_view
        WHERE
            campaign.id = {campaign_id}
            AND segments.date BETWEEN '{start}' AND '{end}'
            AND campaign.advertising_channel_type = 'SEARCH'
            AND segments.ad_network_type IN ('SEARCH', 'SEARCH_PARTNERS')
            AND metrics.all_conversions > 0
    """

    # ── Primary path: per-conversion_action, routed via conv_map ──────────────
    try:
        results = []
        response = ga_service.search_stream(customer_id=customer_id, query=segmented_query)
        for batch in response:
            for row in batch.results:
                resource    = row.segments.keyword.ad_group_criterion
                ag_id, crit = parse_ad_group_criterion_resource(resource)
                results.append({
                    "query_text":        row.search_term_view.search_term,
                    "ad_group_id":       ag_id,
                    "criterion_id":      crit,
                    "campaign_id":       str(row.campaign.id),
                    "report_date":       row.segments.date,
                    "conversion_action": row.segments.conversion_action,
                    "all_conv_count":    row.metrics.all_conversions,
                    "all_conv_value":    row.metrics.all_conversions_value,
                })
        return results, "segmented"
    except GoogleAdsException as ex:
        if not _is_prohibited_combo_error(ex):
            _print_api_error("Pass 1b (segmented)", ex)
            raise
        print("    NOTE: segments.conversion_action unsupported on "
              "search_term_view here — dropping to single-primary fallback "
              "(Google-Home-specific; see module header).")

    # ── Fallback path: unsegmented, Google-Home single-primary shortcut ───────
    fallback_query = f"""
        SELECT
            search_term_view.search_term,
            segments.keyword.ad_group_criterion,
            campaign.id,
            segments.date,
            segments.ad_network_type,
            metrics.conversions,
            metrics.conversions_value,
            metrics.all_conversions,
            metrics.all_conversions_value
        FROM search_term_view
        WHERE
            campaign.id = {campaign_id}
            AND segments.date BETWEEN '{start}' AND '{end}'
            AND campaign.advertising_channel_type = 'SEARCH'
            AND segments.ad_network_type IN ('SEARCH', 'SEARCH_PARTNERS')
            AND metrics.all_conversions > 0
    """
    results = []
    try:
        response = ga_service.search_stream(customer_id=customer_id, query=fallback_query)
        for batch in response:
            for row in batch.results:
                resource    = row.segments.keyword.ad_group_criterion
                ag_id, crit = parse_ad_group_criterion_resource(resource)
                results.append({
                    "query_text":           row.search_term_view.search_term,
                    "ad_group_id":          ag_id,
                    "criterion_id":         crit,
                    "campaign_id":          str(row.campaign.id),
                    "report_date":          row.segments.date,
                    "primary_conv_count":   row.metrics.conversions,
                    "primary_conv_value":   row.metrics.conversions_value,
                    "secondary_conv_count": 0.0,
                    "all_conv_count":       row.metrics.all_conversions,
                    "all_conv_value":       row.metrics.all_conversions_value,
                })
    except GoogleAdsException as ex:
        _print_api_error("Pass 1b (fallback)", ex)
        raise
    return results, "fallback"


# ── Aggregation and write ─────────────────────────────────────────────────────

def aggregate_and_write(conn, cursor, account_id: int,
                        traffic_rows: list, conv_rows: list, conv_path: str,
                        conv_map: dict, conv_cache: dict,
                        ga_service, customer_id: str) -> dict:
    counts = {"written": 0, "skipped": 0, "new_queries": 0}

    # Aggregate traffic — key includes ad_group_id for composite lookup
    traffic: dict = {}
    for row in traffic_rows:
        key = (row["query_text"], row["ad_group_id"], row["criterion_id"],
               row["campaign_id"], row["report_date"], row["match_type_served"])
        if key not in traffic:
            traffic[key] = {
                "clicks": 0, "impressions": 0, "cost_micros": 0.0,
                "primary_conv_count": 0.0, "primary_conv_value": 0.0,
                "secondary_conv_count": 0.0,
                "all_conv_count": 0.0, "all_conv_value": 0.0,
            }
        traffic[key]["clicks"]      += row["clicks"]
        traffic[key]["impressions"] += row["impressions"]
        traffic[key]["cost_micros"] += row["cost_micros"]

    def _ensure_unknown_key(key_base):
        """When a conv row has no matching traffic row, attach it to a synthetic
        UNKNOWN-match-type key (preserves original behavior)."""
        key = key_base + ("UNKNOWN",)
        if key not in traffic:
            traffic[key] = {
                "clicks": 0, "impressions": 0, "cost_micros": 0.0,
                "primary_conv_count": 0.0, "primary_conv_value": 0.0,
                "secondary_conv_count": 0.0,
                "all_conv_count": 0.0, "all_conv_value": 0.0,
            }
        return [key]

    # ── Route conversions onto traffic keys ───────────────────────────────────
    if conv_path == "segmented":
        # UNCHANGED routing semantics from v3.
        for row in conv_rows:
            key_base = (row["query_text"], row["ad_group_id"], row["criterion_id"],
                        row["campaign_id"], row["report_date"])
            matching = [k for k in traffic if k[:5] == key_base] or _ensure_unknown_key(key_base)

            action_name = resolve_conversion_action_name(
                row["conversion_action"], conv_cache, ga_service, customer_id
            )
            mode = conv_map.get(action_name, "monitor")

            for k in matching:
                if mode == "primary":
                    traffic[k]["primary_conv_count"] += row["all_conv_count"]
                    traffic[k]["primary_conv_value"] += row["all_conv_value"]
                    traffic[k]["all_conv_count"]     += row["all_conv_count"]
                    traffic[k]["all_conv_value"]     += row["all_conv_value"]
                elif mode == "secondary_count":
                    traffic[k]["secondary_conv_count"] += row["all_conv_count"]
                    traffic[k]["all_conv_count"]       += row["all_conv_count"]
                elif mode == "monitor":
                    traffic[k]["all_conv_count"] += row["all_conv_count"]
                    traffic[k]["all_conv_value"] += row["all_conv_value"]
                # excluded: skip

    elif conv_path == "fallback":
        # Pre-bucketed rows (Google-Home single-primary). No per-action routing.
        for row in conv_rows:
            key_base = (row["query_text"], row["ad_group_id"], row["criterion_id"],
                        row["campaign_id"], row["report_date"])
            matching = [k for k in traffic if k[:5] == key_base] or _ensure_unknown_key(key_base)
            for k in matching:
                traffic[k]["primary_conv_count"]   += row["primary_conv_count"]
                traffic[k]["primary_conv_value"]   += row["primary_conv_value"]
                traffic[k]["secondary_conv_count"] += row["secondary_conv_count"]
                traffic[k]["all_conv_count"]       += row["all_conv_count"]
                traffic[k]["all_conv_value"]       += row["all_conv_value"]

    else:
        raise ValueError(f"Unknown conv_path: {conv_path!r}")

    # Write
    for (query_text, ag_id, crit_id, campaign_id,
         report_date, match_type_served), m in traffic.items():

        instance_id = get_instance_id(cursor, account_id, ag_id, crit_id)
        if instance_id is None:
            counts["skipped"] += 1
            continue

        prev = cursor.execute(
            "SELECT COUNT(*) FROM search_queries WHERE normalized_text = ?",
            (query_text.lower().strip(),)
        ).fetchone()[0]
        query_id = get_or_create_query(cursor, query_text, report_date, "google")
        if prev == 0:
            counts["new_queries"] += 1

        cursor.execute("""
            INSERT INTO search_query_performance
                (query_id, instance_id, campaign_id, platform,
                 report_date, device, match_type_served,
                 impressions, clicks, cost_micros,
                 primary_conv_count, primary_conv_value,
                 secondary_conv_count, all_conv_count, all_conv_value)
            VALUES (?, ?, ?, 'google', ?, 'ALL', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (query_id, instance_id, report_date, device, match_type_served) DO UPDATE SET
                campaign_id          = excluded.campaign_id,
                match_type_served    = excluded.match_type_served,
                impressions          = excluded.impressions,
                clicks               = excluded.clicks,
                cost_micros          = excluded.cost_micros,
                primary_conv_count   = excluded.primary_conv_count,
                primary_conv_value   = excluded.primary_conv_value,
                secondary_conv_count = excluded.secondary_conv_count,
                all_conv_count       = excluded.all_conv_count,
                all_conv_value       = excluded.all_conv_value
        """, (
            query_id, instance_id, campaign_id,
            report_date, match_type_served,
            m["impressions"], m["clicks"], m["cost_micros"],
            m["primary_conv_count"], m["primary_conv_value"],
            m["secondary_conv_count"],
            m["all_conv_count"], m["all_conv_value"],
        ))
        counts["written"] += 1

    conn.commit()
    return counts


# ── Per-campaign sync ─────────────────────────────────────────────────────────

def sync_campaign(ga_service, conn, cursor, account_id: int,
                  customer_id: str, campaign_id: str,
                  chunks: list, conv_map: dict,
                  conv_cache: dict) -> dict:
    """Syncs sqp for one campaign over all date chunks."""
    totals = {"written": 0, "skipped": 0, "new_queries": 0,
              "traffic_rows": 0, "conv_rows": 0}

    for chunk_start, chunk_end in chunks:
        traffic = fetch_query_traffic(ga_service, customer_id, campaign_id,
                                      chunk_start, chunk_end)
        convs, conv_path = fetch_query_conversions(ga_service, customer_id, campaign_id,
                                                   chunk_start, chunk_end)
        counts  = aggregate_and_write(
            conn, cursor, account_id, traffic, convs, conv_path,
            conv_map, conv_cache, ga_service, customer_id,
        )
        totals["traffic_rows"] += len(traffic)
        totals["conv_rows"]    += len(convs)
        totals["written"]      += counts["written"]
        totals["skipped"]      += counts["skipped"]
        totals["new_queries"]  += counts["new_queries"]

        # conv_path tag tells you at a glance whether the segmented primary path
        # held (segmented) or the single-primary fallback engaged (fallback).
        print(f"    {chunk_start} → {chunk_end}  "
              f"traffic={len(traffic):,}  conv={len(convs):,} [{conv_path}]  "
              f"written={counts['written']:,}  "
              f"skipped={counts['skipped']:,}  "
              f"new_queries={counts['new_queries']:,}")

    return totals


# ── Post-sync diagnostic ──────────────────────────────────────────────────────

def print_diagnostic(conn, account_id: int):
    rows = conn.execute("""
        SELECT
            sqp.campaign_id,
            c.campaign_name,
            COUNT(DISTINCT sqp.query_id)    AS unique_queries,
            COUNT(DISTINCT sqp.instance_id) AS kw_slots,
            COUNT(*)                         AS rows,
            MIN(sqp.report_date)             AS min_date,
            MAX(sqp.report_date)             AS max_date,
            SUM(sqp.cost_micros) / 1e6       AS spend,
            SUM(sqp.primary_conv_value)      AS revenue
        FROM search_query_performance sqp
        JOIN keyword_instances ki ON ki.id = sqp.instance_id
        LEFT JOIN campaigns c     ON c.campaign_id = sqp.campaign_id
        WHERE ki.account_id = ?
          AND sqp.device    = 'ALL'
        GROUP BY sqp.campaign_id
        ORDER BY spend DESC
    """, (account_id,)).fetchall()

    # NOTE: this aggregates the ENTIRE table for the account, not just rows
    # written this run. A run that writes 0 rows can still print large totals
    # here (stale table contents). To verify a run actually wrote, check the
    # per-chunk written= counts above and query the specific date range.
    print(f"\n{'─'*95}")
    print(f"  DIAGNOSTIC — search_query_performance (device='ALL')  [whole-table totals]")
    print(f"{'─'*95}")
    if not rows:
        print("  (no data)")
        return
    print(f"  {'Campaign':<45} {'Queries':>8} {'Slots':>6} {'Rows':>7} "
          f"{'Spend':>12} {'Revenue':>11}")
    print(f"  {'─'*45} {'─'*8} {'─'*6} {'─'*7} {'─'*12} {'─'*11}")
    for r in rows:
        name = (r["campaign_name"] or r["campaign_id"] or "")[:44]
        print(f"  {name:<45} "
              f"{r['unique_queries']:>8,} "
              f"{r['kw_slots']:>6,} "
              f"{r['rows']:>7,} "
              f"${r['spend']:>11,.2f} "
              f"${r['revenue']:>10,.2f}")

    total_queries = conn.execute(
        "SELECT COUNT(DISTINCT query_id) FROM search_query_performance sqp "
        "JOIN keyword_instances ki ON ki.id = sqp.instance_id "
        "WHERE ki.account_id = ?", (account_id,)
    ).fetchone()[0]
    print(f"\n  Total unique queries (this account): {total_queries:,}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    cfg  = ACCOUNT_MAP[args.account]

    customer_id = os.getenv(cfg["customer_id_env"])
    if not customer_id:
        raise ValueError(f"{cfg['customer_id_env']} not set in .env")

    login_cid = (os.getenv(cfg["login_customer_id_env"])
                 if cfg["login_customer_id_env"] else None)

    conn   = get_db_connection()
    cursor = conn.cursor()
    account_id = get_account_id(cursor, customer_id)

    # Pre-flight: confirm the DB is writable BEFORE spending minutes on API calls.
    # If another process (e.g. a Ctrl-C'd run still holding the lock) has the DB,
    # fail fast with a clear message instead of hanging at the first commit.
    try:
        cursor.execute("BEGIN IMMEDIATE")
        conn.rollback()
    except sqlite3.OperationalError as e:
        print(f"\n  [LOCKED] Cannot get a write lock on paid_search.db: {e}")
        print("  Another process is holding the database — almost certainly a")
        print("  lingering python.exe from an interrupted run. Resolve it first:")
        print("    1) close that terminal / `taskkill /F /IM python.exe`")
        print("    2) `python db_unlock_check.py` until it reports [OK]")
        print("  then re-run this command. Nothing was written.")
        conn.close()
        sys.exit(1)
    # Start the long run with a compact WAL.
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError:
        pass

    # Resolve date range
    yesterday = date.today() - timedelta(days=1)
    if args.start:
        start_date = date.fromisoformat(args.start)
        end_date   = date.fromisoformat(args.end) if args.end else yesterday
        mode_desc  = f"range: {start_date} → {end_date}"
    elif args.refresh:
        end_date   = yesterday
        start_date = yesterday - timedelta(days=args.refresh - 1)
        mode_desc  = f"refresh: last {args.refresh} days"
    else:  # --incremental
        last_date = get_last_synced_date(conn, account_id)
        end_date  = yesterday
        if last_date:
            start_date = last_date - timedelta(days=INCREMENTAL_DAYS)
            mode_desc  = (f"incremental: last synced {last_date}, "
                          f"pulling from {start_date} ({INCREMENTAL_DAYS}-day overlap)")
        else:
            start_date = yesterday - timedelta(days=DEFAULT_DAYS - 1)
            mode_desc  = f"incremental (no existing data): last {DEFAULT_DAYS} days"

    chunks = get_monthly_chunks(start_date, end_date)

    conv_map = load_conversion_action_map(cursor, account_id)

    print("=" * 65)
    print("05_sync_search_queries.py  [v7]")
    print(f"  Account  : {cfg['display_name']} ({customer_id})")
    print(f"  Mode     : {mode_desc}")
    print(f"  Range    : {start_date} → {end_date} "
          f"({(end_date - start_date).days + 1} days, {len(chunks)} chunk(s))")
    print("=" * 65)
    print(f"\n  accounts.id = {account_id}")
    print(f"\n  Conversion action map ({len(conv_map)} actions):")
    for name, mode in sorted(conv_map.items()):
        print(f"    [{mode:<16}]  {name}")

    print("\n  Connecting to Google Ads API...")
    client     = get_google_ads_client(login_cid)
    ga_service = client.get_service("GoogleAdsService")
    print("  Connected.")

    # Campaign list
    if args.campaign_id:
        campaigns = [{"campaign_id": args.campaign_id,
                      "campaign_name": f"(specified: {args.campaign_id})",
                      "status": "UNKNOWN"}]
        print(f"\n  Mode: single campaign ({args.campaign_id}) — probe bypassed")
    else:
        print(f"\n  Probing for campaigns with traffic in "
              f"{start_date} → {end_date} ...")
        campaigns = discover_active_campaigns(
            ga_service, customer_id,
            start_date.isoformat(), end_date.isoformat(),
        )
        enabled = sum(1 for c in campaigns if c["status"] == "ENABLED")
        paused  = sum(1 for c in campaigns if c["status"] == "PAUSED")
        print(f"  {len(campaigns):,} campaign(s) served in window "
              f"({enabled} ENABLED, {paused} PAUSED-but-active-in-window) "
              f"— skipping all others")

    conv_cache   = {}
    grand_totals = {"written": 0, "skipped": 0, "new_queries": 0,
                    "traffic_rows": 0, "conv_rows": 0}
    results  = []
    t_start  = time.time()

    for i, camp in enumerate(campaigns, 1):
        cid    = camp["campaign_id"]
        cname  = camp["campaign_name"]
        status = camp["status"]
        print(f"\n  [{i}/{len(campaigns)}] [{status}] {cname} ({cid})")

        t0 = time.time()
        try:
            totals = sync_campaign(
                ga_service, conn, cursor, account_id,
                customer_id, cid, chunks, conv_map, conv_cache,
            )
        except GoogleAdsException as ex:
            _print_api_error(f"campaign {cid}", ex)
            results.append({"name": cname, "id": cid, "ok": False, "elapsed": 0})
            continue

        elapsed = time.time() - t0
        for k in grand_totals:
            grand_totals[k] += totals[k]
        results.append({"name": cname, "id": cid, "ok": True,
                         "written": totals["written"],
                         "skipped": totals["skipped"],
                         "elapsed": elapsed})
        print(f"    ✓ written={totals['written']:,}  "
              f"skipped={totals['skipped']:,}  "
              f"new_queries={totals['new_queries']:,}  "
              f"({elapsed:.0f}s)")

    total_elapsed = time.time() - t_start

    # Summary
    print(f"\n{'='*65}")
    print("SYNC COMPLETE")
    print(f"{'='*65}")
    print(f"  Campaigns processed  : {len(campaigns):,}")
    print(f"  Traffic rows (API)   : {grand_totals['traffic_rows']:,}")
    print(f"  Conversion rows      : {grand_totals['conv_rows']:,}")
    print(f"  sqp rows written     : {grand_totals['written']:,}")
    print(f"  sqp rows skipped     : {grand_totals['skipped']:,}")
    print(f"  New query identities : {grand_totals['new_queries']:,}")
    print(f"  (skipped = no matching row in keyword_instances)")
    print(f"  Total time           : {total_elapsed/60:.1f} min")

    failures = [r for r in results if not r["ok"]]
    if failures:
        print(f"\n  ⚠ {len(failures)} campaign(s) failed:")
        for f in failures:
            print(f"    {f['name']} ({f['id']})")

    print_diagnostic(conn, account_id)

    conn.close()
    print(f"\nNext step:")
    print(f"  python kw_db_scripts/08_tag_search_queries.py --untagged")


if __name__ == "__main__":
    main()
