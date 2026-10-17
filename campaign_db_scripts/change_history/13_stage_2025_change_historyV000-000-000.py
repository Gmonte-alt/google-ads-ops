"""
13_stage_2025_change_history.py  (Google change-history -- 2025 historical seed)

Pipeline step 13. CADENCE: one-time-ish historical seed (re-loadable; row_hash-idempotent).

Loads the 2025 UI Change History export (full year) into its OWN permanent staging
table, google_change_csv_2025_raw -- deliberately NOT co-mingled with the 2026 weekly
ledger google_change_csv_raw, because 2025 is a different structural ERA (single-state
campaigns, target ROAS managed at AD-GROUP grain), ~500x the volume, and feeds the
intervention-clustering layer rather than the weekly log.

Differs from 15 in three deliberate ways:
  1. STREAMS the CSV row-by-row (constant memory) instead of readlines()+full-copy --
     the 2025 file is ~165 MB / ~4.3M physical lines.
  2. Writes to google_change_csv_2025_raw (own table), indexed for the clustering pass.
  3. Treats the AD-GROUP column as primary signal (2025 carrier/vertical/renters live
     there, e.g. 'Renters-NonGeneric-Insured Nation-Core-NA'), preserved verbatim.

Everything else mirrors 15: utf-8-sig read, 2-preamble/header-on-line-3 parse, csv
module assembles multi-line Changes cells, U+202F date normalize (plus mojibake-tolerance
as cheap insurance), row_hash idempotency, dry-run default, --apply, --db required,
backup-before-mutate discipline.

Usage:
    python 13_stage_2025_change_history.py --db PATH --csv PATH                 # dry-run
    python 13_stage_2025_change_history.py --db PATH --csv PATH --apply
"""

import argparse
import csv
import hashlib
import io
import os
import sys
import datetime

# flat import when run in place from change_history\ ; fall back to package path like 15
try:
    import gads_change_common as C
    _HAVE_COMMON = True
except ImportError:
    try:
        import campaign_db_scripts.change_history.gads_change_common as C  # noqa
        _HAVE_COMMON = True
    except ImportError:
        _HAVE_COMMON = False

PREAMBLE_ROWS = 2
EXPECTED_HEADER = ["Date & time", "User", "Campaign", "Ad group", "Changes"]
TABLE = "google_change_csv_2025_raw"
DEFAULT_CID = getattr(C, "DEFAULT_CID", "888-650-2147") if _HAVE_COMMON else "888-650-2147"

# U+202F narrow-no-break-space, plus its UTF-8-misread-as-Latin-1 mojibake form.
NNBSP = "\u202f"
MOJIBAKE_NNBSP = "\u00e2\u20ac\u00af"   # 'â€¯' -- only appears if the file is decoded wrong


def normalize_dt(dt_raw):
    """'Jul 28, 2025, 4:08:55<U+202F>PM' -> ISO 'YYYY-MM-DD HH:MM:SS'.
    Tolerant of clean U+202F and the mojibake sequence; falls back to common formats."""
    s = (dt_raw or "").replace(MOJIBAKE_NNBSP, " ").replace(NNBSP, " ")
    s = " ".join(s.split())  # collapse whitespace
    for fmt in ("%b %d, %Y, %I:%M:%S %p", "%b %d, %Y, %I:%M %p"):
        try:
            return datetime.datetime.strptime(s, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    # last resort: let the shared normalizer try (keeps parity with 15 if present)
    if _HAVE_COMMON and hasattr(C, "normalize_dt"):
        return C.normalize_dt(dt_raw)
    raise ValueError("unparseable date: %r" % dt_raw)


def row_hash(acct, dt_raw, camp, ag, changes):
    if _HAVE_COMMON and hasattr(C, "row_hash"):
        return C.row_hash(acct, dt_raw, camp, ag, changes)
    h = hashlib.sha256()
    h.update(("%s|%s|%s|%s|%s" % (acct, dt_raw, camp or "", ag or "", changes or "")).encode("utf-8"))
    return h.hexdigest()


def stream_records(path):
    """Yield 5-col logical rows, streaming. The csv module assembles multi-line Changes
    cells; we skip the 2 preamble lines, assert the header, then yield data rows."""
    fh = io.open(path, encoding="utf-8-sig", newline="")
    try:
        for _ in range(PREAMBLE_ROWS):
            fh.readline()
        rdr = csv.reader(fh)
        header = next(rdr)
        if header != EXPECTED_HEADER:
            raise RuntimeError("unexpected header %r (preamble shifted?)" % header)
        for r in rdr:
            yield r
    finally:
        fh.close()


def ensure_table(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS %s ("
        " id INTEGER PRIMARY KEY,"
        " account_id INTEGER NOT NULL,"
        " source_file TEXT,"
        " row_hash TEXT UNIQUE,"
        " change_datetime TEXT,"      # ISO, sortable
        " date_time_raw TEXT,"
        " user_email TEXT,"
        " campaign_col TEXT,"
        " ad_group_col TEXT,"         # 2025 primary signal -- preserved verbatim
        " changes_raw TEXT,"
        " loaded_at TEXT)" % TABLE)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_%s_dt ON %s (change_datetime)" % (TABLE, TABLE))
    conn.execute("CREATE INDEX IF NOT EXISTS ix_%s_camp_ag ON %s (campaign_col, ad_group_col)" % (TABLE, TABLE))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--cid", default=DEFAULT_CID)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--progress", type=int, default=100000, help="log every N rows")
    args = ap.parse_args()

    if not _HAVE_COMMON:
        print("[!] gads_change_common not importable -- run from inside change_history\\ "
              "(using built-in fallbacks for hash/date/connect).")

    import sqlite3
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    if _HAVE_COMMON and hasattr(C, "resolve_account_id"):
        acct = C.resolve_account_id(conn, args.cid)
    else:
        row = conn.execute("SELECT id FROM accounts WHERE cid=? OR cid=?",
                           (args.cid, args.cid.replace("-", ""))).fetchone()
        acct = row["id"] if row else 1
    print("[ok] account resolved -> id=%d" % acct)

    ensure_table(conn)
    existing = set(r[0] for r in conn.execute("SELECT row_hash FROM %s" % TABLE))
    print("[ok] %s currently holds %d rows" % (TABLE, len(existing)))

    src = os.path.basename(args.csv)
    loaded_at = datetime.datetime.now().isoformat(timespec="seconds")
    prepared, seen_hashes = [], set()
    n_rows = n_bad_cols = date_fail = dup_in_file = 0

    for r in stream_records(args.csv):
        n_rows += 1
        if len(r) != 5:
            n_bad_cols += 1
            continue
        dt_raw, user, camp, ag, changes = r
        try:
            iso = normalize_dt(dt_raw)
        except ValueError:
            date_fail += 1
            continue
        rh = row_hash(acct, dt_raw, camp, ag, changes)
        if rh in existing or rh in seen_hashes:
            dup_in_file += 1
            continue
        seen_hashes.add(rh)
        prepared.append((acct, src, rh, iso, dt_raw, user or None,
                         camp or None, ag or None, changes, loaded_at))
        if args.progress and n_rows % args.progress == 0:
            print("   ... scanned %d logical rows (%d new so far)" % (n_rows, len(prepared)))

    print("[ok] parsed %d logical rows from %s" % (n_rows, src))
    print("[ok] %d new | %d already in ledger | %d dup-within-file | %d bad-col | %d date-fail"
          % (len(prepared), (n_rows - len(prepared) - n_bad_cols - date_fail - dup_in_file)
             + dup_in_file, dup_in_file, n_bad_cols, date_fail))
    if date_fail:
        print("[WARN] %d rows failed date parse -- paste a few raw dates if this is non-trivial" % date_fail)
    if n_bad_cols:
        print("[WARN] %d rows were not 5 columns (multi-line assembly issue)" % n_bad_cols)

    if args.apply:
        conn.executemany(
            "INSERT OR IGNORE INTO %s "
            "(account_id, source_file, row_hash, change_datetime, date_time_raw, "
            " user_email, campaign_col, ad_group_col, changes_raw, loaded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)" % TABLE, prepared)
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM %s" % TABLE).fetchone()[0]
        rng = conn.execute("SELECT MIN(change_datetime), MAX(change_datetime) FROM %s" % TABLE).fetchone()
        print("[apply] inserted %d new rows | %s now holds %d rows | range %s .. %s"
              % (len(prepared), TABLE, total, rng[0], rng[1]))
    else:
        print("\nDry run only. Re-run with --apply to load the staging table.")
    conn.close()


if __name__ == "__main__":
    main()
