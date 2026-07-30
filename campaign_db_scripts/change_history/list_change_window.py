"""list_changes_window.py -- READ-ONLY. Lists categorized changes in a date window,
across BOTH ledgers (Google prose + Microsoft structured).
    python list_changes_window.py                                  # 2026-06-01..2026-06-08
    python list_changes_window.py --start 2026-06-01 --end 2026-06-08 --csv out.csv
"""
import argparse, collections, csv, sqlite3
import change_taxonomy as T
from xlsx_out import write_xlsx
import re

DEFAULT_DB = r"C:\Projects\google-ads-ops\db\paid_search.db"

_FROM_TO = re.compile(r"changed from\s+(.+?)\s+to\s+(.+?)\s*$", re.I | re.M)
_NAMED_TO = re.compile(r'named\s+"(.+?)"\s+to\s+"(.+?)"', re.I)

def parse_from_to(text):
    """Pull old/new out of Google prose: 'Target ROAS changed from 110% to 113%',
    'Status changed from enabled to paused', 'moved from bid strategy named "X" to "Y"'."""
    m = _FROM_TO.search(text or "")
    if m:
        return m.group(1).strip(), m.group(2).strip()
    m = _NAMED_TO.search(text or "")
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return "", ""

def has(conn, t):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone() is not None

def coverage(conn, table, datecol):
    if not has(conn, table): return "(missing)"
    r = conn.execute("SELECT MIN(substr(%s,1,10)), MAX(substr(%s,1,10)), COUNT(*) FROM %s" % (datecol, datecol, table)).fetchone()
    return "%s .. %s (%d rows)" % (r[0], r[1], r[2])

def google_rows(conn, start, end, acct):
    if not has(conn, "google_change_csv_raw"): return []
    q = ("SELECT change_datetime, campaign_col, ad_group_col, changes_raw FROM google_change_csv_raw "
         "WHERE account_id=? AND substr(change_datetime,1,10) BETWEEN ? AND ? ORDER BY change_datetime")
    out = []
    for dt, camp, ag, raw in conn.execute(q, (acct, start, end)):
        for block, rec in zip(T.split_blocks(raw), T.categorize_cell(raw)):
            lines = block.split("\n")
            header = lines[0].strip()
            specifics = " | ".join(l.strip() for l in lines[1:] if l.strip())
            old, new = parse_from_to(block)
            out.append({
                "platform": "google", "date": (dt or "")[:10], "time": (dt or "")[11:19],
                "campaign": camp or "", "ad_group": ag or "",
                "category": rec["category"], "klass": rec["klass"], "count": rec["count"],
                "detail": header[:90],
                "old_value": old[:120], "new_value": new[:120],
                "specifics": specifics[:400],
            })
    return out

def ms_rows(conn, start, end, acct):
    if not has(conn, "microsoft_change_grid_raw"): return []
    q = ("SELECT change_date, item_changed, attribute_changed, old_value, new_value "
         "FROM microsoft_change_grid_raw WHERE account_id=? "
         "AND change_date BETWEEN ? AND ? ORDER BY change_date")
    out = []
    for dt, item, attr, old, new in conn.execute(q, (acct, start, end)):
        a, i = (attr or "").lower(), (item or "").lower()
        a = (attr or "").strip()
        al = a.lower()
        item = item or ""
        is_campaign_entity = item.upper().startswith(("HOME INSURANCE", "RENTERS INSURANCE"))

        if al == "location":
            cat, kl = "geo_retiering", "restructure"
        elif al == "campaignname":
            cat, kl = "campaign_rename", "restructure"
        elif "budget" in al:
            cat, kl = "budget_change", "modification"
        elif "targetcpa" in al or "targetroas" in al or "roas" in al:
            cat, kl = "bid_target_campaign", "modification"
        elif "bidstrategy" in al or "bidding" in al:
            cat, kl = "bid_strategy_swap", "restructure"
        elif al in ("headline", "description", "path1", "path2", "finalurl") or "adasset" in al:
            cat, kl = "rsa_change", "modification"
        elif al == "negativekeyword":
            cat, kl = "negative_keyword", "modification"
        elif al == "association":
            cat, kl = "negative_list", "modification"
        elif "trackingtemplate" in al:
            cat, kl = "tracking_template", "modification"
        elif "editorialstatus" in al:
            cat, kl = "editorial_status", "modification"
        elif "status" in al:                      # PauseStatusId / LifeCycleStatusId
            cat, kl = ("campaign_status", "restructure") if is_campaign_entity else ("entity_status", "modification")
        elif not a and (new or ""):               # blank attribute + a value = keyword added
            cat, kl = "keyword_add", "modification"
        elif "network" in al:
            cat, kl = "network_optin", "modification"
        else:
            cat, kl = "other", "modification"
        out.append({
            "platform": "microsoft", "date": (dt or "")[:10], "time": "",
            "campaign": item or "", "ad_group": "",
            "category": cat, "klass": kl, "count": 1,
            "detail": (attr or "")[:90],
            "old_value": (old or "")[:120], "new_value": (new or "")[:120],
            "specifics": "",
        })
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--start", default="2026-06-01")
    ap.add_argument("--end", default="2026-06-08")
    ap.add_argument("--google-account", type=int, default=1)
    ap.add_argument("--microsoft-account", type=int, default=2)
    ap.add_argument("--csv", default=None)
    ap.add_argument("--xlsx", default=None)
    a = ap.parse_args()

    conn = sqlite3.connect("file:%s?mode=ro" % a.db, uri=True)
    print("ledger coverage:")
    print("  google_change_csv_raw    ", coverage(conn, "google_change_csv_raw", "change_datetime"))
    print("  microsoft_change_grid_raw", coverage(conn, "microsoft_change_grid_raw", "change_date"))
    rows = google_rows(conn, a.start, a.end, a.google_account) + ms_rows(conn, a.start, a.end, a.microsoft_account)
    conn.close()

    rows.sort(key=lambda r: (r["date"], r["time"], r["platform"]))
    print("\n" + "="*112)
    print("CHANGES %s .. %s   (%d records)" % (a.start, a.end, len(rows)))
    print("="*112)
    if not rows:
        print("(none in window -- check coverage above)"); return
    print("%-10s %-9s %-19s %-12s %-26s %s" % ("DATE","PLATFORM","CATEGORY","CLASS","CAMPAIGN","CHANGE"))
    print("-" * 130)
    for r in rows:
        chg = ("%s: %s -> %s" % (r["detail"], r["old_value"] or "(none)", r["new_value"])
               if r["new_value"] or r["old_value"] else
               ("%s | %s" % (r["detail"], r["specifics"]) if r["specifics"] else r["detail"]))
        print("%-10s %-9s %-19s %-12s %-26s %s" % (
            r["date"], r["platform"], r["category"], r["klass"][:12], r["campaign"][:26], chg[:64]))

    cat, items = collections.Counter(), collections.Counter()
    for r in rows: cat[r["category"]] += 1; items[r["category"]] += r["count"]
    print("\n=== by category (records | items) ===")
    for c, n in cat.most_common(): print("  %-22s %3d | %d" % (c, n, items[c]))
    print("\n=== by class (headline) ===")
    for k, n in collections.Counter(r["klass"] for r in rows).most_common(): print("  %-14s %d" % (k, n))
    print("\n=== by platform ===")
    for p, n in collections.Counter(r["platform"] for r in rows).most_common(): print("  %-10s %d" % (p, n))

    if a.xlsx:
        write_xlsx(rows, a.xlsx,
                   ["platform","date","time","campaign","ad_group","category","klass","count",
                    "detail","old_value","new_value","specifics"],
                   "Changes")

    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["platform","date","time","campaign","ad_group","category","klass","count","detail","old_value","new_value","specifics"])
            w.writeheader(); w.writerows(rows)
        print("\n[ok] wrote %d rows -> %s" % (len(rows), a.csv))

if __name__ == "__main__":
    main()