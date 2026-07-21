#!/usr/bin/env python
"""
inspect_keyword_model.py -- read-only inventory of the keyword data model in paid_search.db.

Shows, for each keyword-related table: column list, row count (account_id=1 where applicable),
distinct criterion_id count, and duplication ratio. Also cross-checks which tables can serve as
the authoritative "current live keyword structure" source vs. the performance source.

Read-only (mode=ro). Safe to run anytime. No writes.

    python inspect_keyword_model.py
"""
import sqlite3

DB = r"C:\Projects\google-ads-ops\db\paid_search.db"
ACCOUNT = 1

TABLES = [
    "keyword_instances",
    "keyword_performance_daily",
    "keyword_identities",
    "keyword_performance_network_daily",
    "campaigns",
    "ad_groups",
]


def main():
    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    c = conn.cursor()

    print("=" * 70)
    print("KEYWORD DATA MODEL INVENTORY  (account_id=%d)" % ACCOUNT)
    print("=" * 70)

    for t in TABLES:
        # does the table exist?
        exists = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
        ).fetchone()
        if not exists:
            print("\n%s -- NOT FOUND" % t)
            continue

        cols = [r[1] for r in c.execute("PRAGMA table_info(%s)" % t)]
        has_acct = "account_id" in cols
        has_crit = "criterion_id" in cols
        has_status = "status" in cols

        # row count (scoped to account if possible)
        if has_acct:
            n = c.execute(
                "SELECT COUNT(*) FROM %s WHERE account_id=?" % t, (ACCOUNT,)
            ).fetchone()[0]
        else:
            n = c.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]

        print("\n" + "-" * 70)
        print("%s   (%s rows%s)" % (t, f"{n:,}", " acct1" if has_acct else " total"))
        print("  columns: %s" % cols)

        # duplication check where criterion_id exists
        if has_crit:
            where = "WHERE account_id=%d" % ACCOUNT if has_acct else ""
            distinct = c.execute(
                "SELECT COUNT(DISTINCT criterion_id) FROM %s %s" % (t, where)
            ).fetchone()[0]
            ratio = (n / distinct) if distinct else 0
            print("  distinct criterion_id: %s   (avg %.1f rows per criterion_id)"
                  % (f"{distinct:,}", ratio))
            if ratio > 1.5:
                print("  ^^ DUPLICATED: this table has ~%.0fx more rows than placements "
                      "-- a sync insert-vs-upsert issue." % ratio)

        # status distribution where present
        if has_status:
            where = "WHERE account_id=%d" % ACCOUNT if has_acct else ""
            dist = c.execute(
                "SELECT status, COUNT(*) FROM %s %s GROUP BY status" % (t, where)
            ).fetchall()
            print("  status distribution: %s" % dict(dist))

        # date coverage for performance tables
        if "date" in cols or "report_date" in cols:
            datecol = "date" if "date" in cols else "report_date"
            where = "WHERE account_id=%d" % ACCOUNT if has_acct else ""
            rng = c.execute(
                "SELECT MIN(%s), MAX(%s) FROM %s %s" % (datecol, datecol, t, where)
            ).fetchone()
            print("  %s range: %s .. %s" % (datecol, rng[0], rng[1]))

    # -------- cross-check: can performance data reveal "truly serving"? --------
    print("\n" + "=" * 70)
    print("CROSS-CHECK: recent-serving keywords (ground truth for 'live')")
    print("=" * 70)
    # keywords with impressions in the last 14 days = provably served
    try:
        recent = c.execute(
            "SELECT COUNT(DISTINCT criterion_id) FROM keyword_performance_daily "
            "WHERE account_id=? AND date >= date('now','-14 day') AND impressions > 0",
            (ACCOUNT,),
        ).fetchone()[0]
        print("  distinct criterion_ids with impressions in last 14 days: %s" % f"{recent:,}")
        print("  ^ this is a reliable 'currently serving' signal, independent of the")
        print("    corrupted status column in keyword_instances.")
    except sqlite3.OperationalError as e:
        print("  (could not run recent-serving check: %s)" % e)

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
