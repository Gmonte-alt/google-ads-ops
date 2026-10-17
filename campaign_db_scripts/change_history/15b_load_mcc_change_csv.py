"""
15b_load_mcc_change_csv.py -- load the MCC-level UI change-history export ("360 Quote"
manager grid) into google_mcc_change_csv_raw.

Why: bidding-strategy changes are NOT tracked by Google's change_event API at any level
(BIDDING_STRATEGY is not a valid change_event resource type -- confirmed live), so the
manager's UI export is the ONLY record of MCC-owned portfolio target changes. This grid
contains manager-level changes exclusively (Account column = the manager; campaign and
ad-group columns empty), so it cannot duplicate account-level ledgers; 17's cross-source
dedup backstops regardless.

Idempotent hash-append: reload overlapping exports freely. Export path in the UI:
switch to the 360 Quote manager -> Change history -> set the date range -> Download CSV.
CADENCE: monthly-ish alongside the account grid backstop, or before any report needing
fresh MCC-owned target history.

    python 15b_load_mcc_change_csv.py --db PATH --csv PATH            # dry-run
    python 15b_load_mcc_change_csv.py --db PATH --csv PATH --apply
"""
import argparse
import csv
import datetime
import hashlib
import io
import re
import sqlite3

DEFAULT_MCC = "698-305-0143"

DDL = """CREATE TABLE IF NOT EXISTS google_mcc_change_csv_raw (
    mcc_customer_id TEXT NOT NULL,
    change_datetime TEXT NOT NULL,
    account_col TEXT,
    user_email TEXT,
    campaign_col TEXT,
    ad_group_col TEXT,
    changes_raw TEXT,
    row_hash TEXT NOT NULL UNIQUE,
    loaded_at TEXT NOT NULL
)"""

_QVALS = re.compile(r'"([^"]+)"\s*:\s*(?:Min\.?|Target)\s+ROAS changed from\s+([\d.]+)\s+to\s+([\d.]+)', re.I)


def parse_dt(s):
    return datetime.datetime.strptime(s.strip(), "%b %d, %Y, %I:%M:%S %p").isoformat(sep=" ")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--mcc", default=DEFAULT_MCC)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    raw = open(args.csv, "rb").read()
    for enc in ("utf-8-sig", "utf-16", "utf-8", "latin-1"):
        try:
            txt = raw.decode(enc)
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
    lines = txt.splitlines(keepends=True)
    # UI export: title line, date-range line, then the header
    hdr_idx = next(i for i, ln in enumerate(lines[:5]) if ln.startswith("Date & time"))
    rng = lines[hdr_idx - 1].strip().strip('"') if hdr_idx >= 1 else "?"
    rdr = csv.DictReader(io.StringIO("".join(lines[hdr_idx:])))
    rows = [r for r in rdr if any((v or "").strip() for v in r.values())]
    print("[ok] parsed %d rows (encoding %s; export range: %s)" % (len(rows), enc, rng))

    non_mgr = sum(1 for r in rows if "(Manager)" not in (r.get("Account") or ""))
    camp_rows = sum(1 for r in rows if (r.get("Campaign") or "").strip() or (r.get("Ad group") or "").strip())
    if non_mgr or camp_rows:
        print("[WARN] %d non-manager-account rows / %d rows with campaign or ad-group context -- "
              "unexpected for an MCC export; loading anyway (17 dedups cross-source)" % (non_mgr, camp_rows))
    else:
        print("[ok] all rows are manager-level (no campaign/ad-group context) -- no account-ledger overlap")

    import collections
    per = collections.Counter()
    for r in rows:
        for name, _o, _n in _QVALS.findall(r.get("Changes") or ""):
            per[name] += 1
    print("[ok] quoted ROAS statements by strategy:")
    for name, n in per.most_common(12):
        print("   %3d  %s" % (n, name))
    if len(per) > 12:
        print("   ... and %d more strategies" % (len(per) - 12))

    prepared = []
    for r in rows:
        dt = parse_dt(r["Date & time"])
        h = hashlib.sha256(("|".join([args.mcc.replace("-", ""), dt, r.get("User") or "",
                                      r.get("Changes") or ""])).encode()).hexdigest()
        prepared.append((args.mcc.replace("-", ""), dt, r.get("Account") or "", r.get("User") or "",
                         r.get("Campaign") or "", r.get("Ad group") or "", r.get("Changes") or "", h))

    if not args.apply:
        print("\nDry run only. Re-run with --apply to write.")
        return
    conn = sqlite3.connect(args.db)
    conn.execute(DDL)
    now = datetime.datetime.now().isoformat(timespec="seconds")
    ins = 0
    for p in prepared:
        c = conn.execute("INSERT OR IGNORE INTO google_mcc_change_csv_raw "
                         "(mcc_customer_id, change_datetime, account_col, user_email, campaign_col, "
                         " ad_group_col, changes_raw, row_hash, loaded_at) VALUES (?,?,?,?,?,?,?,?,?)",
                         p + (now,))
        ins += c.rowcount
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM google_mcc_change_csv_raw WHERE mcc_customer_id=?",
                     (args.mcc.replace("-", ""),)).fetchone()[0]
    print("[apply] inserted %d new, skipped %d already-present | table holds %d rows" %
          (ins, len(prepared) - ins, n))
    conn.close()


if __name__ == "__main__":
    main()
