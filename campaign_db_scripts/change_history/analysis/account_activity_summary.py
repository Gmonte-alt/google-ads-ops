"""
account_activity_summary.py -- What happened in an account over a window, from the change ledgers.

Three sections:
  1) UI-grid ledger (google_change_csv_raw) categorized via change_taxonomy:
     category/action rollup (#blocks, #items, #campaigns) + most-touched campaigns
  2) Derived campaign events (campaign_change_events): tROAS + budget changes, old -> new
  3) API ledger (google_change_event_raw): counts by resource type

    python account_activity_summary.py --db <db> --account 29 --days 30
"""
import argparse
import collections
import datetime
import os
import sqlite3
import sys

CHANGE_HISTORY_DIR = os.environ.get("CHANGE_HISTORY_DIR",
                                    r"C:\Projects\google-ads-ops\campaign_db_scripts\change_history")


def pick(cols, *cands):
    for c in cands:
        if c in cols:
            return c
    return None


def cols_of(conn, table):
    return [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--account", type=int, default=29)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--end", default=datetime.date.today().isoformat())
    ap.add_argument("--show-category", default=None,
                    help="dump the raw prose of blocks in this category (e.g. other)")
    ap.add_argument("--limit", type=int, default=25, help="max blocks to dump with --show-category")
    args = ap.parse_args()
    start = (datetime.date.fromisoformat(args.end)
             - datetime.timedelta(days=args.days)).isoformat()

    if CHANGE_HISTORY_DIR not in sys.path:
        sys.path.insert(0, CHANGE_HISTORY_DIR)
    import change_taxonomy as T

    conn = sqlite3.connect("file:%s?mode=ro" % args.db, uri=True)
    print("account %d activity, %s .. %s" % (args.account, start, args.end))

    # ---- 1) UI-grid ledger, categorized (with user attribution) ----
    grid_cols = cols_of(conn, "google_change_csv_raw")
    u_col = pick(grid_cols, "user_col", "user", "user_email", "changed_by")
    sel_user = (", %s" % u_col) if u_col else ", NULL"
    # live status per campaign name, for annotating the rollup (current names only)
    camp_cols = cols_of(conn, "campaigns")
    s_col = pick(camp_cols, "campaign_status", "status")
    status_by_name = {}
    if s_col:
        for nm, st in conn.execute(
                "SELECT campaign_name, %s FROM campaigns WHERE account_id=?" % s_col,
                (args.account,)):
            status_by_name[nm or ""] = st or "?"
    roll = collections.defaultdict(lambda: {"blocks": 0, "items": 0, "camps": set()})
    camp_touch = collections.Counter()
    user_touch = collections.Counter()
    user_cat = collections.defaultdict(collections.Counter)
    n_rows = 0
    shown = []
    for dt, camp, raw, usr in conn.execute(
            "SELECT change_datetime, campaign_col, changes_raw%s FROM google_change_csv_raw "
            "WHERE account_id=? AND substr(change_datetime,1,10) BETWEEN ? AND ?" % sel_user,
            (args.account, start, args.end)):
        n_rows += 1
        camp_touch[camp or "(account-level)"] += 1
        usr = usr or "(unknown)"
        for rec in T.categorize_cell(raw):
            key = (rec.get("category", "?"), rec.get("action", "?"))
            r = roll[key]
            r["blocks"] += 1
            r["items"] += rec.get("count", 1) or 1
            r["camps"].add(camp or "")
            user_touch[usr] += 1
            user_cat[usr][rec.get("category", "?")] += 1
            if args.show_category and rec.get("category") == args.show_category \
                    and len(shown) < args.limit:
                shown.append((dt, usr, camp or "", raw))
    print("\n=== 1) UI-grid ledger: %d rows ===" % n_rows)
    if roll:
        print("%-24s %-10s %8s %8s %7s" % ("CATEGORY", "ACTION", "blocks", "items", "camps"))
        for (cat, act), r in sorted(roll.items(), key=lambda kv: -kv[1]["blocks"]):
            print("%-24s %-10s %8d %8d %7d" % (cat, act, r["blocks"], r["items"], len(r["camps"])))
        if u_col:
            print("\nby user (blocks; top categories):")
            for usr, n in user_touch.most_common(10):
                cats = ", ".join("%s x%d" % (c, k) for c, k in user_cat[usr].most_common(3))
                print("  %6d  %-40s %s" % (n, (usr or "")[:40], cats))
        else:
            print("\n[note] no user column found in google_change_csv_raw "
                  "(columns: %s) -- user attribution needs a loader patch" % grid_cols)
        print("\nmost-touched campaigns%s:" % (" (with live status)" if status_by_name else ""))
        for camp, n in camp_touch.most_common(10):
            st = status_by_name.get(camp or "", "?") if status_by_name else ""
            print("  %4d  %-62s %s" % (n, (camp or "")[:62], st))
    if args.show_category:
        print("\n=== raw blocks in category '%s' (%d shown) ===" % (args.show_category, len(shown)))
        for dt, usr, camp, raw in shown:
            print("\n[%s] %s | %s" % (dt, usr, camp[:60]))
            for line in (raw or "").splitlines():
                print("    " + line[:150])

    # ---- 2) derived campaign events (tROAS / budget, old -> new) ----
    ev_cols = cols_of(conn, "campaign_change_events")
    if ev_cols:
        c_date = pick(ev_cols, "change_date", "event_date", "date")
        c_type = pick(ev_cols, "change_type", "attribute", "type")
        c_old, c_new = pick(ev_cols, "old_value", "old"), pick(ev_cols, "new_value", "new")
        c_acct = pick(ev_cols, "account_id")
        q = ("SELECT e.%s, e.%s, c.campaign_name, e.%s, e.%s FROM campaign_change_events e "
             "LEFT JOIN campaigns c ON c.campaign_id=e.campaign_id AND c.account_id=%d "
             "WHERE e.%s=%d AND e.%s BETWEEN ? AND ? ORDER BY e.%s"
             % (c_date, c_type, c_old, c_new, args.account, c_acct, args.account, c_date, c_date))
        rows = [r for r in conn.execute(q, (start, args.end))]
        print("\n=== 2) derived campaign events (tROAS/budget/rename): %d ===" % len(rows))
        for d, t, cname, old, new in rows:
            if old is None:
                continue  # timeline anchors
            print("  %s  %-12s %-52s %s -> %s" % (d, t, (cname or "")[:52], old, new))

    # ---- 3) API ledger by resource type ----
    api_cols = cols_of(conn, "google_change_event_raw")
    if api_cols:
        a_dt = pick(api_cols, "change_datetime", "change_date_time", "event_datetime")
        a_rt = pick(api_cols, "change_resource_type", "resource_type")
        a_op = pick(api_cols, "resource_change_operation", "operation")
        a_user = pick(api_cols, "user_email", "changed_by", "user_col", "user")
        a_acct = pick(api_cols, "account_id")
        if a_dt and a_rt:
            sel_op = (", %s" % a_op) if a_op else ""
            q = ("SELECT %s%s, COUNT(*) FROM google_change_event_raw WHERE %s=%d "
                 "AND substr(%s,1,10) BETWEEN ? AND ? GROUP BY 1%s ORDER BY COUNT(*) DESC"
                 % (a_rt, sel_op, a_acct, args.account, a_dt, ", 2" if a_op else ""))
            rows = [r for r in conn.execute(q, (start, args.end))]
            print("\n=== 3) API ledger by resource type ===")
            for r in rows:
                print("  " + " | ".join(str(x) for x in r))
            if a_user:
                print("\napi ledger by user:")
                qu = ("SELECT %s, COUNT(*) FROM google_change_event_raw WHERE %s=%d "
                      "AND substr(%s,1,10) BETWEEN ? AND ? GROUP BY 1 ORDER BY COUNT(*) DESC"
                      % (a_user, a_acct, args.account, a_dt))
                for r in conn.execute(qu, (start, args.end)):
                    print("  %6d  %s" % (r[1], r[0]))
    conn.close()


if __name__ == "__main__":
    main()
