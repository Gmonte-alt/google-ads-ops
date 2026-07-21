#!/usr/bin/env python
"""
inspect_taxonomy_and_prefixes.py -- read-only. Dumps product_taxonomy (with business_unit + vertical)
and the account-1 campaign-name prefixes, so the change-history enricher can map prefix -> vertical.

Prints counts first so it never looks silently empty.

    python inspect_taxonomy_and_prefixes.py
"""
import sqlite3
import re

DB = r"C:\Projects\google-ads-ops\db\paid_search.db"

def main():
    c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)

    print("=" * 70)
    print("PRODUCT_TAXONOMY  (business_unit | vertical | product_code | node_type | parent)")
    print("=" * 70)
    cols = [r[1] for r in c.execute("PRAGMA table_info(product_taxonomy)")]
    has_bu = "business_unit" in cols
    rows = c.execute(
        "SELECT %s vertical, product_code, product_name, node_type, parent_code "
        "FROM product_taxonomy ORDER BY vertical, node_type, product_code"
        % ("business_unit," if has_bu else "")
    ).fetchall()
    print("rows: %d   (business_unit column present: %s)\n" % (len(rows), has_bu))
    for r in rows:
        if has_bu:
            bu, vert, code, name, node, parent = r
            print("  %-6s | %-10s | %-22s | %-18s | %s"
                  % (bu or "-", vert or "-", code, node, parent or ""))
        else:
            vert, code, name, node, parent = r
            print("  %-10s | %-22s | %-18s | %s" % (vert or "-", code, node, parent or ""))

    print("\n" + "=" * 70)
    print("ACCOUNT-1 CAMPAIGN NAME PREFIXES  (vertical-token before first '-' or '_')")
    print("=" * 70)
    names = [r[0] for r in c.execute(
        "SELECT campaign_name FROM campaigns WHERE account_id=1 AND campaign_name IS NOT NULL")]
    print("account-1 campaigns: %d\n" % len(names))

    # derive the leading vertical token: everything up to the first '-' or '_'
    from collections import Counter
    pfx = Counter()
    for nm in names:
        m = re.split(r"[-_]", nm, maxsplit=1)
        token = m[0].strip() if m else nm
        pfx[token] += 1
    print("distinct leading tokens: %d" % len(pfx))
    print("%-32s %s" % ("prefix token", "campaigns"))
    print("-" * 45)
    for token, n in pfx.most_common():
        print("  %-30s %d" % (token, n))

    print("\n" + "=" * 70)
    print("SAMPLE full campaign names per prefix (to confirm the mapping)")
    print("=" * 70)
    seen = set()
    for nm in sorted(names):
        m = re.split(r"[-_]", nm, maxsplit=1)
        token = m[0].strip() if m else nm
        if token not in seen:
            seen.add(token)
            print("  [%s]  %s" % (token, nm))

    c.close()
    print("\nDone.")

if __name__ == "__main__":
    main()
