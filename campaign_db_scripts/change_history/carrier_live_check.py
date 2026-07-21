"""
carrier_live_check.py -- READ-ONLY end-to-end check on LIVE data (un-numbered test).

Uses the canonical BrandMatcher. Builds it from the live brand_registry, pulls real
keyword-add cells from google_change_csv_raw (account 1), extracts + classifies each
added keyword, prints carrier/competitor/generic. Opens the DB read-only.

    cd change_history && python carrier_live_check.py
"""
import argparse
import collections
import sqlite3
import change_taxonomy as T
import carrier_enrich as CE
import brand_matcher as BM

DEFAULT_DB = r"C:\Projects\google-ads-ops\db\paid_search.db"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--account", type=int, default=1)
    args = ap.parse_args()

    c = sqlite3.connect("file:%s?mode=ro" % args.db, uri=True)
    bm = BM.BrandMatcher(c)
    print("[ok] BrandMatcher loaded: %d exact variants, %d word-boundary patterns" % (
        len(bm.exact_map), len(bm.compiled)))

    rows = [r[0] for r in c.execute(
        "SELECT changes_raw FROM google_change_csv_raw WHERE account_id=?", (args.account,))]
    kwadd = [raw for raw in rows if T.categorize(raw)["category"] == "keyword_add"]
    header_total = sum(T.categorize(raw)["count"] for raw in kwadd)
    print("[ok] %d CSV cells | %d keyword_add cells | %d keywords by header count" % (
        len(rows), len(kwadd), header_total))

    bucket = collections.Counter()
    examples = collections.defaultdict(list)
    extracted = 0
    for raw in kwadd:
        for kw in CE.extract_keywords(raw):
            extracted += 1
            b = CE.to_bucket(CE.classify_match(bm.match(kw)), three_way=True)
            bucket[b] += 1
            if len(examples[b]) < 8:
                examples[b].append(kw)
    print("[ok] %d keywords extracted (should be near the %d header count)\n" % (extracted, header_total))
    print("=== added-keyword classification (three-way) ===")
    for b in ("carrier", "competitor", "generic"):
        print("  %-11s %4d   e.g. %s" % (b, bucket.get(b, 0), examples.get(b, [])))
    c.close()


if __name__ == "__main__":
    main()
