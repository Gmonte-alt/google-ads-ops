"""
troas_changes_export.py -- Export campaign tROAS changes for a window (default: this year)
from the derived change-history model (campaign_change_events), rename-aware by campaign_id.

Output: terminal table + xlsx with two sheets:
  * "tROAS changes" -- one row per change: date, campaign, old -> new, delta
  * "By campaign"   -- per-campaign summary: #changes, first/last change, starting -> current target

Timeline-start anchors (old_value NULL -- the model's opening markers, not real changes) are
excluded from the change list by default and counted in the footer; --include-anchors keeps them.

    python troas_changes_export.py --db <path-to-db> --out <out-dir>
    python troas_changes_export.py --db <path-to-db> --start 2026-01-01 --end 2026-12-31
    python C:\Projects\google-ads-ops\campaign_db_scripts\change_history\analysis\troas_changes_export.py --db C:\Projects\google-ads-ops\db\paid_search.db --account 29 --out c:\Projects\google-ads-ops\campaign_db_scripts\change_history\analysis\output
"""
import argparse
import collections
import datetime
import os
import sqlite3

ACCOUNT_ID = 1


def pick(cols, *candidates):
    for c in candidates:
        if c in cols:
            return c
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default=datetime.date.today().isoformat())
    ap.add_argument("--account", type=int, default=ACCOUNT_ID)
    ap.add_argument("--out", default=".")
    ap.add_argument("--include-anchors", action="store_true",
                    help="include timeline-start rows (old_value NULL)")
    args = ap.parse_args()

    conn = sqlite3.connect("file:%s?mode=ro" % args.db, uri=True)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(campaign_change_events)")]
    if not cols:
        raise SystemExit("campaign_change_events not found -- run 17 first")
    c_date = pick(cols, "change_date", "event_date", "date")
    c_type = pick(cols, "change_type", "attribute", "type")
    c_camp = pick(cols, "campaign_id")
    c_old = pick(cols, "old_value", "old")
    c_new = pick(cols, "new_value", "new")
    c_acct = pick(cols, "account_id")
    if not all([c_date, c_type, c_camp, c_old, c_new]):
        raise SystemExit("unexpected campaign_change_events schema: %s" % cols)

    acct_clause = (" AND e.%s=%d" % (c_acct, args.account)) if c_acct else ""
    q = ("SELECT e.%s, e.%s, c.campaign_name, e.%s, e.%s "
         "FROM campaign_change_events e "
         "LEFT JOIN campaigns c ON c.campaign_id = e.%s AND c.account_id = %d "
         "WHERE upper(e.%s) LIKE '%%ROAS%%' AND e.%s BETWEEN ? AND ?%s "
         "ORDER BY e.%s, c.campaign_name"
         % (c_date, c_camp, c_old, c_new, c_camp, args.account,
            c_type, c_date, acct_clause, c_date))

    rows, anchors = [], 0
    for d, cid, cname, old, new in conn.execute(q, (args.start, args.end)):
        if old is None:
            anchors += 1
            if not args.include_anchors:
                continue
        try:
            delta = round(float(new) - float(old), 4) if old is not None and new is not None else None
        except (TypeError, ValueError):
            delta = None
        rows.append({"date": d, "campaign_id": str(cid), "campaign": cname or "(id %s)" % cid,
                     "old": old, "new": new, "delta": delta})
    conn.close()

    print("tROAS changes %s .. %s : %d changes (%d timeline-start anchors %s)"
          % (args.start, args.end, len(rows), anchors,
             "included" if args.include_anchors else "excluded"))
    print("%-11s %-46s %8s %8s %8s" % ("DATE", "CAMPAIGN", "OLD", "NEW", "DELTA"))
    for r in rows:
        print("%-11s %-46s %8s %8s %8s" % (r["date"], (r["campaign"] or "")[:46],
                                           r["old"], r["new"],
                                           "" if r["delta"] is None else ("%+g" % r["delta"])))

    by_camp = collections.defaultdict(list)
    for r in rows:
        by_camp[r["campaign"]].append(r)
    summary = []
    for cname in sorted(by_camp):
        rs = sorted(by_camp[cname], key=lambda x: x["date"])
        summary.append({"campaign": cname, "n_changes": len(rs),
                        "first_change": rs[0]["date"], "last_change": rs[-1]["date"],
                        "start_target": rs[0]["old"], "current_target": rs[-1]["new"]})
    print("\nBy campaign:")
    for s in summary:
        print("  %-46s %2d changes | %s .. %s | %s -> %s"
              % (s["campaign"][:46], s["n_changes"], s["first_change"], s["last_change"],
                 s["start_target"], s["current_target"]))

    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "troas_changes_%s_to_%s.xlsx" % (args.start, args.end))
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        from openpyxl.utils import get_column_letter
        wb = Workbook()
        s1 = wb.active; s1.title = "tROAS changes"
        hdr = ["date", "campaign", "campaign_id", "old", "new", "delta"]
        for j, h in enumerate(hdr, 1):
            cell = s1.cell(row=1, column=j, value=h.upper())
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1F4E79")
        for i, r in enumerate(rows, start=2):
            for j, k in enumerate(hdr, 1):
                s1.cell(row=i, column=j, value=r[k])
        for j, w in enumerate([12, 50, 16, 8, 8, 8], 1):
            s1.column_dimensions[get_column_letter(j)].width = w
        s1.freeze_panes = "A2"; s1.auto_filter.ref = s1.dimensions
        s2 = wb.create_sheet("By campaign")
        hdr2 = ["campaign", "n_changes", "first_change", "last_change", "start_target", "current_target"]
        for j, h in enumerate(hdr2, 1):
            cell = s2.cell(row=1, column=j, value=h.upper())
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1F4E79")
        for i, s in enumerate(summary, start=2):
            for j, k in enumerate(hdr2, 1):
                s2.cell(row=i, column=j, value=s[k])
        for j, w in enumerate([50, 10, 12, 12, 12, 14], 1):
            s2.column_dimensions[get_column_letter(j)].width = w
        s2.freeze_panes = "A2"
        wb.save(path)
        print("\n[ok] wrote %s" % path)
    except ImportError:
        print("[!] openpyxl not installed; terminal output only")


if __name__ == "__main__":
    main()
