"""
geico_rsa_diff_export.py -- Extract the actual BEFORE -> AFTER copy pairs from Geico RSA
change blocks in a window, deduplicated across campaigns.

Why: the change export truncates prose; RSA blocks carry full 'Headline was "X" / Headline is "Y"'
(and Description was/is) lines. Thousands of blocks across state campaigns are usually the SAME
few rewrites replicated -- so the useful output is distinct (field, old, new) pairs with counts,
not thousands of truncated rows.

Asset IDs are stripped from the text for dedup (the same copy carries different Asset IDs per
campaign) but kept in the raw sheet.

    python geico_rsa_diff_export.py --db PATH --start 2025-07-01 --end 2025-07-31 --out ./output
"""
import argparse
import collections
import os
import re
import sqlite3
import sys

CHANGE_HISTORY_DIR = os.environ.get("CHANGE_HISTORY_DIR",
                                    r"C:\Projects\google-ads-ops\campaign_db_scripts\change_history")
ACCOUNT_ID = 1

_LINE = re.compile(r'^\s*(Headline|Description|Path ?[12]?|Final URL)\s+(was|is)\s+"?(.*?)"?\s*$',
                   re.I | re.M)
_ASSET_ID = re.compile(r'\s*\(Asset ID:\s*\d+\)\s*', re.I)


def strip_asset(text):
    return _ASSET_ID.sub("", text or "").strip().strip('"')


def extract_pairs(block):
    """Yield (field, old_text, new_text) pairs from one RSA change block.
    Google logs 'X was ...' then 'X is ...' in sequence; pair them in order per field."""
    was_q = collections.defaultdict(list)
    is_q = collections.defaultdict(list)
    for m in _LINE.finditer(block):
        field = m.group(1).title().replace(" ", "")
        (was_q if m.group(2).lower() == "was" else is_q)[field].append(m.group(3))
    for field in set(list(was_q) + list(is_q)):
        w, i = was_q[field], is_q[field]
        for k in range(max(len(w), len(i))):
            old = strip_asset(w[k]) if k < len(w) else ""
            new = strip_asset(i[k]) if k < len(i) else ""
            if old or new:
                yield field, old, new


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--brand", default="Geico")
    ap.add_argument("--start", default="2025-07-01")
    ap.add_argument("--end", default="2025-07-31")
    ap.add_argument("--account", type=int, default=ACCOUNT_ID)
    ap.add_argument("--out", default=".")
    args = ap.parse_args()

    if CHANGE_HISTORY_DIR not in sys.path:
        sys.path.insert(0, CHANGE_HISTORY_DIR)
    import change_taxonomy as T

    b = args.brand.lower()
    like = "%" + b + "%"
    conn = sqlite3.connect(args.db)
    # The historical ledger covers 2024-08..2025-12; 2026 changes live in the weekly ledger
    # (google_change_csv_raw). Read BOTH so any window works.
    def rows():
        for table in ("google_change_csv_history_raw", "google_change_csv_raw"):
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                (table,)).fetchone():
                continue
            q = ("SELECT change_datetime, campaign_col, ad_group_col, changes_raw "
                 "FROM %s WHERE account_id=? AND lower(ad_group_col) LIKE ? "
                 "AND substr(change_datetime,1,10) BETWEEN ? AND ?" % table)
            for r in conn.execute(q, (args.account, like, args.start, args.end)):
                yield r
    agg = collections.defaultdict(lambda: {"n": 0, "campaigns": set(), "first": None, "last": None})
    raw_rows = []
    n_blocks = 0
    for dt, camp, ag, raw in rows():
        d = (dt or "")[:10]
        for block, rec in zip(T.split_blocks(raw), T.categorize_cell(raw)):
            if rec["category"] not in ("rsa_change", "rsa_new"):
                continue
            n_blocks += 1
            for field, old, new in extract_pairs(block):
                key = (field, old, new)
                a = agg[key]
                a["n"] += 1
                a["campaigns"].add(camp or "")
                a["first"] = d if a["first"] is None or d < a["first"] else a["first"]
                a["last"] = d if a["last"] is None or d > a["last"] else a["last"]
                raw_rows.append({"date": d, "campaign": camp or "", "ad_group": ag or "",
                                 "field": field, "old_text": old, "new_text": new})
    conn.close()

    pairs = [{"field": f, "old_text": o, "new_text": n, "n_blocks": a["n"],
              "n_campaigns": len(a["campaigns"]), "first": a["first"], "last": a["last"]}
             for (f, o, n), a in agg.items()]
    pairs.sort(key=lambda r: -r["n_blocks"])

    print("%d RSA blocks scanned in %s..%s -> %d distinct (field, old->new) pairs"
          % (n_blocks, args.start, args.end, len(pairs)))
    geico_mentions_old = sum(p["n_blocks"] for p in pairs if b in p["old_text"].lower())
    geico_mentions_new = sum(p["n_blocks"] for p in pairs if b in p["new_text"].lower())
    print("brand-in-copy check: %r appears in OLD text of %d pair-instances, NEW text of %d"
          % (args.brand, geico_mentions_old, geico_mentions_new))
    print("\n=== top rewrites ===")
    for p in pairs[:20]:
        print("  [%s x%d, %d camps, %s..%s]" % (p["field"], p["n_blocks"], p["n_campaigns"],
                                                p["first"], p["last"]))
        print("    was: %s" % (p["old_text"] or "(new)"))
        print("    is : %s" % (p["new_text"] or "(removed)"))

    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "%s_rsa_diffs_%s_to_%s.xlsx" % (args.brand.lower(), args.start, args.end))
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        from openpyxl.utils import get_column_letter
        wb = Workbook()
        s = wb.active; s.title = "Distinct rewrites"
        hdr = ["field", "old_text", "new_text", "n_blocks", "n_campaigns", "first", "last"]
        for j, h in enumerate(hdr, 1):
            c = s.cell(row=1, column=j, value=h.upper())
            c.font = Font(bold=True, color="FFFFFF"); c.fill = PatternFill("solid", fgColor="1F4E79")
        for r, p in enumerate(pairs, start=2):
            for j, k in enumerate(hdr, 1):
                s.cell(row=r, column=j, value=p[k])
        for j, w in enumerate([12, 70, 70, 10, 12, 12, 12], 1):
            s.column_dimensions[get_column_letter(j)].width = w
        s.freeze_panes = "A2"; s.auto_filter.ref = s.dimensions
        d = wb.create_sheet("All pair instances")
        dh = ["date", "campaign", "ad_group", "field", "old_text", "new_text"]
        for j, h in enumerate(dh, 1):
            c = d.cell(row=1, column=j, value=h.upper())
            c.font = Font(bold=True, color="FFFFFF"); c.fill = PatternFill("solid", fgColor="1F4E79")
        for r, row in enumerate(raw_rows, start=2):
            for j, k in enumerate(dh, 1):
                d.cell(row=r, column=j, value=row[k])
        for j, w in enumerate([12, 40, 34, 12, 60, 60], 1):
            d.column_dimensions[get_column_letter(j)].width = w
        d.freeze_panes = "A2"; d.auto_filter.ref = d.dimensions
        wb.save(path)
        print("\n[ok] wrote %s" % path)
    except ImportError:
        print("[!] openpyxl not installed; skipped xlsx")


if __name__ == "__main__":
    main()
