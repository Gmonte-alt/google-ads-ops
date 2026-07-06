"""
15_load_google_change_csv.py  (Google change-history, Phase 1)

Pipeline step 15.
CADENCE: RECURRING -- run for every new CSV export (weekly). Model-A: idempotent
on row_hash, so the ledger grows forward and outlives Google's export window.
NOTE: weekly-CSV is the PHASE 1 INTERIM. Once the API change_event layer is built,
the recurring feed becomes API-pull + re-derive and this CSV load becomes the
historical seed only.

Loads a Google UI change-history CSV into the accumulating raw ledger
(google_change_csv_raw). Model-A: idempotent on row_hash, so re-loading the same
export -- or overlapping weekly exports -- never duplicates. The ledger grows
forward and outlives the export window.

Handles the verified format: 2 preamble rows, header on line 3, multi-line prose
in Changes (assembled by the csv module), U+202F narrow-no-break-space in dates.

Usage:
    python load_google_change_csv.py --db PATH --csv PATH                 # dry-run
    python load_google_change_csv.py --db PATH --csv PATH --apply
"""

import argparse
import csv
import os
import sys
import gads_change_common as C

PREAMBLE_ROWS = 2
EXPECTED_HEADER = ["Date & time", "User", "Campaign", "Ad group", "Changes"]


def read_records(path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        lines = fh.readlines()
    # header is the (PREAMBLE_ROWS+1)-th physical line; csv assembles multi-line cells
    import io
    rdr = csv.reader(io.StringIO("".join(lines[PREAMBLE_ROWS:])))
    header = next(rdr)
    if header != EXPECTED_HEADER:
        raise RuntimeError("unexpected header %r (preamble shifted?)" % header)
    rows = list(rdr)
    bad = [i for i, r in enumerate(rows) if len(r) != 5]
    if bad:
        raise RuntimeError("%d rows not 5-col (multi-line assembly failed): %r" % (len(bad), bad[:5]))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--cid", default=C.DEFAULT_CID)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    rows = read_records(args.csv)
    src = os.path.basename(args.csv)
    print("[ok] parsed %d logical records from %s" % (len(rows), src))

    conn = C.connect(args.db, write=args.apply)
    try:
        acct = C.resolve_account_id(conn, args.cid)
        loaded_at = C.now_iso()

        prepared, date_fail = [], 0
        for dt_raw, user, camp, ag, changes in rows:
            try:
                iso = C.normalize_dt(dt_raw)
            except ValueError:
                date_fail += 1
                continue
            rh = C.row_hash(acct, dt_raw, camp, ag, changes)
            prepared.append((acct, src, rh, iso, dt_raw, user or None,
                             camp or None, ag or None, changes, loaded_at))
        if date_fail:
            print("[WARN] %d rows failed date parse (check for new date format)" % date_fail)

        # how many are new vs already in the ledger
        existing = set()
        if C.table_exists(conn, "google_change_csv_raw"):
            for (rh,) in conn.execute("SELECT row_hash FROM google_change_csv_raw"):
                existing.add(rh)
        new = [p for p in prepared if p[2] not in existing]
        dup = len(prepared) - len(new)
        print("[ok] %d records prepared | %d new | %d already in ledger (idempotent skip)" % (
            len(prepared), len(new), dup))

        if args.apply:
            conn.executemany(
                "INSERT OR IGNORE INTO google_change_csv_raw "
                "(account_id, source_file, row_hash, change_datetime, date_time_raw, "
                " user_email, campaign_col, ad_group_col, changes_raw, loaded_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)", prepared)
            conn.execute("COMMIT")
            total = conn.execute("SELECT COUNT(*) FROM google_change_csv_raw").fetchone()[0]
            print("[apply] inserted %d new rows | ledger now holds %d rows" % (len(new), total))
        else:
            print("\nDry run only. Re-run with --apply to load the ledger.")
    except Exception:
        if args.apply:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
