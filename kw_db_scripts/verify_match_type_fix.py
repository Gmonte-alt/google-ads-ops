#!/usr/bin/env python
"""
verify_match_type_fix.py  --  confirm the match_type enum fix corrected the DB account-wide.

Cross-checks keyword_instances.match_type against an independent ground truth: a Google Ads UI
keyword report CSV export. The export carries both the Match type column AND the bracket
convention ([exact], "phrase", bare=broad), plus the Ad group ID + Keyword ID that together
form ad_group_criterion_id -- so we join DB<->export on the exact placement and compare.

This is a TRUE independent check (DB vs. Google's own export), not the DB validating itself.

Two cross-checks per row:
  1. export 'Match type' column  vs  DB match_type
  2. export bracket convention   vs  DB match_type   (belt-and-suspenders)

Read-only on the DB. Prints match/mismatch counts and lists any mismatches.

Usage:
    python verify_match_type_fix.py --db PATH --export "Search_keyword_report__18_.csv"
"""
import argparse
import csv
import re
import sqlite3
import sys


def norm_match(s):
    """Normalize the UI 'Match type' string to EXACT/PHRASE/BROAD."""
    s = (s or "").strip().lower()
    if "exact" in s:
        return "EXACT"
    if "phrase" in s:
        return "PHRASE"
    if "broad" in s:
        return "BROAD"
    return "UNKNOWN"


def bracket_match(keyword):
    """Infer match type from Google's bracket convention in the keyword text."""
    k = (keyword or "").strip()
    if k.startswith("[") and k.endswith("]"):
        return "EXACT"
    if k.startswith('"') and k.endswith('"'):
        return "PHRASE"
    return "BROAD"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--export", required=True, help="Google Ads UI keyword report CSV")
    ap.add_argument("--account", type=int, default=1)
    args = ap.parse_args()

    # --- load the UI export (ground truth) ---
    # The export has 3 preamble lines before the header row.
    rows = []
    with open(args.export, encoding="utf-8-sig", errors="replace") as f:
        reader = csv.reader(f)
        header = None
        for r in reader:
            if header is None:
                # find the header row (contains 'Keyword ID')
                if any(c.strip() == "Keyword ID" for c in r):
                    header = r
                continue
            if not r or len(r) < len(header):
                continue
            rows.append(dict(zip(header, r)))
    if not rows:
        sys.exit("[ABORT] no data rows parsed from export -- check the file/header.")

    print("UI export rows parsed: %d" % len(rows))

    # build ground-truth map: ad_group_criterion_id -> (match_from_column, match_from_bracket, keyword, campaign)
    truth = {}
    for r in rows:
        agid = (r.get("Ad group ID") or "").strip()
        kwid = (r.get("Keyword ID") or "").strip()
        if not agid or not kwid:
            continue
        agc = "%s~%s" % (agid, kwid)
        truth[agc] = (
            norm_match(r.get("Match type")),
            bracket_match(r.get("Keyword")),
            (r.get("Keyword") or "").strip(),
            (r.get("Campaign") or "").strip(),
        )
    print("distinct placements (ad_group_criterion_id) in export: %d" % len(truth))

    # --- compare against DB ---
    conn = sqlite3.connect("file:%s?mode=ro" % args.db, uri=True)
    c = conn.cursor()

    checked = 0
    col_match = col_mismatch = 0
    brk_match = brk_mismatch = 0
    not_in_db = 0
    mismatches = []

    for agc, (m_col, m_brk, kw, camp) in truth.items():
        row = c.execute(
            "SELECT match_type FROM keyword_instances "
            "WHERE account_id=? AND ad_group_criterion_id=?",
            (args.account, agc),
        ).fetchone()
        if row is None:
            not_in_db += 1
            continue
        db_mt = (row[0] or "").upper()
        checked += 1

        # cross-check 1: export column vs DB
        if m_col == db_mt:
            col_match += 1
        else:
            col_mismatch += 1
            mismatches.append((agc, kw, camp, "col", m_col, db_mt))

        # cross-check 2: bracket convention vs DB
        if m_brk == db_mt:
            brk_match += 1
        else:
            brk_mismatch += 1

    print("\n" + "=" * 60)
    print("MATCH-TYPE VERIFICATION  (account_id=%d)" % args.account)
    print("=" * 60)
    print("placements checked (in both export & DB): %d" % checked)
    print("  not found in DB (export-only):          %d" % not_in_db)
    print()
    print("Cross-check 1 -- export 'Match type' column vs DB:")
    print("  MATCH:    %d" % col_match)
    print("  MISMATCH: %d" % col_mismatch)
    print()
    print("Cross-check 2 -- bracket convention vs DB:")
    print("  MATCH:    %d" % brk_match)
    print("  MISMATCH: %d" % brk_mismatch)
    print()
    if col_mismatch == 0:
        print("RESULT: PASS -- every checked placement's DB match_type equals the UI export. "
              "The enum fix corrected the data.")
    else:
        print("RESULT: FAIL -- %d mismatches remain. The fix did NOT fully correct the data." % col_mismatch)
        print("\nFirst mismatches (ad_group_criterion_id | keyword | campaign | export -> DB):")
        for agc, kw, camp, kind, exp, db in mismatches[:25]:
            print("  %s | %s | %s | export=%s -> db=%s" % (agc, kw[:30], camp[:35], exp, db))

    conn.close()


if __name__ == "__main__":
    main()
