"""
16_derive_campaign_name_history_google.py  (Google change-history)

Pipeline step 16.
CADENCE: RECURRING -- run after 18 (API pull) to rebuild from the freshened ledger.

Rename-aware name dimension (SCD2). Reconstructs every interval FROM SCRATCH each
run, from three durable sources merged by date:
  * current campaigns (the latest name + first_seen floor),
  * campaign.name events in the API ledger (google_change_event_raw),
  * campaign_rename_overrides (pre-API-window renames, 'new' inferred by chaining).
Because renames live durably in the ledger, a re-derive rebuilds byte-identical
intervals -- there is NO in-place append path to clobber. Zero rename events ->
one open interval per campaign (the former baseline behavior, unchanged).

Boundary convention: a rename day belongs to the NEW name; the closing interval
ends the day before (clean for inclusive-BETWEEN resolution). If the reconstructed
latest name != the current campaigns name, an uncaptured-rename WARN fires.

Idempotent derive-from-scratch, scoped to platform='google' AND the resolved
account so Microsoft's rows in this shared table are never touched. Backs up the
table before mutating.

Usage:
    python derive_campaign_name_history_google.py --db PATH                 # dry-run
    python derive_campaign_name_history_google.py --db PATH --apply
"""

import argparse
import collections
import json
import sys
import campaign_db_scripts.change_history.gads_change_common as C

EPOCH_FLOOR = "1970-01-01"  # inversion-guard fallback if first_seen_date is null


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--cid", default=C.DEFAULT_CID)
    ap.add_argument("--convention-version", default="v1-names-2026-06",
                    help="non-null tag stored in convention_version (live table requires it)")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = C.connect(args.db, write=args.apply)
    try:
        acct = C.resolve_account_id(conn, args.cid)
        camps = conn.execute(
            "SELECT campaign_id, campaign_name, first_seen_date FROM campaigns WHERE account_id=?",
            (acct,)).fetchall()
        print("[ok] %d Google campaigns under account %d" % (len(camps), acct))

        # Rename events: API ledger (CAMPAIGN + campaign.name) + override table.
        # Reconstruct-from-scratch every run -> a re-derive rebuilds identical
        # intervals (renames live durably in the ledger; no in-place append).
        renames = collections.defaultdict(list)   # campaign_id -> [(date, old, new, source)]
        if C.table_exists(conn, "google_change_event_raw"):
            for r in conn.execute(
                "SELECT change_datetime, change_resource_name, changes_json FROM google_change_event_raw "
                "WHERE account_id=? AND change_resource_type='CAMPAIGN'", (acct,)):
                changes = json.loads(r["changes_json"])
                if C.NAME_PATH in changes:
                    old, new = changes[C.NAME_PATH]
                    _, cid = C.parse_resource_segment_id(r["change_resource_name"])
                    renames[cid].append((r["change_datetime"][:10], old, new, "api_rename"))
        if C.table_exists(conn, "campaign_rename_overrides"):
            ov_cols = {r["name"] for r in conn.execute("PRAGMA table_info(campaign_rename_overrides)")}
            need = {"campaign_id", "historical_name", "valid_to", "platform", "account_id"}
            if need.issubset(ov_cols):
                for r in conn.execute(
                    "SELECT campaign_id, historical_name, valid_to FROM campaign_rename_overrides "
                    "WHERE platform=? AND account_id=?", (C.PLATFORM, acct)):
                    renames[r["campaign_id"]].append((r["valid_to"][:10], r["historical_name"], None, "override"))
            else:
                print("[WARN] campaign_rename_overrides exists but lacks the expected columns "
                      "%s (found: %s); skipping the overrides source. Harmless if empty (pre-window "
                      "renames only); reconcile the shared-table schema when convenient." % (
                          sorted(need), sorted(ov_cols)))
        print("[ok] %d rename events across %d campaigns (ledger + overrides)" % (
            sum(len(v) for v in renames.values()), sum(1 for v in renames.values() if v)))

        rows, null_floor, warns = [], 0, 0
        for c in camps:
            cid, cur = c["campaign_id"], c["campaign_name"]
            vf = c["first_seen_date"] or EPOCH_FLOOR
            if not c["first_seen_date"]:
                null_floor += 1
            rlist = sorted(renames.get(cid, []), key=lambda x: x[0])
            filled = []   # infer missing 'new' for override rows by chaining
            for i, (d, old, new, src) in enumerate(rlist):
                if new is None:
                    new = rlist[i + 1][1] if i + 1 < len(rlist) else cur
                filled.append((d, old, new, src))
            intervals, warn = C.reconstruct_name_intervals(cur, vf, filled)
            if warn:
                warns += 1
                print("[WARN] %s: %s" % (cid, warn))
            for iv in intervals:
                rows.append((C.PLATFORM, acct, cid, iv["name"], iv["valid_from"],
                             iv["valid_to"], iv["source"], args.convention_version))
        if null_floor:
            print("[WARN] %d campaigns had no first_seen_date; floored to %s" % (null_floor, EPOCH_FLOOR))

        n_closed = sum(1 for r in rows if r[5] is not None)
        print("[plan] %d intervals across %d campaigns (%d closed, %d open)" % (
            len(rows), len(camps), n_closed, len(rows) - n_closed))
        for r in rows[:8]:
            print("       %s  %-44s  %s -> %s  [%s]" % (r[2], r[3][:44], r[4], r[5] or "open", r[6]))
        if len(rows) > 8:
            print("       ... (%d more)" % (len(rows) - 8))

        NH_COLS = ["platform", "account_id", "campaign_id", "campaign_name",
                   "valid_from", "valid_to", "source", "convention_version"]
        viol = C.check_rows_notnull(conn, "campaign_name_history", NH_COLS, rows)
        if viol:
            for col, n in viol:
                msg = "not populated by this script" if n < 0 else "%d rows write NULL" % n
                print("[ABORT] live NOT NULL column campaign_name_history.%s: %s" % (col, msg))
            sys.exit(2)
        print("[ok] all rows satisfy live NOT NULL constraints (convention_version='%s')" % args.convention_version)

        if args.apply:
            bk = C.backup_table(conn, "campaign_name_history")
            print("[apply] backed up campaign_name_history -> %s" % bk)
            conn.execute("DELETE FROM campaign_name_history WHERE platform=? AND account_id=?",
                         (C.PLATFORM, acct))
            conn.executemany(
                "INSERT INTO campaign_name_history "
                "(platform, account_id, campaign_id, campaign_name, valid_from, valid_to, source, convention_version) "
                "VALUES (?,?,?,?,?,?,?,?)", rows)
            conn.execute("COMMIT")
            print("[apply] wrote %d Google name-history intervals" % len(rows))
            print("        resolve names via: report_date BETWEEN valid_from AND COALESCE(valid_to,'9999-12-31')")
        else:
            print("\nDry run only. Re-run with --apply to write.")
    except Exception:
        if args.apply:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
