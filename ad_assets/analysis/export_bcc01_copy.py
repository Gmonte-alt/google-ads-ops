#!/usr/bin/env python
"""
export_bcc01_copy.py -- ad_assets/analysis/ -- emit the EXACT BCC-01 copy
block for the bulk-import build, machine-verified so nothing is retyped:

  * the 15 headlines are embedded here verbatim from
    creative_decision_record_BCC-01.md §4, and the script RECOMPUTES the
    fingerprint from them with the canonical recipe and ABORTS unless it
    equals the pre-registered e1fd7d1595 -- so if this file emits, the texts
    are the registered creative, character for character
  * the 4 descriptions are pulled VERBATIM from the DB (V1's current
    composition, fingerprint fdb8e75afa) -- the decision record says "V1's
    four, verbatim" without printing them; the DB is the untruncated source

Output: ad_assets\\exports\\bcc01_copy_<date>.csv
        (type, position, text, char_length) -- the import generator's input.
READ-ONLY on the DB.

  python export_bcc01_copy.py --db "C:\\Projects\\google-ads-ops\\db\\paid_search.db"
"""
import argparse
import csv
import datetime
import hashlib
import os
import re
import sqlite3
import sys

ACCOUNT_ID = 1
EXPECTED_FP = "e1fd7d1595"
V1_FP = "fdb8e75afa"
EXPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "exports")

HEADLINES = [  # record §4, verbatim -- DO NOT EDIT (fingerprint-checked below)
    "Enter Zip Code To Get Quotes",
    "$98 {LOCATION(State)} Home Insurance",
    "{LOCATION(State)} Home Insurance",
    "2026 Insurance Rate Comparison",
    "Home Insurance Prices In 2026",
    "See Your New 2026 Home Rate",
    "Home Insurance From $98/Mo",
    "Get A Quote In Less Than 3 Min",
    "Instant Home Quote",
    "Protect Your Home",
    "Home Insurance Quotes Online",
    "{LOCATION(State)} Best Rates",
    "Cheapest Insurance Rates",
    "Compare Quotes Side By Side",
    "Home Insurance Free Quotes",
]


def fingerprint(headlines):
    norm = sorted(re.sub(r"\s+", " ", h).strip().lower() for h in headlines)
    return hashlib.md5("||".join(norm).encode("utf-8")).hexdigest()[:10]


def main():
    p = argparse.ArgumentParser(description="Emit the verified BCC-01 copy block.")
    p.add_argument("--db", required=True)
    args = p.parse_args()
    if not os.path.isfile(args.db):
        sys.exit("[ABORT] database not found: %s" % args.db)
    os.makedirs(EXPORT_DIR, exist_ok=True)

    fp = fingerprint(HEADLINES)
    if fp != EXPECTED_FP:
        sys.exit("[ABORT] headline set computes to %s, expected %s -- the embedded "
                 "texts have drifted from the record. DO NOT LOAD; reconcile first."
                 % (fp, EXPECTED_FP))
    print("[FP] headline set verified -> %s (matches pre-registered)" % fp)

    # length check: plain lines must fit 30 raw chars; insertion lines
    # ({LOCATION...}) validate by RENDERED length under Google's fallback
    # handling, and these exact strings serve live in V1/V12 today -- raw
    # length is expected to exceed 30 and is not an error.
    over = [h for h in HEADLINES if "{" not in h and len(h) > 30]
    if over:
        sys.exit("[ABORT] plain headline(s) over 30 chars: %s" % over)
    for h in HEADLINES:
        if "{" in h:
            print("[LEN] insertion line, raw %d chars (renders shorter; live "
                  "precedent in V1/V12): %s" % (len(h), h))

    conn = sqlite3.connect("file:%s?mode=ro" % args.db.replace("\\", "/"), uri=True)
    descs = [r[0] for r in conn.execute(
        """SELECT DISTINCT t.text
           FROM rsa_ads r
           JOIN rsa_ad_assets c ON c.account_id=r.account_id AND c.ad_id=r.ad_id
           JOIN asset_text_data t ON t.account_id=c.account_id AND t.asset_id=c.asset_id
           WHERE r.account_id=? AND r.fingerprint=? AND c.field_type='DESCRIPTION'
             AND c.removed_date IS NULL
           ORDER BY t.text""", (ACCOUNT_ID, V1_FP)).fetchall()]
    conn.close()

    if len(descs) != 4:
        sys.exit("[ABORT] expected exactly 4 distinct V1 descriptions, found %d -- "
                 "inspect before loading (record says 'V1's four, verbatim')." % len(descs))
    over_d = [d for d in descs if len(d) > 90]
    if over_d:
        sys.exit("[ABORT] description(s) over 90 chars: %s" % [d[:40] for d in over_d])
    print("[DESC] 4 descriptions pulled verbatim from V1 (%s), all <=90 chars" % V1_FP)

    stamp = datetime.date.today().isoformat()
    out = os.path.join(EXPORT_DIR, "bcc01_copy_%s.csv" % stamp)
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["type", "position", "text", "char_length"])
        for i, h in enumerate(HEADLINES, 1):
            w.writerow(["HEADLINE", i, h, len(h)])
        for i, d in enumerate(descs, 1):
            w.writerow(["DESCRIPTION", i, d, len(d)])
    print("[DONE] %s -- 15 headlines + 4 descriptions, fingerprint-verified. "
          "The import generator should consume THIS file, never retyped text." % out)


if __name__ == "__main__":
    main()
