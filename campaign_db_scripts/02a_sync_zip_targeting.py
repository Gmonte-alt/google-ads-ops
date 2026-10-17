"""
02a_sync_zip_targeting.py  [v1 — diff-then-reload, gate-ratified 2026-10-07]
=========================
Term Life Zip-Targeting Subsystem — Quote.com Google Ads
Folder: C:\\Projects\\google-ads-ops\\campaign_db_scripts\\

Structure sync (sub-bullet of the 02 structure syncs — Gilbert ruling
2026-10-06: daily, in the structure block, immediately after
`02_sync_campaigns_adgroups.py --account term_life`). Structure-sync scope
always covers performance-sync scope; 02a before any future zip-perf step.

WHAT IT DOES, per account:
  1. Pulls ALL non-removed LOCATION campaign_criterion rows from the API.
  2. Resolves geo constants LOCAL-FIRST (geo_target_constants, ~98.9% hit),
     API fallback in chunks for misses; unresolvable ids are STORED with
     NULL geo fields and counted loudly (never dropped — they are live
     criteria referencing removed constants).
  3. Validates postal rows: zip = constant name, must match ^\\d{5}$ —
     failures are SKIPPED and counted loudly (hard rule: 5-digit TEXT,
     leading zeros preserved). Non-US postal rows (e.g. the 8 DE zips)
     are STORED with country_code (ratified — hygiene findings).
  4. DIFF-BEFORE-DELETE (architect amendment, 2026-10-07): compares incoming
     vs current campaign_zip_targeting and writes ADDED / REMOVED /
     STATUS_CHANGED rows to zip_targeting_transitions — the system of record
     for targeting history. --plan-label stamps the Layer-2 annotation
     (ZIPMOVE-YYYYMMDD convention) onto this run's transition rows.
     BASELINE RULE: if the account has NO current rows, this run is day zero —
     the full load is a baseline, NOT 126K ADDED transitions; nothing is
     written to the transitions table and the baseline date prints loudly.
  5. Deletes the account's rows and reloads incoming, in ONE transaction
     (rollback on fatal error restores pre-run state). Inserts are
     pre-validated; on a batch failure the load falls back to row-by-row
     skip-and-count — never an all-or-nothing load where one bad row
     zeroes everything.

ACCEPTANCE (first real run vs probe of 2026-10-07):
  rows loaded ≈ 126,151 · postal+ 47,957 · postal− 63,811 ·
  non-US stored = 8 · unresolved stored-with-NULL = 9 · skipped = 0 ·
  BASELINE banner printed · v_zip_targeting_map ≈ 9,582 rows (CR12+CR3).

RUN:
  conda activate paid-search

  # dry run first — pulls + diffs, writes NOTHING:
  python campaign_db_scripts\\02a_sync_zip_targeting.py --account term_life --dry-run

  # real run:
  python campaign_db_scripts\\02a_sync_zip_targeting.py --account term_life

  # deliberate move plan (Layer-2 annotation on this run's transitions):
  python campaign_db_scripts\\02a_sync_zip_targeting.py --account term_life --plan-label ZIPMOVE-20261015

DAILY ROUTINE LINE (structure block, after 02 term_life):
  python campaign_db_scripts\\02a_sync_zip_targeting.py --account term_life > daily_2a_zip_targeting.txt 2>&1
"""

import re
import sqlite3
import os
import argparse
import logging
from pathlib import Path
from collections import defaultdict
from datetime import date, datetime, timezone
from dotenv import load_dotenv

# ── Logging — attach handler before Google Ads client import ──────────────────
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter(
    "%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S"
))
logging.root.setLevel(logging.INFO)
logging.root.addHandler(_handler)
log = logging.getLogger(__name__)

from google.ads.googleads.client import GoogleAdsClient  # noqa: E402 — after logging setup

# ── Configuration ─────────────────────────────────────────────────────────────
load_dotenv()

DB_PATH   = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
MCC_ID    = os.getenv("GOOGLE_ADS_MCC_ID")
ZIP_RE    = re.compile(r"^\d{5}$")
GEO_CHUNK = 1000


# ── Database ──────────────────────────────────────────────────────────────────

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


def get_account_internal_id(conn, platform_account_id: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM accounts WHERE platform_account_id = ? AND platform = 'google'",
        (platform_account_id,),
    ).fetchone()
    return row["id"] if row else None


# ── Account discovery (same pattern as 02/03) ─────────────────────────────────

def discover_accounts(account_filter: str | None = None) -> list[dict]:
    accounts = []
    prefix   = "GOOGLE_ADS_CUSTOMER_ID_"
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        suffix    = key[len(prefix):]
        cid       = value.strip()
        login_key = f"GOOGLE_ADS_LOGIN_CUSTOMER_ID_{suffix}"
        login_id  = os.getenv(login_key, "").strip()
        accounts.append({
            "customer_id":       cid,
            "vertical":          suffix.lower(),
            "login_customer_id": login_id if login_id else MCC_ID,
            "is_standalone":     bool(login_id),
        })
    accounts.sort(key=lambda a: a["vertical"])
    if account_filter:
        wanted = account_filter.lower()
        accounts = [a for a in accounts if a["vertical"] == wanted]
    return accounts


def get_google_ads_client(login_customer_id: str) -> GoogleAdsClient:
    client = GoogleAdsClient.load_from_storage()
    client.login_customer_id = login_customer_id
    return client


# ── Helpers ───────────────────────────────────────────────────────────────────

def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resource_tail(resource_name: str) -> str | None:
    if not resource_name:
        return None
    parts = str(resource_name).split("/")
    return parts[-1] if parts else None


# ── API pulls ─────────────────────────────────────────────────────────────────

CRITERION_QUERY = """
    SELECT
        campaign.id,
        campaign.name,
        campaign.status,
        campaign_criterion.criterion_id,
        campaign_criterion.negative,
        campaign_criterion.bid_modifier,
        campaign_criterion.status,
        campaign_criterion.location.geo_target_constant
    FROM campaign_criterion
    WHERE campaign_criterion.type = 'LOCATION'
      AND campaign_criterion.status != 'REMOVED'
      AND campaign.status != 'REMOVED'
    ORDER BY campaign.id
"""

GEO_CONSTANT_QUERY_TEMPLATE = """
    SELECT
        geo_target_constant.id,
        geo_target_constant.name,
        geo_target_constant.country_code,
        geo_target_constant.target_type
    FROM geo_target_constant
    WHERE geo_target_constant.id IN ({ids})
"""


def pull_location_criteria(client, ga_service, customer_id: str) -> list[dict]:
    request = client.get_type("SearchGoogleAdsRequest")
    request.customer_id = customer_id
    request.query = CRITERION_QUERY
    criteria = []
    for row in ga_service.search(request=request):
        c  = row.campaign
        cc = row.campaign_criterion
        geo_id = resource_tail(cc.location.geo_target_constant)
        criteria.append({
            "campaign_id":      str(c.id),
            "campaign_name":    c.name,
            "criterion_id":     str(cc.criterion_id),
            "is_negative":      1 if cc.negative else 0,
            "bid_modifier":     float(cc.bid_modifier) if cc.bid_modifier else None,
            "criterion_status": cc.status.name if cc.status else None,
            "geo_id":           int(geo_id) if geo_id and geo_id.isdigit() else None,
        })
    return criteria


def resolve_geo_constants(conn, client, ga_service, customer_id: str,
                          geo_ids: set[int]) -> tuple[dict[int, dict], int, int]:
    """
    Local-first resolution: geo_target_constants table, then API fallback
    for misses in chunks. Returns (details, local_hits, api_hits).
    """
    details: dict[int, dict] = {}
    if geo_ids:
        # local pass — chunked IN lists against the local table
        ids = sorted(geo_ids)
        for i in range(0, len(ids), 900):
            batch = ids[i:i + 900]
            ph = ",".join("?" * len(batch))
            for r in conn.execute(
                f"SELECT criterion_id, name, target_type, country_code "
                f"FROM geo_target_constants WHERE criterion_id IN ({ph})", batch):
                details[r["criterion_id"]] = {
                    "name":         r["name"],
                    "target_type":  r["target_type"],
                    "country_code": r["country_code"],
                }
    local_hits = len(details)

    misses = sorted(geo_ids - set(details.keys()))
    for i in range(0, len(misses), GEO_CHUNK):
        batch = misses[i:i + GEO_CHUNK]
        request = client.get_type("SearchGoogleAdsRequest")
        request.customer_id = customer_id
        request.query = GEO_CONSTANT_QUERY_TEMPLATE.format(
            ids=", ".join(str(x) for x in batch))
        for row in ga_service.search(request=request):
            g = row.geo_target_constant
            details[int(g.id)] = {
                "name":         g.name,
                "target_type":  g.target_type,
                "country_code": g.country_code,
            }
    api_hits = len(details) - local_hits
    return details, local_hits, api_hits


# ── Row building + validation (skip-and-count) ────────────────────────────────

def build_rows(criteria: list[dict], geo: dict[int, dict],
               account_id: int) -> tuple[list[tuple], dict]:
    """
    Builds insert tuples for campaign_zip_targeting. Pre-validates:
      - postal rows with a name failing ^\\d{5}$ → SKIPPED (counted loudly)
      - non-US postal rows → STORED with country_code (counted)
      - unresolvable geo ids → STORED with NULL geo fields (counted)
    Returns (rows, counters).
    """
    ts = now_utc()
    rows = []
    cnt = defaultdict(int)
    skipped_detail = []

    for cr in criteria:
        g = geo.get(cr["geo_id"]) if cr["geo_id"] is not None else None
        zip_val, geo_type, geo_name, country = None, None, None, None

        if g is None:
            cnt["unresolved_stored"] += 1
        else:
            geo_type = g["target_type"]
            geo_name = g["name"]
            country  = g["country_code"]
            if "postal" in (geo_type or "").lower():
                if not ZIP_RE.match(g["name"] or ""):
                    cnt["skipped_invalid_zip"] += 1
                    skipped_detail.append(
                        (cr["campaign_id"], cr["criterion_id"], g["name"]))
                    continue
                zip_val = g["name"]
                if country != "US":
                    cnt["non_us_stored"] += 1
                if cr["is_negative"]:
                    cnt["postal_neg"] += 1
                else:
                    cnt["postal_pos"] += 1

        rows.append((
            account_id,
            cr["campaign_id"],
            cr["criterion_id"],
            zip_val,
            cr["geo_id"],
            geo_type,
            geo_name,
            country,
            cr["is_negative"],
            cr["bid_modifier"],
            cr["criterion_status"],
            ts,
        ))

    cnt["built"] = len(rows)
    if skipped_detail:
        print(f"  SKIPPED (invalid zip name, ^\\d{{5}}$ failed) — "
              f"{len(skipped_detail)} rows:")
        for d in skipped_detail[:20]:
            print(f"    campaign {d[0]} criterion {d[1]} name={d[2]!r}")
    return rows, cnt


# ── Diff-before-delete ────────────────────────────────────────────────────────

INSERT_SQL = """
    INSERT OR REPLACE INTO campaign_zip_targeting (
        account_id, campaign_id, criterion_id, zip,
        geo_target_constant_id, geo_type, geo_name, country_code,
        is_negative, bid_modifier, status, last_synced_at
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
"""

TRANSITION_SQL = """
    INSERT OR REPLACE INTO zip_targeting_transitions (
        account_id, observed_date, campaign_id, criterion_id,
        zip, geo_type, is_negative, change_type,
        prev_status, new_status, plan_label
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
"""


def diff_transitions(conn, account_id: int, rows: list[tuple],
                     plan_label: str | None) -> tuple[list[tuple], bool]:
    """
    Compares incoming rows against current campaign_zip_targeting for this
    account. Returns (transition_tuples, is_baseline).
    Key = (campaign_id, criterion_id); STATUS_CHANGED compares criterion status.
    """
    today = date.today().isoformat()

    current = {}
    for r in conn.execute("""
        SELECT campaign_id, criterion_id, zip, geo_type, is_negative, status
        FROM campaign_zip_targeting WHERE account_id = ?
    """, (account_id,)):
        current[(r["campaign_id"], r["criterion_id"])] = r

    if not current:
        return [], True   # BASELINE — day zero, no transitions recorded

    # incoming tuple layout: see INSERT_SQL ordering
    incoming = {(r[1], r[2]): r for r in rows}

    transitions = []
    for key, r in incoming.items():
        zip_val, geo_type, is_neg, status = r[3], r[5], r[8], r[10]
        cur = current.get(key)
        if cur is None:
            transitions.append((account_id, today, key[0], key[1],
                                zip_val, geo_type, is_neg, "ADDED",
                                None, status, plan_label))
        elif (cur["status"] or None) != (status or None):
            transitions.append((account_id, today, key[0], key[1],
                                zip_val, geo_type, is_neg, "STATUS_CHANGED",
                                cur["status"], status, plan_label))

    for key, cur in current.items():
        if key not in incoming:
            transitions.append((account_id, today, key[0], key[1],
                                cur["zip"], cur["geo_type"], cur["is_negative"],
                                "REMOVED", cur["status"], None, plan_label))

    return transitions, False


def reload_account(conn, account_id: int, rows: list[tuple],
                   transitions: list[tuple]) -> tuple[int, int]:
    """
    One transaction: write transitions, delete account rows, reload incoming.
    Rollback on fatal error restores pre-run state. Batch insert falls back
    to row-by-row skip-and-count on failure — never all-or-nothing.
    Returns (rows_loaded, rows_failed).
    """
    failed = 0
    cur = conn.cursor()
    try:
        cur.execute("BEGIN")
        if transitions:
            cur.executemany(TRANSITION_SQL, transitions)
        cur.execute("DELETE FROM campaign_zip_targeting WHERE account_id = ?",
                    (account_id,))
        try:
            cur.executemany(INSERT_SQL, rows)
            loaded = len(rows)
        except Exception as batch_err:
            print(f"  WARNING: batch insert failed ({batch_err}) — "
                  f"falling back to row-by-row skip-and-count.")
            loaded = 0
            for r in rows:
                try:
                    cur.execute(INSERT_SQL, r)
                    loaded += 1
                except Exception as row_err:
                    failed += 1
                    if failed <= 20:
                        print(f"    ROW FAILED: campaign {r[1]} "
                              f"criterion {r[2]}: {row_err}")
        conn.commit()
        return loaded, failed
    except Exception:
        conn.rollback()
        raise


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Sync LOCATION campaign criteria (zip targeting) into "
                    "campaign_zip_targeting, with diff-before-delete history "
                    "capture into zip_targeting_transitions.")
    parser.add_argument("--account", default="term_life", metavar="VERTICAL",
                        help="Vertical label to sync (default: term_life — "
                             "the only account with zip targeting today; "
                             "schema is account-generic).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Pull + diff + print everything; write NOTHING.")
    parser.add_argument("--plan-label", default=None, metavar="LABEL",
                        help="Layer-2 annotation stamped on this run's "
                             "transition rows (e.g. ZIPMOVE-20261015). "
                             "Unlabeled transitions = undocumented edits.")
    args = parser.parse_args()

    print("=" * 65)
    print("02a_sync_zip_targeting.py  [v1 — diff-then-reload]")
    print(f"Database : {DB_PATH}")
    print(f"Started  : {now_utc()}")
    print(f"Account  : {args.account}")
    if args.dry_run:
        print("Mode     : DRY RUN — no writes")
    if args.plan_label:
        print(f"Plan     : {args.plan_label}")
    print("=" * 65)

    accounts = discover_accounts(account_filter=args.account)
    if not accounts:
        raise SystemExit(f"[ABORT] no GOOGLE_ADS_CUSTOMER_ID_"
                         f"{args.account.upper()} in .env")
    account = accounts[0]
    cid = account["customer_id"]
    print(f"\nCustomer : {cid}  login={account['login_customer_id']}")

    conn = get_db_connection()
    account_id = get_account_internal_id(conn, cid)
    if account_id is None:
        raise SystemExit(f"[ABORT] accounts.id not found for {cid} — "
                         f"run 02_sync_campaigns_adgroups.py first "
                         f"(structure scope must cover this sync).")
    print(f"accounts.id = {account_id}")

    client     = get_google_ads_client(account["login_customer_id"])
    ga_service = client.get_service("GoogleAdsService")

    # ── Pull + resolve ─────────────────────────────────────────────────────
    print("\nPulling LOCATION criteria...", flush=True)
    criteria = pull_location_criteria(client, ga_service, cid)
    print(f"  criteria pulled: {len(criteria):,}")

    geo_ids = {c["geo_id"] for c in criteria if c["geo_id"] is not None}
    geo, local_hits, api_hits = resolve_geo_constants(
        conn, client, ga_service, cid, geo_ids)
    print(f"  geo ids resolved: {len(geo):,} / {len(geo_ids):,} "
          f"(local {local_hits:,}, API fallback {api_hits:,}, "
          f"unresolved {len(geo_ids) - len(geo):,})")

    rows, cnt = build_rows(criteria, geo, account_id)

    # ── Diff ───────────────────────────────────────────────────────────────
    transitions, is_baseline = diff_transitions(conn, account_id, rows,
                                                args.plan_label)
    if is_baseline:
        print(f"\n  ████ BASELINE LOAD — day zero of zip-targeting history "
              f"is {date.today().isoformat()} ████")
        print(f"  No transitions recorded for the initial load; as-of "
              f"reconstruction begins today.")
    else:
        by_type = defaultdict(int)
        for t in transitions:
            by_type[t[7]] += 1
        print(f"\n  Transitions detected: {len(transitions):,} "
              f"({dict(by_type) if by_type else 'none'})")
        for t in transitions[:20]:
            print(f"    {t[7]:<15} campaign {t[2]} zip={t[4]} "
                  f"neg={t[6]} {t[8]}→{t[9]}")
        if len(transitions) > 20:
            print(f"    ... and {len(transitions) - 20:,} more")

    # ── Write or dry-run report ────────────────────────────────────────────
    loaded, failed = 0, 0
    if args.dry_run:
        print(f"\n  [DRY RUN] would load {len(rows):,} rows and write "
              f"{0 if is_baseline else len(transitions):,} transitions. "
              f"Nothing written.")
    else:
        loaded, failed = reload_account(conn, account_id, rows, transitions)

    # ── Summary ────────────────────────────────────────────────────────────
    print(f"\n{'=' * 65}\nSYNC COMPLETE\n{'=' * 65}")
    print(f"  Criteria pulled            : {len(criteria):,}")
    print(f"  Rows built                 : {cnt['built']:,}")
    print(f"  Positive postal (zip)      : {cnt['postal_pos']:,}")
    print(f"  Negative postal            : {cnt['postal_neg']:,}")
    print(f"  Non-US postal STORED       : {cnt['non_us_stored']:,}")
    print(f"  Unresolved geo STORED/NULL : {cnt['unresolved_stored']:,}")
    print(f"  Skipped invalid zip names  : {cnt['skipped_invalid_zip']:,}")
    if not args.dry_run:
        print(f"  Rows loaded                : {loaded:,}"
              + (f"  (FAILED: {failed:,})" if failed else ""))
        print(f"  Transitions written        : "
              f"{0 if is_baseline else len(transitions):,}"
              + ("  [BASELINE — none by design]" if is_baseline else ""))
    if failed:
        print(f"  WARNING: {failed:,} rows failed to load — review above.")

    conn.close()
    print(f"\nDone. {now_utc()}")


if __name__ == "__main__":
    main()
