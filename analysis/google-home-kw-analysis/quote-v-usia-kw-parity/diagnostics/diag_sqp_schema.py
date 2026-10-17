"""Read-only: find where query TEXT lives for search_query_performance.query_id, and how
platform/account gating works. Run: python diag_sqp_schema.py
"""
import sqlite3

DB = r"C:\Projects\google-ads-ops\db\paid_search.db"
c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)

print("=== tables/views containing 'quer' ===")
tabs = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")]
qtabs = [t for t in tabs if "quer" in t.lower()]
for t in qtabs:
    cols = [r[1] for r in c.execute("PRAGMA table_info(%s)" % t)]
    print("  %-40s %s" % (t, ", ".join(cols)))

print("\n=== candidate text-lookup tables (any table with a *text* column + an id) ===")
for t in tabs:
    cols = [r[1] for r in c.execute("PRAGMA table_info(%s)" % t)]
    textcols = [x for x in cols if "text" in x.lower() or x.lower() in ("query", "term", "search_term")]
    if textcols and any("id" in x.lower() for x in cols):
        n = c.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
        print("  %-40s rows=%-9d text_cols=%s all=%s" % (t, n, textcols, ", ".join(cols[:8])))

print("\n=== search_query_performance: platform values + date range ===")
for p, n in c.execute("SELECT platform, COUNT(*) FROM search_query_performance GROUP BY platform"):
    print("   platform=%r rows=%d" % (p, n))
print("   report_date range:", c.execute(
    "SELECT MIN(report_date), MAX(report_date) FROM search_query_performance").fetchone())

print("\n=== sample join sanity: first 3 rows with query_id ===")
for r in c.execute("SELECT query_id, campaign_id, report_date, cost_micros, primary_conv_count "
                   "FROM search_query_performance LIMIT 3"):
    print("  ", tuple(r))
c.close()
