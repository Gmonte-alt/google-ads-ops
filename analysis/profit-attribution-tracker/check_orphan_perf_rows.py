#!/usr/bin/env python
"""
check_orphan_perf_rows.py -- sizes the orphan performance rows (§8 Q6, profit framework).

An "orphan" is a keyword_performance_daily row whose instance_id has no keyword_instances
parent -- spend that can't resolve to a placement and is excluded from the category map.
This script sizes them so we can decide: document-as-exclusion vs investigate.

Read-only (mode=ro). Safe to run anytime.

    python check_orphan_perf_rows.py
"""
import sqlite3

DB = r"C:\Projects\google-ads-ops\db\paid_search.db"


def main():
    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    c = conn.cursor()

    print("=" * 66)
    print("ORPHAN PERFORMANCE ROWS  (perf.instance_id with no ki parent)")
    print("=" * 66)

    # --- headline: the architect's query ---
    n, cost, dmin, dmax = c.execute("""
        SELECT COUNT(*), COALESCE(SUM(cost_micros)/1e6, 0),
               MIN(report_date), MAX(report_date)
        FROM keyword_performance_daily p
        LEFT JOIN keyword_instances ki ON ki.id = p.instance_id
        WHERE ki.id IS NULL
    """).fetchone()
    print("orphan rows : %s" % f"{n:,}")
    print("orphan cost : $%s" % f"{cost:,.2f}")
    print("date range  : %s .. %s" % (dmin, dmax))

    if n == 0:
        print("\nNo orphans -- nothing excluded. Done.")
        conn.close()
        return

    # --- context: how much is in the trailing 12 weeks (the analysis window)? ---
    n12, cost12 = c.execute("""
        SELECT COUNT(*), COALESCE(SUM(cost_micros)/1e6, 0)
        FROM keyword_performance_daily p
        LEFT JOIN keyword_instances ki ON ki.id = p.instance_id
        WHERE ki.id IS NULL
          AND p.report_date >= date('now', '-84 day')
    """).fetchone()
    print("\nwithin trailing 12 weeks (the waterfall window):")
    print("  rows: %s   cost: $%s" % (f"{n12:,}", f"{cost12:,.2f}"))

    # --- shape: orphan cost by month, to see if it's historical or ongoing ---
    print("\norphan cost by month:")
    for r in c.execute("""
        SELECT substr(report_date, 1, 7) ym, COUNT(*),
               ROUND(SUM(cost_micros)/1e6, 2)
        FROM keyword_performance_daily p
        LEFT JOIN keyword_instances ki ON ki.id = p.instance_id
        WHERE ki.id IS NULL
        GROUP BY ym ORDER BY ym
    """):
        print("  %s | rows %8s | $%s" % (r[0], f"{r[1]:,}", f"{r[2]:,.2f}"))

    # --- sample orphan instance_ids for tracing if it turns out material ---
    print("\nsample orphan instance_ids (for tracing):")
    for r in c.execute("""
        SELECT p.instance_id, COUNT(*), ROUND(SUM(p.cost_micros)/1e6, 2),
               MIN(p.report_date), MAX(p.report_date)
        FROM keyword_performance_daily p
        LEFT JOIN keyword_instances ki ON ki.id = p.instance_id
        WHERE ki.id IS NULL
        GROUP BY p.instance_id
        ORDER BY SUM(p.cost_micros) DESC
        LIMIT 10
    """):
        print("  instance_id %-12s rows %6s  $%-10s  %s..%s"
              % (r[0], f"{r[1]:,}", f"{r[2]:,.2f}", r[3], r[4]))

    conn.close()
    print("\nDone. Send this output to the profit-attribution architect.")


if __name__ == "__main__":
    main()
