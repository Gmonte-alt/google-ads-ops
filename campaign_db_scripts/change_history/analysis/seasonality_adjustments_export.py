"""
seasonality_adjustments_export.py -- every seasonality adjustment in the grid ledger,
parsed and lifecycle-reconstructed, for analysis.

Reads google_change_csv_raw (and google_change_csv_history_raw when present) for one or
both accounts, extracts 'Seasonality adjustment ...' blocks, parses name / action /
status / start / end / CvR adjustment / scope from the prose, and reconstructs each
adjustment's lifecycle (created -> updates -> removed). Unparsed lines are preserved
verbatim so nothing drops silently. Read-only; --xlsx writes the workbook.

    python seasonality_adjustments_export.py --db <db>                     # both accounts, 2026
    python seasonality_adjustments_export.py --db <db> --account 29
    python seasonality_adjustments_export.py --db <db> --since 2025-01-01  # include history table era
    python seasonality_adjustments_export.py --db <db> --xlsx <out.xlsx>
"""
import argparse
import re
import sqlite3

NAME_RX = re.compile(r'Seasonality adjustment (?:named\s+)?["\u201c]?(.+?)["\u201d]?\s+(created|updated|changed|removed)', re.I)
FIELD_RXS = {
    "status":    re.compile(r'Status (?:is|changed from .+? to)\s*:?\s*"?([A-Za-z]+)"?', re.I),
    "start":     re.compile(r'Start date (?:is|changed from .+? to)\s*:?\s*"?([^"\n]+?)"?\s*$', re.I | re.M),
    "end":       re.compile(r'End date (?:is|changed from .+? to)\s*:?\s*"?([^"\n]+?)"?\s*$', re.I | re.M),
    "cvr_adj":   re.compile(r'Conversion rate adjustment (?:is|changed from .+? to)\s*:?\s*"?([+\-]?\d+(?:\.\d+)?%)"?', re.I),
    "scope":     re.compile(r'Scope (?:is|changed from .+? to)\s*:?\s*"?([^"\n]+?)"?\s*$', re.I | re.M),
    "applies":   re.compile(r'Applies to\s*"?([^"\n]+?)"?\s*(?:specific campaigns|campaign types)', re.I),
}


def user_col(conn, table):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]
    return next((c for c in cols if "user" in c.lower()), None)


def fetch(conn, table, acct, since):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return []
    uc = user_col(conn, table)
    q = ("SELECT change_datetime, %s AS u, changes_raw FROM %s "
         "WHERE account_id=? AND change_datetime>=? AND changes_raw LIKE '%%Seasonality adjustment%%' "
         "ORDER BY change_datetime" % (uc if uc else "''", table))
    return [dict(r) for r in conn.execute(q, (acct, since))]


def parse(block):
    m = NAME_RX.search(block)
    rec = {"name": m.group(1).strip() if m else None,
           "action": m.group(2).lower() if m else None}
    for k, rx in FIELD_RXS.items():
        fm = rx.search(block)
        rec[k] = fm.group(1).strip() if fm else None
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--account", type=int, default=None, help="default: both 1 and 29")
    ap.add_argument("--since", default="2026-01-01")
    ap.add_argument("--xlsx", default=None)
    args = ap.parse_args()

    conn = sqlite3.connect("file:%s?mode=ro" % args.db, uri=True)
    conn.row_factory = sqlite3.Row
    accounts = [args.account] if args.account else [1, 29]

    events = []   # (account, dt, user, name, action, fields..., raw)
    for acct in accounts:
        rows = fetch(conn, "google_change_csv_raw", acct, args.since) + \
               fetch(conn, "google_change_csv_history_raw", acct, args.since)
        seen = set()
        for r in sorted(rows, key=lambda x: x["change_datetime"]):
            key = (r["change_datetime"], r["changes_raw"])
            if key in seen:
                continue          # grid double-logging
            seen.add(key)
            p = parse(r["changes_raw"])
            events.append({"account": acct, "dt": r["change_datetime"], "user": r["u"] or "",
                           "raw": r["changes_raw"].strip(), **p})

    print("%d seasonality events since %s (accounts %s)" % (
        len(events), args.since, ",".join(str(a) for a in accounts)))
    unparsed = [e for e in events if not e["name"]]
    if unparsed:
        print("[WARN] %d event(s) did not parse a name -- shown verbatim below" % len(unparsed))

    # lifecycle per (account, name)
    life = {}
    for e in events:
        if not e["name"]:
            continue
        k = (e["account"], e["name"])
        L = life.setdefault(k, {"created": None, "removed": None, "last": {}, "users": set(),
                                "n_events": 0, "cvr_values": []})
        L["n_events"] += 1
        L["users"].add(e["user"])
        if e["action"] == "created":
            L["created"] = e["dt"]
        if e["status"] and e["status"].lower() == "removed":
            L["removed"] = e["dt"]
        for f in ("start", "end", "cvr_adj", "scope", "applies", "status"):
            if e[f]:
                L["last"][f] = e[f]
        if e["cvr_adj"] and e["cvr_adj"] not in L["cvr_values"]:
            L["cvr_values"].append(e["cvr_adj"])

    for acct in accounts:
        names = [k for k in life if k[0] == acct]
        print("\n" + "=" * 78)
        print("account %d: %d adjustment(s)" % (acct, len(names)))
        print("=" * 78)
        for k in sorted(names, key=lambda k: life[k]["created"] or ""):
            L = life[k]
            print("\n  [%s]  %d event(s) | user(s): %s" % (k[1], L["n_events"], ", ".join(sorted(u for u in L["users"] if u))))
            print("    created: %s   removed: %s" % (L["created"] or "?", L["removed"] or "no (ran/runs to end date)"))
            print("    CvR adjustment value(s): %s" % (", ".join(L["cvr_values"]) or "?"))
            print("    last-known window: %s  ->  %s" % (L["last"].get("start", "?"), L["last"].get("end", "?")))
            print("    scope: %s | applies to: %s" % (L["last"].get("scope", "?"), L["last"].get("applies", "?")))
        # chronological event log
        print("\n  event log:")
        for e in [e for e in events if e["account"] == acct]:
            bits = "; ".join("%s=%s" % (f, e[f]) for f in ("status", "start", "end", "cvr_adj", "scope", "applies") if e[f])
            print("    %s  %-18s %-8s %s  [%s]" % (e["dt"], (e["name"] or "<unparsed>")[:18],
                                                   e["action"] or "?", bits[:90], e["user"].split("@")[0]))
    for e in unparsed:
        print("\n[unparsed block] %s account %d\n%s" % (e["dt"], e["account"], e["raw"][:400]))

    if args.xlsx:
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active; ws.title = "Events"
        hdr = ["account", "datetime", "user", "adjustment", "action", "status", "start",
               "end", "cvr_adjustment", "scope", "applies_to", "raw_prose"]
        ws.append(hdr)
        for e in events:
            ws.append([e["account"], e["dt"], e["user"], e["name"], e["action"], e["status"],
                       e["start"], e["end"], e["cvr_adj"], e["scope"], e["applies"], e["raw"][:1000]])
        ws2 = wb.create_sheet("Adjustments")
        ws2.append(["account", "adjustment", "created", "removed_early", "last_start", "last_end",
                    "cvr_values", "scope", "applies_to", "events", "users"])
        for (acct, name), L in sorted(life.items()):
            ws2.append([acct, name, L["created"], L["removed"], L["last"].get("start"),
                        L["last"].get("end"), ", ".join(L["cvr_values"]), L["last"].get("scope"),
                        L["last"].get("applies"), L["n_events"],
                        ", ".join(sorted(u for u in L["users"] if u))])
        wb.save(args.xlsx)
        print("\n[ok] wrote %s (Events: %d rows; Adjustments: %d)" % (args.xlsx, len(events), len(life)))
    conn.close()


if __name__ == "__main__":
    main()
