"""
18_pull_google_change_events_api.py  (Google change-history, Phase 1 -> API layer)

Pipeline step 18.
CADENCE: RECURRING -- the going-forward weekly feed. Pulls change_event from the
Google Ads API into the Model-A ledger google_change_event_raw (idempotent on the
event's own unique resource_name, so a few-days overlap per run no-ops cleanly).
Replaces the weekly UI-CSV load for the three tracked types; the one-time YTD CSV
seed stays as pre-API history. After this runs: 16 (name-history) -> 17 (events).

DEPENDENCY NOTE: unlike every other script here, this one is NOT pure stdlib --
it needs the google-ads client library + python-dotenv (both already in the env).
Auth mirrors syncs 03/05 exactly: load_dotenv() reads .env (which holds
GOOGLE_ADS_CONFIGURATION_FILE_PATH ->
C:\\ProgramData\\google-ads-credentials\\google-ads.yaml), then bare
load_from_storage() picks up that env var. RUN FROM THE PROJECT ROOT so .env is
found. Pinned to API v23 to match those syncs and the enum-tolerance assumption.
The DB writes are still guarded (busy_timeout, foreign_keys, one writer),
dry-run by default.

GAP DETECTION: the API only reaches back 30 days. If the newest event already in
the ledger is older than today-30d, there is an UNRECOVERABLE hole -- this warns
loudly rather than silently pulling only the last 30 days.

Usage (run from the project root C:\\Projects\\google-ads-ops so .env is found):
    python campaign_db_scripts\\18_pull_google_change_events_api.py --db PATH        # dry-run (calls the API, no DB write)
    python campaign_db_scripts\\18_pull_google_change_events_api.py --db PATH --apply
    python ...\\18_pull_google_change_events_api.py --db PATH --google-ads-yaml C:\\path\\google-ads.yaml --apply   # explicit override
"""

import argparse
import collections
import datetime
import json
import sys
import campaign_db_scripts.change_history.gads_change_common as C
from dotenv import load_dotenv

load_dotenv()  # read .env -> GOOGLE_ADS_CONFIGURATION_FILE_PATH, exactly like syncs 03/05

API_VERSION = "v23"
OVERLAP_DAYS = 3            # re-pull a few days each run, like the sqp overlap
MAX_LOOKBACK_DAYS = 28      # API rejects start dates OLDER than 30 days (START_DATE_TOO_OLD).
                           # Date-only start = midnight, so exactly-30 is already >30 in absolute
                           # time; stay 2 days inside to clear the midnight + account-timezone edge.
                           # No history lost: the CSV YTD seed covers everything before the seam.
PAGE_LIMIT = 10000          # API hard cap; required LIMIT clause

GAQL = """
SELECT change_event.resource_name, change_event.change_date_time,
       change_event.change_resource_name, change_event.user_email,
       change_event.client_type, change_event.change_resource_type,
       change_event.old_resource, change_event.new_resource,
       change_event.resource_change_operation, change_event.changed_fields
FROM change_event
WHERE change_event.change_date_time >= '%s' AND change_event.change_date_time <= '%s'
ORDER BY change_event.change_date_time DESC
LIMIT %d
"""


def extract_changes_from_event(event, get_nested_attr):
    """Pure extraction: change_event row -> a ledger record dict.
    Keyed off change_resource_type (NOT which sub-resource is populated). Tolerates
    the known enum-vs-int return for resource types newer than the API version
    (unknown -> str, which normalize_api_record treats as not-tracked). Reads each
    changed_fields path off old_resource/new_resource (the ChangedResource oneof)."""
    crt = event.change_resource_type
    try:
        crt_name = crt.name                       # normal enum
    except AttributeError:
        crt_name = str(crt)                        # unknown int (newer type) -> not tracked
    try:
        op = event.resource_change_operation.name
    except AttributeError:
        op = str(event.resource_change_operation)

    changes = {}
    for path in event.changed_fields.paths:
        old = new = None
        try:
            old = get_nested_attr(event.old_resource, path)
        except Exception:
            old = None
        try:
            new = get_nested_attr(event.new_resource, path)
        except Exception:
            new = None
        changes[path] = (_jsonable(old), _jsonable(new))

    dt = str(event.change_date_time)[:19].replace("T", " ")
    return {
        "resource_name": event.resource_name,
        "change_datetime": dt,
        "change_resource_type": crt_name,
        "resource_operation": op,
        "change_resource_name": event.change_resource_name,
        "user_email": event.user_email or None,
        "client_type": getattr(event.client_type, "name", str(event.client_type)),
        "changed_fields": list(event.changed_fields.paths),
        "changes_json": changes,
    }


def _jsonable(v):
    if v is None:
        return None
    if isinstance(v, (str, int, float, bool)):
        return v
    return str(v)  # enums / messages -> string


def compute_window(conn, account_id, today):
    """Window to request: overlap the last pull, capped to the 30-day API limit.
    Also returns a gap warning if the newest ledger event predates the window
    floor (an unrecoverable hole)."""
    floor = today - datetime.timedelta(days=MAX_LOOKBACK_DAYS)
    row = conn.execute(
        "SELECT MAX(change_datetime) m FROM google_change_event_raw WHERE account_id=?",
        (account_id,)).fetchone()
    last = row["m"] if row else None
    warn = None
    if last:
        last_d = datetime.date.fromisoformat(last[:10])
        start = max(floor, last_d - datetime.timedelta(days=OVERLAP_DAYS))
        if last_d < floor:
            gap = (floor - last_d).days
            warn = ("last ledger event %s is %d days before the 30-day API floor "
                    "-- UNRECOVERABLE GAP; the UI grid is the only way to backfill it" % (last, gap))
    else:
        start = floor  # first pull
    return start.isoformat(), (today + datetime.timedelta(days=1)).isoformat(), warn


def pull_events(args, conn, account_id):
    """Calls the Google Ads API (operator-validated seam). Returns ledger records."""
    from google.ads.googleads.client import GoogleAdsClient
    from google.ads.googleads.util import get_nested_attr

    yaml_path = args.google_ads_yaml
    if yaml_path:
        print("[ok] loading google-ads config from %s (explicit override)" % yaml_path)
        client = GoogleAdsClient.load_from_storage(yaml_path, version=args.api_version)
    else:
        print("[ok] loading google-ads config from .env (GOOGLE_ADS_CONFIGURATION_FILE_PATH), like 03/05")
        client = GoogleAdsClient.load_from_storage(version=args.api_version)
    svc = client.get_service("GoogleAdsService")
    cid_digits = args.cid.replace("-", "")

    today = datetime.date.today()
    start, end, gap_warn = compute_window(conn, account_id, today)
    if gap_warn:
        print("[WARN] %s" % gap_warn)
    print("[ok] pulling change_event window %s .. %s (overlap %dd)" % (start, end, OVERLAP_DAYS))

    records, cursor_end = [], end
    for _ in range(6):  # paginate by re-anchoring; >6 pages of 10k is implausible weekly
        q = GAQL % (start, cursor_end, PAGE_LIMIT)
        page = list(svc.search(customer_id=cid_digits, query=q))
        for r in page:
            records.append(extract_changes_from_event(r.change_event, get_nested_attr))
        if len(page) < PAGE_LIMIT:
            break
        cursor_end = str(page[-1].change_event.change_date_time)[:19].replace("T", " ")
        print("[ok] hit page limit; re-anchoring before %s" % cursor_end)
    return records


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--cid", default=C.DEFAULT_CID)
    ap.add_argument("--google-ads-yaml", default=None,
                    help="optional explicit path; default reads .env via load_dotenv() like 03/05")
    ap.add_argument("--api-version", default=API_VERSION)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = C.connect(args.db, write=args.apply)
    try:
        acct = C.resolve_account_id(conn, args.cid)
        records = pull_events(args, conn, acct)
        print("[ok] %d change_event rows pulled" % len(records))
        if records:
            TRACKED = {"CAMPAIGN", "CAMPAIGN_BUDGET", "BIDDING_STRATEGY"}
            by_type = collections.Counter(r["change_resource_type"] for r in records)
            print("[preview] by resource type: %s" % dict(by_type))
            n_tracked_type = sum(1 for r in records if r["change_resource_type"] in TRACKED)
            print("[preview] %d of %d rows are tracked resource types (CAMPAIGN/CAMPAIGN_BUDGET/"
                  "BIDDING_STRATEGY); 17 further narrows to name/tROAS/budget fields" % (n_tracked_type, len(records)))
            for r in sorted(records, key=lambda x: x["change_datetime"])[:12]:
                paths = ",".join(r["changes_json"].keys()) or "(no changed_fields)"
                mark = "" if r["change_resource_type"] in TRACKED else "   <- not tracked, 17 skips"
                print("   %s  %-17s %-7s %s%s" % (
                    r["change_datetime"][:10], r["change_resource_type"], r["resource_operation"], paths, mark))

        loaded_at = C.now_iso()
        prepared = [(acct, r["resource_name"], r["change_datetime"], r["change_resource_type"],
                     r["resource_operation"], r["change_resource_name"], r["user_email"],
                     r["client_type"], json.dumps(r["changed_fields"]),
                     json.dumps(r["changes_json"]), loaded_at) for r in records]

        existing = {x[0] for x in conn.execute(
            "SELECT resource_name FROM google_change_event_raw WHERE account_id=?", (acct,))}
        new = [p for p in prepared if p[1] not in existing]
        print("[ok] %d new | %d already in ledger (idempotent skip on resource_name)" % (
            len(new), len(prepared) - len(new)))

        if args.apply:
            conn.executemany(
                "INSERT OR IGNORE INTO google_change_event_raw "
                "(account_id, resource_name, change_datetime, change_resource_type, resource_operation, "
                " change_resource_name, user_email, client_type, changed_fields, changes_json, loaded_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)", prepared)
            conn.execute("COMMIT")
            total = conn.execute("SELECT COUNT(*) FROM google_change_event_raw WHERE account_id=?", (acct,)).fetchone()[0]
            print("[apply] inserted %d new rows | API ledger now holds %d rows" % (len(new), total))
        else:
            print("\nDry run only (the API was queried; nothing written). Re-run with --apply.")
    except Exception:
        if args.apply:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
