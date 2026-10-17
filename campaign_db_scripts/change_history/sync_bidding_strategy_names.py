"""
sync_bidding_strategy_names.py -- one small API sync: portfolio bidding-strategy id + NAME
per account, into `bidding_strategies`.

Why: Google's UI change grid logs portfolio-strategy changes account-level (blank campaign
column) identified only by the strategy's quoted DISPLAY NAME. 17 resolves those names to
campaigns via real assignment data (name -> id -> campaigns.bidding_strategy_id) -- but the
DB never stored strategy names. This sync supplies them; 17 auto-detects the table
(name+id+account_id) with no further changes.

Config exactly like 18/03/05: run from the repo root so .env loads
(GOOGLE_ADS_CONFIGURATION_FILE_PATH -> google-ads.yaml). Dry-run by default.

    cd C:\\Projects\\google-ads-ops
    python campaign_db_scripts\\change_history\\sync_bidding_strategy_names.py --db <db> --cid 167-115-5176
    python campaign_db_scripts\\change_history\\sync_bidding_strategy_names.py --db <db> --cid 167-115-5176 --apply
"""
import argparse
import datetime
import os
import sqlite3
import sys

DEFAULT_CID = "888-650-2147"

DDL = """CREATE TABLE IF NOT EXISTS bidding_strategies (
    account_id INTEGER NOT NULL,
    bidding_strategy_id NOT NULL,  -- no affinity: stored in campaigns' id type
    strategy_name TEXT,
    strategy_type TEXT,
    status TEXT,
    owner TEXT,                    -- 'account' | 'mcc'
    owner_customer_id TEXT,
    owner_descriptive_name TEXT,
    last_synced_at TEXT,
    PRIMARY KEY (account_id, bidding_strategy_id)
)"""

MIGRATE_COLS = [("owner", "TEXT"), ("owner_customer_id", "TEXT"), ("owner_descriptive_name", "TEXT")]


def resolve_account_id(conn, cid):
    digits = (cid or "").replace("-", "")
    row = conn.execute(
        "SELECT id FROM accounts WHERE platform='google' "
        "AND replace(platform_account_id,'-','')=?", (digits,)).fetchone()
    if not row:
        raise SystemExit("[ABORT] no google account with CID %s in accounts table" % cid)
    return row[0]


def id_storage_caster(conn):
    """Store strategy ids in the SAME sqlite type campaigns.bidding_strategy_id uses,
    so 17's dict lookups (strat_name2id -> strat2camps) match exactly."""
    r = conn.execute("SELECT typeof(bidding_strategy_id) FROM campaigns "
                     "WHERE bidding_strategy_id IS NOT NULL LIMIT 1").fetchone()
    return (int if r and r[0] == "integer" else str)


def fetch_strategies(cid_digits, api_version):
    from dotenv import load_dotenv
    load_dotenv()
    yaml_path = os.environ.get("GOOGLE_ADS_CONFIGURATION_FILE_PATH")
    print("[ok] loading google-ads config from .env (GOOGLE_ADS_CONFIGURATION_FILE_PATH), like 03/05/18")
    from google.ads.googleads.client import GoogleAdsClient
    kw = {"version": api_version} if api_version else {}
    client = (GoogleAdsClient.load_from_storage(yaml_path, **kw) if yaml_path
              else GoogleAdsClient.load_from_env(**kw))
    svc = client.get_service("GoogleAdsService")
    by_id = {}
    # (a) strategies OWNED by this account: full detail incl. status
    q = ("SELECT bidding_strategy.id, bidding_strategy.name, bidding_strategy.type, "
         "bidding_strategy.status FROM bidding_strategy")
    for batch in svc.search_stream(customer_id=cid_digits, query=q):
        for row in batch.results:
            bs = row.bidding_strategy
            by_id[str(bs.id)] = {"name": bs.name or "", "type": bs.type_.name,
                                 "status": bs.status.name, "owner_cid": cid_digits,
                                 "owner_name": "", "owner": "account"}
    # (b) strategies ACCESSIBLE to this account -- includes MANAGER-OWNED (MCC) portfolios,
    # which (a) cannot see. Limited attributes, but carries name + owner.
    try:
        q2 = ("SELECT accessible_bidding_strategy.id, accessible_bidding_strategy.name, "
              "accessible_bidding_strategy.type, accessible_bidding_strategy.owner_customer_id, "
              "accessible_bidding_strategy.owner_descriptive_name FROM accessible_bidding_strategy")
        for batch in svc.search_stream(customer_id=cid_digits, query=q2):
            for row in batch.results:
                ab = row.accessible_bidding_strategy
                sid = str(ab.id)
                owner_cid = str(ab.owner_customer_id)
                rec = by_id.setdefault(sid, {"name": "", "type": ab.type_.name, "status": "",
                                             "owner_cid": owner_cid, "owner_name": "",
                                             "owner": "account"})
                rec["name"] = rec["name"] or (ab.name or "")
                rec["owner_cid"] = owner_cid
                rec["owner_name"] = ab.owner_descriptive_name or ""
                rec["owner"] = "account" if owner_cid == cid_digits else "mcc"
    except Exception as e:
        print("[WARN] accessible_bidding_strategy query failed (%s) -- MCC-owned strategies "
              "will be missing; account-owned pull still complete" % e)
    return [(sid, r["name"], r["type"], r["status"], r["owner"], r["owner_cid"], r["owner_name"])
            for sid, r in by_id.items()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--cid", default=DEFAULT_CID)
    ap.add_argument("--api-version", default=None, help="e.g. v23; default = client library default")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    acct = resolve_account_id(conn, args.cid)
    print("[ok] account resolved -> id=%s" % acct)
    cast = id_storage_caster(conn)

    rows = fetch_strategies(args.cid.replace("-", ""), args.api_version)
    n_mcc = sum(1 for r in rows if r[4] == "mcc")
    print("[ok] %d portfolio bidding strategies pulled (%d account-owned, %d MCC-owned)"
          % (len(rows), len(rows) - n_mcc, n_mcc))
    now = datetime.datetime.now().isoformat(timespec="seconds")
    prepared = [(acct, cast(sid), name, typ, st, owner, ocid, oname, now)
                for sid, name, typ, st, owner, ocid, oname in rows]
    for _, sid, name, typ, st, owner, ocid, oname, _n in prepared:
        tag = "  <-- MCC-owned (%s %s)" % (ocid, oname[:24]) if owner == "mcc" else ""
        print("   %-16s %-40s %-24s %-8s%s" % (sid, name[:40], typ, st, tag))

    # cross-check against real assignment: which strategies drive campaigns here
    strat_ids_in_use = {r[0] for r in conn.execute(
        "SELECT DISTINCT bidding_strategy_id FROM campaigns "
        "WHERE account_id=? AND bidding_strategy_id IS NOT NULL", (acct,))}
    pulled_ids = {p[1] for p in prepared}
    missing = strat_ids_in_use - pulled_ids
    if missing:
        print("[WARN] %d strategy id(s) referenced by campaigns not in the pull: %s" %
              (len(missing), sorted(missing)[:5]))
    print("[ok] %d of the pulled strategies are attached to campaigns in this account" %
          len(pulled_ids & strat_ids_in_use))

    if not args.apply:
        print("\nDry run only. Re-run with --apply to write.")
        conn.close()
        return
    conn.execute(DDL)
    have = {r[1] for r in conn.execute("PRAGMA table_info(bidding_strategies)")}
    for col, typ in MIGRATE_COLS:
        if col not in have:
            conn.execute("ALTER TABLE bidding_strategies ADD COLUMN %s %s" % (col, typ))
            print("[apply] migrated: added column %s" % col)
    conn.execute("DELETE FROM bidding_strategies WHERE account_id=?", (acct,))
    conn.executemany("INSERT INTO bidding_strategies "
                     "(account_id, bidding_strategy_id, strategy_name, strategy_type, status, "
                     "owner, owner_customer_id, owner_descriptive_name, last_synced_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?)", prepared)
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM bidding_strategies WHERE account_id=?", (acct,)).fetchone()[0]
    print("[apply] wrote %d rows | bidding_strategies now holds %d for account %s" % (len(prepared), n, acct))
    conn.close()


if __name__ == "__main__":
    main()
