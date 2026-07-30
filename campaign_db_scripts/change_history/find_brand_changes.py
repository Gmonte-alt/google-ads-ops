"""find_brand_changes.py -- READ-ONLY. Surfaces every change mentioning a brand
(default: geico) across BOTH ledgers for a year. Shows only the matching lines.
    python find_brand_changes.py                       # geico, 2026
    python find_brand_changes.py --brand lemonade --year 2026 --csv geico.csv
"""
import argparse, collections, csv, re, sqlite3
import change_taxonomy as T
import xlsx_out

DEFAULT_DB = r"C:\Projects\google-ads-ops\db\paid_search.db"

def has(conn, t):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone() is not None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--brand", default="geico")
    ap.add_argument("--year", default="2026")
    ap.add_argument("--google-account", type=int, default=1)
    ap.add_argument("--microsoft-account", type=int, default=2)
    ap.add_argument("--csv", default=None)
    ap.add_argument("--xlsx", default=None)
    a = ap.parse_args()

    b = a.brand.lower()
    pat = re.compile(re.escape(b), re.I)
    lo, hi = "%s-01-01" % a.year, "%s-12-31" % a.year
    conn = sqlite3.connect("file:%s?mode=ro" % a.db, uri=True)
    rows = []

    # ---- Google: prose. Report the block containing the brand, plus matching lines.
    if has(conn, "google_change_csv_raw"):
        q = ("SELECT change_datetime, campaign_col, ad_group_col, changes_raw FROM google_change_csv_raw "
             "WHERE account_id=? AND substr(change_datetime,1,10) BETWEEN ? AND ? "
             "AND lower(changes_raw) LIKE ? ORDER BY change_datetime")
        for dt, camp, ag, raw in conn.execute(q, (a.google_account, lo, hi, "%%%s%%" % b)):
            for block, rec in zip(T.split_blocks(raw), T.categorize_cell(raw)):
                hits = [l.strip() for l in block.split("\n") if pat.search(l)]
                if not hits: continue
                rows.append({"platform":"google","date":dt[:10],"campaign":camp or "",
                             "ad_group":ag or "","category":rec["category"],"klass":rec["klass"],
                             "header":block.split("\n")[0].strip()[:70],
                             "matches":len(hits),"sample":" | ".join(hits[:4])[:150]})

    # ---- Microsoft: structured. Search item/attribute/old/new.
    if has(conn, "microsoft_change_grid_raw"):
        q = ("SELECT change_datetime, item_changed, attribute_changed, old_value, new_value "
             "FROM microsoft_change_grid_raw WHERE account_id=? "
             "AND substr(COALESCE(change_date,change_datetime),1,10) BETWEEN ? AND ? AND ("
             "lower(item_changed) LIKE ? OR lower(attribute_changed) LIKE ? "
             "OR lower(old_value) LIKE ? OR lower(new_value) LIKE ?) ORDER BY change_datetime")
        w = "%%%s%%" % b
        for dt, item, attr, old, new in conn.execute(q, (a.microsoft_account, lo, hi, w, w, w, w)):
            rows.append({"platform":"microsoft","date":(dt or "")[:10],"campaign":item or "",
                         "ad_group":"","category":"keyword_change" if "keyword" in (item or "").lower() else "other",
                         "klass":"modification","header":(attr or "")[:70],"matches":1,
                         "sample":("%s -> %s" % (old or "", new or ""))[:150]})

    conn.close()
    rows.sort(key=lambda r: (r["date"], r["platform"]))

    print("="*116)
    print("'%s' CHANGES IN %s  --  %d matching change blocks" % (a.brand.upper(), a.year, len(rows)))
    print("="*116)
    if not rows:
        print("(no matches -- the brand may pre-date ledger coverage, or the Microsoft ledger may not be loaded)")
        return
    for r in rows:
        print("\n%s  [%s]  %s  (%s / %s)" % (r["date"], r["platform"], r["campaign"][:44], r["category"], r["klass"]))
        print("    %s" % r["header"])
        print("    -> %s" % r["sample"])

    print("\n" + "="*116)
    print("=== first / last occurrence per platform ===")
    for p in ("google", "microsoft"):
        d = [r["date"] for r in rows if r["platform"] == p]
        print("  %-10s %s" % (p, ("first %s | last %s | %d blocks" % (d[0], d[-1], len(d))) if d else "(none)"))
    print("\n=== by category ===")
    for c, n in collections.Counter(r["category"] for r in rows).most_common(): print("  %-20s %d" % (c, n))
    print("\n=== by campaign ===")
    for c, n in collections.Counter(r["campaign"][:50] for r in rows).most_common(12): print("  %-52s %d" % (c, n))

    if a.xlsx:
        xlsx_out.write_xlsx(rows, a.xlsx,
            ["platform","date","campaign","ad_group","category","klass","header","matches","sample"],
            "Geico Changes")

    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8") as f:
            wr = csv.DictWriter(f, fieldnames=["platform","date","campaign","ad_group","category","klass","header","matches","sample"])
            wr.writeheader(); wr.writerows(rows)
        print("\n[ok] wrote %d rows -> %s" % (len(rows), a.csv))

if __name__ == "__main__":
    main()